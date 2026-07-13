#!/usr/bin/env python3
"""
Plum-Audio — in-process Sendspin server + per-source PushStream feeders.

Phase 1: AirPlay FIFO → in-process SendspinServer → local player, end-to-end.

Grounded in the verified aiosendspin 6.0.5 API (audited signatures; the ingest/re-route/
reclaim control plane is exercised on hardware by _resources/spike/mesh_smoke.py, and the
full render + handoff path — including the group/stream lifecycle rules this module relies
on — by _resources/spike/handoff_probe.py).

Responsibilities (this process, one per unit):
  - Run a SendspinServer on this unit (mDNS advertising OFF — we drive by URL).
  - For each active local source FIFO (/tmp/<source>-fifo), run a feeder that reads PCM and
    pushes it into the source's group via PushStream.prepare_audio + commit_audio, paced to
    real time by PushStream.sleep_to_limit_buffer (bounded latency + backpressure).
  - Own the source→group lifecycle so the mesh orchestrator can route players into a source's
    group (group.add_client) or reclaim them across units.

Design notes (the *why*, learned from the lifecycle audit + handoff probe):
  - Each source anchors its own server-side group via a dedicated, transport-less anchor
    client ("src:<source_id>"). This decouples ingest from rendering: the group (a routing
    target) exists whether or not any player is currently attached — the "servers stay,
    players roam" mesh model. The anchor is a non-player client, so the group is never
    auto-deleted when the last real player leaves.
  - The PushStream is NOT a stable handle. group.add_client() stops the *departing* client's
    old group, and start_stream() replaces any prior stream so "stale handles cannot continue
    committing." Group.remove_client() also stops the stream when no player-role client
    remains. So the feeder OWNS its stream and re-acquires it (group.start_stream()) whenever
    a commit raises StreamStoppedError — it self-heals across routing/membership churn instead
    of dying. Correct routing is therefore remove_client(old) → add_client(new); a bare
    add_client onto an already-grouped player would stop that player's current source.
  - Idle = the source service closed its FIFO writer (EOF). We flip set_live_source(False) and
    loop back to wait for the next writer, keeping the group intact so routing survives across
    source sessions.

TODO(Phase 1+):
  - Drive start_source/stop_source from the integration lifecycle (control scripts signalling
    activity); for now main() brings up a single AirPlay source from env.
  - Metadata/artwork/visualizer role emission from the control scripts (out-of-band).
  - supervisord integration (this module is the `sendspin_server` program).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os

from aiosendspin.server.audio import AudioFormat
from aiosendspin.server.group import SendspinGroup
from aiosendspin.server.push_stream import PushStream, StreamStoppedError
from aiosendspin.server.server import ConnectionReason, SendspinServer

from mesh.model import PlayerState, SourceState, UnitSnapshot
from sources.airplay_metadata import AirplayMetadataReader

logger = logging.getLogger("plum.sendspin_server")

SERVER_PORT = 8927
# AirPlay (shairport-sync pipe backend) emits 44100:16:2 PCM by default.
DEFAULT_FORMAT = AudioFormat(44100, 16, 2)
COMMIT_CHUNK_MS = 20  # feeder read/commit cadence — small for low added latency
# Keep this much audio buffered ahead of playback. Covers the player's default
# required_lead_time (250 ms) + min_buffer (250 ms) with margin, and bounds ingest latency.
TARGET_BUFFER_US = 500_000
ANCHOR_PREFIX = "src:"  # server-side group anchor client id namespace
REACQUIRE_BACKOFF_S = 0.1  # pause before re-acquiring a stopped stream (avoid hot-looping)


def _bytes_per_frame(fmt: AudioFormat) -> int:
    return fmt.channels * (fmt.bit_depth // 8)


def _chunk_bytes(fmt: AudioFormat, ms: int) -> int:
    frames = fmt.sample_rate * ms // 1000
    return frames * _bytes_per_frame(fmt)


class SourceFeeder:
    """Reads PCM from one source FIFO and pushes it into that source's group.

    Owns the group's PushStream lifecycle: acquires it via group.start_stream() and re-acquires
    it if a commit raises StreamStoppedError (which happens when routing/membership churn stops
    the stream out from under us — see module docstring). This keeps a source feeding through
    player joins/leaves without the feeder dying.

    Pacing: after each commit we yield in PushStream.sleep_to_limit_buffer(TARGET_BUFFER_US),
    which bounds latency and applies backpressure when a source bursts (shairport fills the
    pipe buffer at session start). Steady-state, the real-time FIFO writer paces us.

    Session model: EOF on the FIFO = the source service closed its writer (session ended). We
    flip the stream to non-live and loop back to wait for the next writer.
    """

    def __init__(self, source_id: str, fifo_path: str, group: SendspinGroup,
                 fmt: AudioFormat = DEFAULT_FORMAT) -> None:
        self.source_id = source_id
        self.fifo_path = fifo_path
        self.group = group
        self.fmt = fmt
        self.ps: PushStream | None = None
        self._task: asyncio.Task | None = None
        self._stop_evt = asyncio.Event()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self.run())

    async def stop(self) -> None:
        self._stop_evt.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def _acquire_stream(self) -> PushStream:
        """(Re)create this group's PushStream and mark it a live source."""
        self.ps = self.group.start_stream()
        self.ps.set_live_source(True)
        return self.ps

    def _ensure_fifo(self) -> None:
        """Create the FIFO if the source service hasn't yet, so we can open the read end and
        wait for the writer rather than racing it."""
        if not os.path.exists(self.fifo_path):
            os.mkfifo(self.fifo_path, mode=0o660)
            logger.info("[%s] created FIFO %s", self.source_id, self.fifo_path)

    async def _open_reader(self) -> tuple[asyncio.StreamReader, asyncio.ReadTransport]:
        """Open the FIFO read end non-blocking and wrap it in an asyncio StreamReader.

        O_RDONLY|O_NONBLOCK on a FIFO returns immediately even with no writer connected;
        reads then simply await (EAGAIN) until a writer appears — no busy spin, no spurious
        EOF before the first writer. EOF is only seen after a writer has connected and closed.
        """
        self._ensure_fifo()
        loop = asyncio.get_running_loop()
        fd = os.open(self.fifo_path, os.O_RDONLY | os.O_NONBLOCK)
        pipe = os.fdopen(fd, "rb", buffering=0)
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        transport, _ = await loop.connect_read_pipe(lambda: protocol, pipe)
        return reader, transport

    async def run(self) -> None:
        chunk = _chunk_bytes(self.fmt, COMMIT_CHUNK_MS)
        logger.info("[%s] feeder up: %s @ %d:%d:%d, %d-byte chunks",
                    self.source_id, self.fifo_path, self.fmt.sample_rate,
                    self.fmt.bit_depth, self.fmt.channels, chunk)
        self._acquire_stream()
        while not self._stop_evt.is_set():
            reader = transport = None
            try:
                reader, transport = await self._open_reader()
                await self._pump(reader, chunk)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - feeders must survive source hiccups
                logger.exception("[%s] feeder error; retrying", self.source_id)
                await asyncio.sleep(0.5)
            finally:
                if transport is not None:
                    transport.close()

    async def _pump(self, reader: asyncio.StreamReader, chunk: int) -> None:
        """Read fixed chunks and push them until the writer closes (session end) or we stop."""
        if self.ps is None or self.ps.is_stopped:
            self._acquire_stream()
        else:
            self.ps.set_live_source(True)
        try:
            while not self._stop_evt.is_set():
                try:
                    data = await reader.readexactly(chunk)
                    eof = False
                except asyncio.IncompleteReadError as e:
                    data = e.partial  # flush the trailing partial frame(s), then treat as EOF
                    eof = True

                if data:
                    assert self.ps is not None
                    self.ps.prepare_audio(data, self.fmt)
                    try:
                        await self.ps.commit_audio()
                    except StreamStoppedError:
                        # Routing/membership churn stopped our stream — re-acquire and re-push
                        # this same chunk so no audio is dropped.
                        logger.info("[%s] stream stopped under feeder; re-acquiring",
                                    self.source_id)
                        await asyncio.sleep(REACQUIRE_BACKOFF_S)
                        self._acquire_stream()
                        with contextlib.suppress(StreamStoppedError):
                            self.ps.prepare_audio(data, self.fmt)
                            await self.ps.commit_audio()
                    else:
                        # Yield until we're back under the buffer target: real-time pacing.
                        await self.ps.sleep_to_limit_buffer(TARGET_BUFFER_US)

                if eof:
                    logger.info("[%s] FIFO writer closed (session end) — going idle",
                                self.source_id)
                    return
        finally:
            # Mark the live source paused; the stream/group stay up for the next session.
            if self.ps is not None:
                with contextlib.suppress(Exception):
                    self.ps.set_live_source(False)


class SourceHandle:
    """A live source: its anchor group + feeder. The group is the routing target the mesh
    orchestrator adds/removes player clients on (remove_client(old) → add_client(new))."""

    def __init__(self, source_id: str, group: SendspinGroup, feeder: SourceFeeder) -> None:
        self.source_id = source_id
        self.group = group
        self.feeder = feeder


class PlumSendspinServer:
    """Owns the unit's SendspinServer and its per-source feeders/groups."""

    def __init__(self, unit_id: str, unit_name: str, port: int = SERVER_PORT) -> None:
        self.unit_id = unit_id
        self.unit_name = unit_name
        self.port = port
        self.server: SendspinServer | None = None
        self.sources: dict[str, SourceHandle] = {}
        self._local_player_tasks: list[asyncio.Task] = []
        self._metadata_readers: list[AirplayMetadataReader] = []
        self._stop_evt = asyncio.Event()

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.server = SendspinServer(loop=loop, server_id=self.unit_id, server_name=self.unit_name)
        # mDNS OFF: SendspinServer always constructs AsyncZeroconf; keep it from advertising
        # (5353 collides with our Avahi). We connect players by explicit URL via the orchestrator.
        await self.server.start_server(port=self.port, advertise_addresses=[], discover_clients=False)
        logger.info("Sendspin server up: %s (%s) :%d", self.unit_name, self.unit_id, self.port)

    async def stop(self) -> None:
        self._stop_evt.set()
        for reader in self._metadata_readers:
            await reader.stop()
        self._metadata_readers.clear()
        for task in self._local_player_tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._local_player_tasks.clear()
        for source_id in list(self.sources):
            await self.stop_source(source_id)
        if self.server:
            await self.server.stop_server()
            await self.server.close()
            self.server = None

    def start_source(self, source_id: str, fifo_path: str,
                     fmt: AudioFormat = DEFAULT_FORMAT) -> SourceHandle:
        """Create the source's anchor group and launch its FIFO feeder.

        Idempotent: returns the existing handle if the source is already running.
        """
        assert self.server is not None, "start() the server before starting sources"
        if source_id in self.sources:
            return self.sources[source_id]

        anchor = self.server.get_or_create_client(ANCHOR_PREFIX + source_id)
        group = anchor.group
        feeder = SourceFeeder(source_id, fifo_path, group, fmt)
        handle = SourceHandle(source_id, group, feeder)
        self.sources[source_id] = handle
        feeder.start()
        logger.info("[%s] source started (group=%s)", source_id, group.group_id[:8])
        return handle

    async def stop_source(self, source_id: str) -> None:
        handle = self.sources.pop(source_id, None)
        if handle is None:
            return
        await handle.feeder.stop()
        with contextlib.suppress(Exception):
            handle.group.stop_stream()
        logger.info("[%s] source stopped", source_id)

    async def attach_player(self, source_id: str, player_id: str) -> None:
        """Route a connected player into a source's group.

        Uses remove-then-add: take the player out of its current group first (that group keeps
        streaming to any remaining players), then add it here. A bare add_client would stop the
        player's *current* source group — see module docstring.
        """
        assert self.server is not None
        handle = self.sources.get(source_id)
        if handle is None:
            raise KeyError(f"unknown source {source_id!r}")
        player = self.server.get_or_create_client(player_id)
        if player.group is not None and player.group is not handle.group:
            await player.group.remove_client(player)
        await handle.group.add_client(player)
        logger.info("[%s] attached player %s", source_id, player_id)

    async def detach_player(self, source_id: str, player_id: str) -> None:
        assert self.server is not None
        handle = self.sources.get(source_id)
        if handle is None:
            return
        player = self.server.get_client(player_id)
        if player is not None:
            await handle.group.remove_client(player)
            logger.info("[%s] detached player %s", source_id, player_id)

    def preconnect_player(self, player_id: str, player_url: str) -> None:
        """Hold a remote player in a DISCOVERY connection so a later route is cheap.

        The idle fast-switch primitive (replaces Plum-Snapcast's auto-switch-service). Dialing
        with ConnectionReason.DISCOVERY parks the player without claiming it for playback; a
        subsequent reclaim_remote_player() promotes it to PLAYBACK without a cold connect.
        """
        assert self.server is not None
        self.server.register_client_url(player_id, player_url)
        self.server.connect_to_client(
            player_url, connection_reason=ConnectionReason.DISCOVERY,
            retry_initial_connection=True)

    async def reclaim_remote_player(self, source_id: str, player_id: str, player_url: str,
                                    timeout_s: float = 10.0) -> bool:
        """Pull a player from its current (peer) server onto a local source group.

        The cross-server roam primitive. `reclaim_client_for_playback` is SYNCHRONOUS: it dials
        the player for PLAYBACK and schedules the reclaim timeout, returning whether a URL was
        available — it does NOT wait for the player to land here. The player then releases its
        old server with GoodbyeReason.ANOTHER_SERVER (handshake in sendspin_player.py) and
        reconnects to us. So we register the URL, initiate the reclaim, wait for the player to
        actually reconnect, then group it. Reconnect-class (~200 ms on hardware); a prior
        preconnect_player() masks most of that. Returns False if no URL or the player never lands.
        """
        assert self.server is not None
        handle = self.sources.get(source_id)
        if handle is None:
            raise KeyError(f"unknown source {source_id!r}")
        self.server.register_client_url(player_id, player_url)
        if not self.server.reclaim_client_for_playback(player_id, timeout_s=timeout_s):
            logger.warning("[%s] no URL to reclaim player %s", source_id, player_id)
            return False
        if not await self._await_client_connected(player_id, timeout_s):
            logger.warning("[%s] reclaim of remote player %s timed out", source_id, player_id)
            return False
        await self.attach_player(source_id, player_id)
        logger.info("[%s] reclaimed remote player %s from %s", source_id, player_id, player_url)
        return True

    async def _await_client_connected(self, player_id: str, timeout_s: float) -> bool:
        """Poll until a (reclaimed) player has reconnected to this server, or timeout."""
        assert self.server is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            client = self.server.get_client(player_id)
            if client is not None and client.is_connected:
                return True
            await asyncio.sleep(0.05)
        return False

    def snapshot(self) -> UnitSnapshot:
        """This unit's local view for the mesh aggregator / REST snapshot.

        Structural only (sources, grouping, streaming, connected players). `host` is left None —
        the aggregator fills it from the beacon source IP, the one authority on how peers reach us.
        """
        sources: list[SourceState] = []
        for source_id, handle in self.sources.items():
            group = handle.group
            player_ids = [c.client_id for c in group.clients
                          if not c.client_id.startswith(ANCHOR_PREFIX)]
            sources.append(SourceState(
                source_id=source_id, group_id=group.group_id, group_name=group.group_name,
                streaming=group.has_active_stream, player_ids=player_ids))

        players: list[PlayerState] = []
        if self.server is not None:
            for client in self.server.clients:
                if client.client_id.startswith(ANCHOR_PREFIX):
                    continue  # anchors are group scaffolding, not render endpoints
                if not client.is_connected:
                    continue  # a disconnected client isn't a live endpoint here — e.g. a player
                    # that roamed to a peer leaves a stub; reporting it would make the mesh view
                    # (and the router's find_player) think the player is still on this unit.
                players.append(PlayerState(
                    player_id=client.client_id, name=client.name or client.client_id,
                    connected=True,
                    group_id=client.group.group_id if client.group is not None else None,
                    url=self.server.get_client_url(client.client_id)))

        return UnitSnapshot(unit_id=self.unit_id, name=self.unit_name, host=None,
                            sources=sources, players=players)

    def start_airplay_metadata(self, source_id: str, metadata_fifo: str) -> None:
        """Attach the shairport metadata/artwork → Sendspin roles reader to a source's group."""
        handle = self.sources.get(source_id)
        if handle is None:
            raise KeyError(f"unknown source {source_id!r}")
        reader = AirplayMetadataReader(handle.group, metadata_fifo)
        self._metadata_readers.append(reader)
        reader.start()
        logger.info("[%s] airplay metadata reader started (%s)", source_id, metadata_fifo)

    def attach_local_player(self, source_id: str, player_id: str, player_url: str,
                            *, supervise: bool = True) -> None:
        """Attach this unit's own player to a source, registering its reclaim URL.

        Always registers the player's URL so peers can reclaim it. Then, depending on `supervise`:
          - True  (single-unit / mesh off): keep the player attached to the local source, dialing
            and re-attaching whenever it (re)connects — self-heals across player/process restarts.
          - False (mesh on): dial + attach ONCE. The mesh orchestrator owns routing thereafter; a
            perpetual re-attach would fight cross-server roams (yank the player back the instant a
            peer reclaims it). Registration still lets peers find and reclaim this player by URL.
        """
        assert self.server is not None
        self.server.register_client_url(player_id, player_url)
        coro = (self._supervise_local_player(source_id, player_id, player_url) if supervise
                else self._attach_local_player_once(source_id, player_id, player_url))
        self._local_player_tasks.append(asyncio.ensure_future(coro))

    async def _attach_local_player_once(self, source_id: str, player_id: str,
                                        player_url: str) -> None:
        """Dial the local player and attach it to its source exactly once (mesh-owned routing).

        Does not re-dial after a later disconnect: when the player roams to a peer it sends
        GoodbyeReason.ANOTHER_SERVER and the library stops retrying this URL, so there is nothing
        to fight. Initial connection is still retried (the player may not be up yet at boot).
        """
        assert self.server is not None
        self.server.connect_to_client(
            player_url, connection_reason=ConnectionReason.PLAYBACK, retry_initial_connection=True)
        if await self._await_client_connected(player_id, timeout_s=30.0):
            with contextlib.suppress(Exception):
                await self.attach_player(source_id, player_id)
                logger.info("[%s] local player %s attached (mesh-owned)", source_id, player_id)

    def _dial_local_player(self, player_url: str) -> None:
        assert self.server is not None
        self.server.connect_to_client(
            player_url, connection_reason=ConnectionReason.PLAYBACK,
            retry_initial_connection=True, retry_indefinitely=True)

    async def _supervise_local_player(self, source_id: str, player_id: str,
                                      player_url: str) -> None:
        """Dial + (re)attach the local player, self-healing across restarts.

        We proactively re-dial while disconnected: a clean player shutdown sends a goodbye, after
        which the library stops retrying that URL — so on a supervisord restart we must dial
        again rather than wait for a reconnect that never comes.
        """
        assert self.server is not None
        REDIAL_EVERY = 3  # seconds between re-dials while the player is down
        attached = False
        down_ticks = REDIAL_EVERY  # dial immediately on first pass
        while not self._stop_evt.is_set():
            client = self.server.get_client(player_id)
            connected = client is not None and client.is_connected
            if connected:
                down_ticks = 0
                if not attached:
                    try:
                        await self.attach_player(source_id, player_id)
                        attached = True
                    except Exception:  # noqa: BLE001 - transient during connect/route churn
                        logger.exception("[%s] local player attach failed; will retry", source_id)
            else:
                if attached:
                    attached = False
                    logger.info("[%s] local player %s down; re-dialing", source_id, player_id)
                if down_ticks >= REDIAL_EVERY:
                    self._dial_local_player(player_url)
                    down_ticks = 0
                down_ticks += 1
            await asyncio.sleep(1.0)


async def main() -> None:
    logging.basicConfig(
        level=os.environ.get("PLUM_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    unit_id = os.environ.get("PLUM_UNIT_ID", "unit-local")
    unit_name = os.environ.get("PLUM_UNIT_NAME", "Plum Audio")
    airplay_fifo = os.environ.get("PLUM_AIRPLAY_FIFO", "/tmp/airplay-fifo")
    airplay_meta_fifo = os.environ.get("PLUM_AIRPLAY_METADATA_FIFO", "/tmp/airplay-metadata-fifo")
    # Single-unit glue: auto-attach our own player to the AirPlay source. Empty URL disables it
    # (Phase 2: the mesh orchestrator drives routing instead).
    local_player_id = os.environ.get("PLUM_LOCAL_PLAYER_ID", f"{unit_id}-player")
    local_player_url = os.environ.get("PLUM_LOCAL_PLAYER_URL", "ws://127.0.0.1:8928/sendspin")

    mesh_enabled = os.environ.get("PLUM_MESH_ENABLED", "1") != "0"

    srv = PlumSendspinServer(unit_id, unit_name)
    await srv.start()
    # Phase 1: bring up the AirPlay source immediately. The feeder waits for shairport-sync to
    # open the FIFO writer, so starting it eagerly is safe.
    srv.start_source("airplay", airplay_fifo)
    srv.start_airplay_metadata("airplay", airplay_meta_fifo)  # metadata/artwork → roles (item 3)
    if local_player_url:
        # With the mesh on, register + attach the local player once and let routing roam it;
        # the perpetual re-attach supervisor (Phase-1 glue) would fight cross-server reclaims.
        srv.attach_local_player("airplay", local_player_id, local_player_url,
                                supervise=not mesh_enabled)

    # Phase 2: the mesh (discovery + aggregation + routing + REST). Local playback above stands
    # on its own; the mesh layers cross-unit roaming on top. Disable with PLUM_MESH_ENABLED=0.
    mesh = None
    if mesh_enabled:
        from mesh.orchestrator import MeshOrchestrator  # local import: avoids an import cycle
        mesh = MeshOrchestrator(
            srv, beacon_port=int(os.environ.get("PLUM_BEACON_PORT", "8929")),
            api_port=int(os.environ.get("PLUM_MESH_API_PORT", "5001")))
        await mesh.start()

    stop = asyncio.Event()
    try:
        await stop.wait()  # run forever; supervisord manages the process lifecycle
    except asyncio.CancelledError:
        pass
    finally:
        if mesh is not None:
            await mesh.stop()
        await srv.stop()


if __name__ == "__main__":
    asyncio.run(main())
