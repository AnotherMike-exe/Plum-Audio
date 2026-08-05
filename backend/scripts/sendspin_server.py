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
  - Idle = the source service closed its FIFO writer (EOF), or went quiet past
    PLUM_SOURCE_IDLE_TIMEOUT. We announce it the way the spec does — `group.stop()` sets
    playback_state=stopped and pushes a group/update to every client — then loop back to wait
    for the next writer. A stream exists ONLY while a sender is feeding us; the group and its
    anchor persist regardless, so routing survives across source sessions.

TODO(Phase 1+):
  - Drive start_source/stop_source from the integration lifecycle (control scripts signalling
    activity); for now main() brings up a single AirPlay source from env.
  - Metadata/artwork/visualizer role emission from the control scripts (out-of-band).
  - supervisord integration (this module is the `sendspin_server` program).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import os
import time

from aiosendspin.models.types import GoodbyeReason, MediaCommand, has_role_family
from aiosendspin.server.audio import AudioFormat
from aiosendspin.server.group import SendspinGroup
from aiosendspin.server.push_stream import PushStream, StreamStoppedError
from aiosendspin.server.roles.controller.events import (
    ControllerNextEvent,
    ControllerPauseEvent,
    ControllerPlayEvent,
    ControllerPreviousEvent,
    ControllerRepeatEvent,
    ControllerShuffleEvent,
)
from aiosendspin.server.roles.player import PlayerV1Role
from aiosendspin.server.server import ClientAddedEvent, ClientUpdatedEvent, ConnectionReason, SendspinServer

from mesh.model import PlayerState, SourceState, UnitSnapshot
from sources import airplay_config, bluetooth_config, spotify_config
from sources.airplay_metadata import AirplayMetadataReader
from sources.airplay_manager import AirplayManager
from sources.airplay_remote import AirplayRemote
from sources.bluetooth_adapter import BluetoothAdapter
from sources.bluetooth_avrcp import BluetoothAvrcp
from sources.bluetooth_coverart import BluetoothCoverArt
from sources.bluetooth_manager import BluetoothManager
from sources.spotify_golibrespot import SpotifyGoLibrespot
from sources.spotify_manager import SpotifyManager

import unit_identity

logger = logging.getLogger("plum.sendspin_server")

SERVER_PORT = 8927
# AirPlay (shairport-sync pipe backend) emits 44100:16:2 PCM by default.
DEFAULT_FORMAT = AudioFormat(44100, 16, 2)
COMMIT_CHUNK_MS = 20  # feeder read/commit cadence — small for low added latency
# Keep this much audio buffered ahead of playback. Covers the player's default
# required_lead_time (250 ms) + min_buffer (250 ms) with margin, and bounds ingest latency.
TARGET_BUFFER_US = 500_000
ANCHOR_PREFIX = "src:"  # server-side group anchor client id namespace
# A source counts as "in use" while audio keeps arriving. Past this gap (or once the writer closes)
# it goes idle: we announce group playback_state=stopped, and the GUI drops it from the stream list.
# The group, feeder and routing all stay up, so any sender can reconnect to it at any time.
# Generous by default so a long pause doesn't tear a stream down mid-listen.
SOURCE_IDLE_TIMEOUT_S = float(os.environ.get("PLUM_SOURCE_IDLE_TIMEOUT", "300"))
CONTROLLER_PREFIX = "ctrl:"  # GUI controller client id namespace: "ctrl:<source_id>:<nonce>"
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

    Session model: a stream exists only while a sender is actually feeding us. We start one on the
    first audio (group.start_stream() → playback_state=playing) and stop the group on EOF — the
    source service closed its writer, i.e. the session ended — or after SOURCE_IDLE_TIMEOUT_S of
    silence with the pipe still open. `SendspinGroup.stop()` is the spec's way to say "not playing":
    it announces playback_state=stopped in a group/update to every client and freezes the metadata
    progress anchor. (`stop_stream()` deliberately does NOT — it keeps clients logically PLAYING for
    a stream-to-stream transition, which is not our case.) The group and its anchor persist through
    all of this, so routing survives an idle source and the next session just starts a new stream.
    """

    def __init__(self, source_id: str, fifo_path: str, group: SendspinGroup, fmt: AudioFormat = DEFAULT_FORMAT) -> None:
        self.source_id = source_id
        self.fifo_path = fifo_path
        self.group = group
        self.fmt = fmt
        self.ps: PushStream | None = None
        self._task: asyncio.Task | None = None
        self._stop_evt = asyncio.Event()
        self._last_data_at: float | None = None  # monotonic; None = idle (announced as stopped)

    @property
    def is_active(self) -> bool:
        """Is a sender actually feeding this source right now?

        Mirrors exactly what we announce on the wire: True between the first audio of a session
        (playback_state=playing) and the EOF or idle timeout that ends it (playback_state=stopped).
        The mesh view carries this so the GUI can hide idle sources; it is never a second, separate
        notion of "playing".
        """
        return self._last_data_at is not None

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

    def refresh_stream(self) -> None:
        """Re-acquire the stream so a player just added to the group is actually IN it.

        A stream's membership is fixed when start_stream() is called. A client that CONNECTS while
        a stream is live is handed it during the handshake, but a client that was already connected
        and is later added to the group is not — it sits in the group, in the GUI, at the right
        volume, and completely silent until the source's next session.

        Measured on .7.204 on 2026-08-04: with airplay-1 streaming, unrouting and re-routing the
        already-connected local player produced no second "Stream started" on the client and a
        renderer whose buffer never left 0 ms. It looked exactly like a dead speaker. The reason
        roaming never showed this is that a cross-server roam RECONNECTS (goodbye another_server),
        and a reconnect gets the stream for free; only the intra-server "live re-route, no
        reconnect" path — the one ARCHITECTURE calls tier 1 — was affected.

        Cost: start_stream() replaces the stream for the whole group, so members already playing get
        a brief discontinuity. That is the same cost the manual unjoin/rejoin workaround already
        paid, it only happens on a deliberate routing change, and it is strictly better than one
        endpoint being silently mute.
        """
        if self.ps is None or self.ps.is_stopped:
            return  # nothing playing: the next chunk starts a stream that includes everyone
        self._acquire_stream()
        logger.info("[%s] stream re-acquired to include a new group member", self.source_id)

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
        logger.info(
            "[%s] feeder up: %s @ %d:%d:%d, %d-byte chunks",
            self.source_id,
            self.fifo_path,
            self.fmt.sample_rate,
            self.fmt.bit_depth,
            self.fmt.channels,
            chunk,
        )
        # No stream until a sender actually feeds us: starting one here would announce
        # playback_state=playing to every client from boot, for a source nobody is using.
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
        """Push audio while a sender feeds us; announce idle when it stops or goes quiet."""
        while not self._stop_evt.is_set():
            try:
                data = await asyncio.wait_for(reader.readexactly(chunk), timeout=SOURCE_IDLE_TIMEOUT_S)
                eof = False
            except asyncio.IncompleteReadError as e:
                data = e.partial  # flush the trailing partial frame(s), then treat as EOF
                eof = True
            except asyncio.TimeoutError:
                # Pipe still open, but the sender has gone quiet for a long time.
                await self._go_idle("no audio for %.0fs" % SOURCE_IDLE_TIMEOUT_S)
                continue

            if data:
                if self.ps is None or self.ps.is_stopped:
                    self._acquire_stream()  # first audio of a session → playback_state=playing
                    logger.info("[%s] active: sender feeding us (playback_state=playing)", self.source_id)
                self._last_data_at = time.monotonic()
                assert self.ps is not None
                self.ps.prepare_audio(data, self.fmt)
                try:
                    await self.ps.commit_audio()
                except StreamStoppedError:
                    # Routing/membership churn stopped our stream — re-acquire and re-push
                    # this same chunk so no audio is dropped.
                    logger.info("[%s] stream stopped under feeder; re-acquiring", self.source_id)
                    await asyncio.sleep(REACQUIRE_BACKOFF_S)
                    self._acquire_stream()
                    with contextlib.suppress(StreamStoppedError):
                        self.ps.prepare_audio(data, self.fmt)
                        await self.ps.commit_audio()
                else:
                    # Yield until we're back under the buffer target: real-time pacing.
                    await self.ps.sleep_to_limit_buffer(TARGET_BUFFER_US)

            if eof:
                await self._go_idle("FIFO writer closed (session end)")
                return

    async def _go_idle(self, why: str) -> None:
        """Announce that nothing is playing on this source, per the spec.

        group.stop() sets playback_state=stopped and pushes a group/update to every client (and
        freezes the metadata progress anchor). Deliberately NOT stop_stream(), which keeps clients
        logically PLAYING for a stream-to-stream handover. The group survives — its anchor client
        holds it — so players stay routed here and the next session just starts a new stream.
        """
        if self._last_data_at is None and (self.ps is None or self.ps.is_stopped):
            return  # already idle
        self._last_data_at = None
        self.ps = None
        with contextlib.suppress(Exception):
            await self.group.stop()
        logger.info("[%s] idle: %s (announced playback_state=stopped)", self.source_id, why)


class SourceHandle:
    """A live source: its anchor group + feeder. The group is the routing target the mesh
    orchestrator adds/removes player clients on (remove_client(old) → add_client(new))."""

    def __init__(self, source_id: str, group: SendspinGroup, feeder: SourceFeeder, name: str = "") -> None:
        self.source_id = source_id
        self.group = group
        self.feeder = feeder
        self.name = name or source_id  # endpoint device name, shown in the GUI; follows a rename


class PlumSendspinServer:
    """Owns the unit's SendspinServer and its per-source feeders/groups."""

    def __init__(self, unit_id: str, unit_name: str, port: int = SERVER_PORT) -> None:
        self.unit_id = unit_id
        self.unit_name = unit_name
        self.port = port
        self.server: SendspinServer | None = None
        self.sources: dict[str, SourceHandle] = {}
        self._primary_source: str | None = None  # source group that controller-only clients join
        self._local_player_tasks: list[asyncio.Task] = []
        self._metadata_readers: dict[str, AirplayMetadataReader] = {}  # source_id -> shairport pipe reader
        self._airplay_remotes: dict[str, AirplayRemote] = {}  # source_id -> per-instance MPRIS remote
        self._spotify_monitors: dict[str, SpotifyGoLibrespot] = {}  # source_id -> go-librespot event monitor
        self._bluetooth_adapters: dict[str, BluetoothAdapter] = {}  # source_id -> BlueZ adapter owner
        self._bluetooth_avrcp: dict[str, BluetoothAvrcp] = {}  # source_id -> AVRCP metadata/transport
        self._bluetooth_coverart: dict[str, BluetoothCoverArt] = {}  # source_id -> OBEX cover-art fetcher
        # Per-source transport remote (has async play/pause/next_track/previous_track). AirPlay's
        # AirplayRemote and each SpotifyGoLibrespot both satisfy it, so controller events route by source.
        self._source_remotes: dict[str, object] = {}
        self._wired_sources: set[str] = set()  # source_ids whose group already has the control listener
        # source_id -> {"volume": int|None, "muted": bool|None} as the SENDER last reported it (the
        # phone's AirPlay/BT slider, Spotify Connect device volume). Fed by each remote's readback
        # callback and served in the snapshot; the protocol carries no such state, so this is ours.
        self._source_volumes: dict[str, dict] = {}
        self._stop_evt = asyncio.Event()

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.server = SendspinServer(loop=loop, server_id=self.unit_id, server_name=self.unit_name)
        # mDNS OFF: SendspinServer always constructs AsyncZeroconf; keep it from advertising
        # (5353 collides with our Avahi). We connect players by explicit URL via the orchestrator.
        await self.server.start_server(port=self.port, advertise_addresses=[], discover_clients=False)
        # Join controller-only clients (the GUI's metadata/artwork/controller WS) to a source group
        # so they receive its now-playing state — the server otherwise leaves them in a solo group.
        self.server.add_event_listener(self._on_server_event)
        logger.info("Sendspin server up: %s (%s) :%d", self.unit_name, self.unit_id, self.port)

    async def stop(self) -> None:
        self._stop_evt.set()
        for remote in self._airplay_remotes.values():
            await remote.close()
        self._airplay_remotes.clear()
        for monitor in self._spotify_monitors.values():
            await monitor.stop()
        self._spotify_monitors.clear()
        for avrcp in self._bluetooth_avrcp.values():
            await avrcp.stop()
        self._bluetooth_avrcp.clear()
        for cover_art in self._bluetooth_coverart.values():
            await cover_art.stop()
        self._bluetooth_coverart.clear()
        for adapter in self._bluetooth_adapters.values():
            await adapter.stop()
        self._bluetooth_adapters.clear()
        self._source_remotes.clear()
        for reader in self._metadata_readers.values():
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

    def start_source(
        self, source_id: str, fifo_path: str, fmt: AudioFormat = DEFAULT_FORMAT, *, name: str = ""
    ) -> SourceHandle:
        """Create the source's anchor group and launch its FIFO feeder.

        Idempotent: returns the existing handle if the source is already running.
        """
        assert self.server is not None, "start() the server before starting sources"
        if source_id in self.sources:
            return self.sources[source_id]

        anchor = self.server.get_or_create_client(ANCHOR_PREFIX + source_id)
        group = anchor.group
        feeder = SourceFeeder(source_id, fifo_path, group, fmt)
        handle = SourceHandle(source_id, group, feeder, name=name)
        self.sources[source_id] = handle
        if self._primary_source is None:
            self._primary_source = source_id  # first source: where controller clients get grouped
        feeder.start()
        logger.info("[%s] source started (group=%s)", source_id, group.group_id[:8])
        return handle

    def set_source_name(self, source_id: str, name: str) -> None:
        """Rename a source's GUI label in place (the user renamed its endpoint in Settings)."""
        handle = self.sources.get(source_id)
        if handle is None or not name or handle.name == name:
            return
        handle.name = name
        logger.info("[%s] source renamed to %r", source_id, name)

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
        if player.group is handle.group:
            return  # already on this source — idempotent, avoids a redundant re-group
        if player.group is not None:
            await player.group.remove_client(player)
        await handle.group.add_client(player)
        # add_client alone does not put an already-connected player into a stream that is already
        # running — see SourceFeeder.refresh_stream. Without this the player joins the group and
        # stays silent.
        handle.feeder.refresh_stream()
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

    def _on_server_event(self, _server: SendspinServer, event: object) -> None:
        """React to client lifecycle events. A controller-only client (the GUI's now-playing WS)
        connects into its own solo group by default, where it sees no source metadata — join it to
        the primary source group so its metadata/artwork/playback-state roles receive live state."""
        if isinstance(event, (ClientAddedEvent, ClientUpdatedEvent)):
            asyncio.ensure_future(self._maybe_group_controller(event.client_id))

    async def _maybe_group_controller(self, client_id: str) -> None:
        """Join a controller-only client to the source group it asked for (or the primary source).

        A client holds exactly one websocket and therefore sits in exactly one group, so a single
        controller can only ever see ONE source's now-playing. The GUI opens one controller per
        source and names it "ctrl:<source_id>:<nonce>"; we honour that request here. Without the
        hint (any other client id) it lands on the primary source, preserving the Phase-1 behaviour.
        """
        if self.server is None or self._primary_source is None:
            return
        if client_id.startswith(ANCHOR_PREFIX):
            return  # anchors are group scaffolding, not clients to regroup
        client = self.server.get_client(client_id)
        if client is None or not client.is_connected:
            return
        if has_role_family("player", client.negotiated_roles):
            return  # a player — the mesh orchestrator owns its routing, never regroup it here
        source_id = self._requested_source(client_id) or self._primary_source
        handle = self.sources.get(source_id)
        if handle is None or client.group is handle.group:
            return  # unknown source, or already grouped — idempotent
        with contextlib.suppress(Exception):
            await handle.group.add_client(client)
            logger.info("[%s] grouped controller client %s", source_id, client_id)
            # A controller joining creates the controller group role; (re)advertise transport
            # commands on it now so this controller sees the source's supported commands. Gate on
            # this source having a remote — a source with no transport control (a bare FIFO) must not
            # claim commands it can't honour. The command set is per-remote (Spotify adds
            # repeat/shuffle; AirPlay stays play/pause/next/previous — see _supported_commands_for).
            remote = self._source_remotes.get(source_id)
            if remote is not None:
                controller = handle.group.group_role("controller")
                if controller is not None:
                    controller.set_supported_commands(self._supported_commands_for(source_id))
                    # The controller role only exists once a controller is present, so republish the
                    # source's current repeat/shuffle onto it now that this one has joined.
                    if hasattr(remote, "push_modes"):
                        remote.push_modes()

    def _requested_source(self, client_id: str) -> str | None:
        """The source a controller client asked to observe, from a "ctrl:<source_id>:<nonce>" id."""
        if not client_id.startswith(CONTROLLER_PREFIX):
            return None
        requested = client_id[len(CONTROLLER_PREFIX) :].split(":", 1)[0]
        return requested if requested in self.sources else None

    def set_player_volume(self, player_id: str, volume: int, muted: bool) -> None:
        """Set one player's volume (0-100) and mute — per-client, independent of its group.

        Drives the player's Sendspin volume/mute role commands; the player applies them as
        render-side gain (AlsaRenderer). Per-client so concurrent groups (and members within a
        group) each hold their own level.
        """
        assert self.server is not None
        client = self.server.get_client(player_id)
        if client is None or not client.is_connected:
            raise KeyError(f"player {player_id!r} not connected")
        # The volume/mute setters live on the active player Role object (roles_by_family), not on
        # its persistent role *state* (which is what get_role_state returns).
        roles = [r for r in client.roles_by_family("player") if isinstance(r, PlayerV1Role)]
        if not roles:
            raise RuntimeError(f"player {player_id!r} has no negotiated player role")
        for role in roles:
            role.set_volume(max(0, min(100, volume)))
            role.set_mute(muted)
        logger.info("[vol] player %s -> %d%%%s", player_id, volume, " (muted)" if muted else "")

    # -- source volume (the level ON THE SENDING DEVICE) ----------------------
    #
    # Distinct from everything above: player volume (and the controller role's group volume, which
    # the library derives from it) is OUR output gain. This is the phone's own AirPlay/Bluetooth
    # slider, or the Spotify Connect device volume — it changes what the sender transmits, and it is
    # visible on the sender's screen. The Sendspin spec has no concept of it, so it travels over our
    # mesh API and snapshot rather than the protocol.

    def _supports_source_volume(self, source_id: str) -> bool:
        remote = self._source_remotes.get(source_id)
        return remote is not None and getattr(remote, "supports_source_volume", False)

    def note_source_volume(self, source_id: str, volume: int | None = None, muted: bool | None = None) -> None:
        """Record what the sender reports its volume/mute to be (the readback half)."""
        state = self._source_volumes.setdefault(source_id, {"volume": None, "muted": None})
        if volume is not None:
            state["volume"] = max(0, min(100, int(volume)))
        if muted is not None:
            state["muted"] = bool(muted)
        logger.debug("[srcvol] %s reports %s", source_id, state)

    async def set_source_volume(self, source_id: str, volume: int | None = None, muted: bool | None = None) -> None:
        """Drive the sending device's own volume/mute through this source's remote.

        Optimistically caches the requested value so the GUI holds it: the sender confirms with its
        own event a moment later (MPRIS PropertiesChanged / AVRCP / go-librespot), which overwrites
        this with the truth.
        """
        if source_id not in self.sources:
            raise KeyError(f"unknown source {source_id!r}")
        remote = self._source_remotes.get(source_id)
        if remote is None or not getattr(remote, "supports_source_volume", False):
            raise RuntimeError(f"source {source_id!r} has no source-volume control")
        if volume is not None:
            await remote.set_source_volume(max(0, min(100, int(volume))))
        if muted is not None and hasattr(remote, "set_source_mute"):
            await remote.set_source_mute(bool(muted))
        self.note_source_volume(source_id, volume, muted)
        logger.info("[srcvol] %s -> vol=%s muted=%s", source_id, volume, muted)

    async def reclaim_remote_player(
        self, source_id: str, player_id: str, player_url: str, timeout_s: float = 10.0
    ) -> bool:
        """Pull a player from its current (peer) server onto a local source group.

        The cross-server roam primitive. `reclaim_client_for_playback` is SYNCHRONOUS: it dials
        the player for PLAYBACK and schedules the reclaim timeout, returning whether a URL was
        available — it does NOT wait for the player to land here. The player then releases its
        old server with GoodbyeReason.ANOTHER_SERVER (handshake in sendspin_player.py) and
        reconnects to us. So we register the URL, initiate the reclaim, wait for the player to
        actually reconnect, then group it. Returns False if no URL or the player never lands.

        Cost: reconnect-class, ~25-55 ms on hardware — but INAUDIBLE. The player never flushes on
        a roam (no stream_clear/stream_end fires), so its jitter buffer (~300 ms) keeps feeding the
        DAC straight through the reconnect: measured pad_ms (emitted silence) does not move across
        a roam. Seamless as long as reconnect << buffer depth, which holds with ~6x headroom.

        There is deliberately NO DISCOVERY "pre-connect" to warm this path: a SendspinClient holds
        exactly ONE websocket (attach_websocket raises if already connected), so a player cannot
        hold a warm connection to a second server while playing on the first. Worse, the server
        only reports connection_reason in server/hello — which the client sees only AFTER it has
        attached — so a player cannot even decline a DISCOVERY dial while busy: our on_connection
        would treat it as a reclaim and yank the player off its current server, causing the very
        dropout the buffer otherwise prevents. The buffer already masks the gap; nothing to warm.
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

    async def adopt_foreign_client(
        self, source_id: str, url: str, player_id: str | None = None, timeout_s: float = 15.0
    ) -> bool:
        """Dial a Sendspin speaker we did not create and put it on one of our sources.

        A speaker found by mDNS is just a player whose URL came from the neighbourhood instead of a
        peer snapshot, so this is the same pair of primitives the mesh uses for a peer's player:
        connect_to_client(PLAYBACK) then group.add_client. Its previous server loses it the spec's
        way — the client sends goodbye `another_server`. We learn its real client_id from the
        handshake, so `player_id` is only a hint for URL registration.
        """
        assert self.server is not None
        if source_id not in self.sources:
            raise KeyError(f"unknown source {source_id!r}")
        before = {c.client_id for c in self.server.clients}
        if player_id:
            self.server.register_client_url(player_id, url)
        # Drop any dial we are already holding for this URL before opening a new one. Without this a
        # second adopt is a NO-OP: connect_to_client sees a live registration for the URL and does
        # nothing, so if the speaker went away in the meantime (unplugged, rebooted, or taken back by
        # its own server) nothing ever re-dials and we time out blaming it — "never connected" about
        # a speaker whose port is plainly open. Only release_foreign_client cancelled the dial, which
        # is why releasing first was the accidental workaround.
        # Measured on .7.204 against a Home Assistant Voice PE, seconds apart on the same URL:
        # adopt alone -> ok:false; release (whose only extra step is this) then adopt -> ok:true.
        with contextlib.suppress(Exception):
            self.server.disconnect_from_client(url)
        self.server.connect_to_client(url, connection_reason=ConnectionReason.PLAYBACK, retry_initial_connection=True)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            for client in self.server.clients:
                if not client.is_connected:
                    continue
                if not has_role_family("player", client.negotiated_roles):
                    continue  # not a render endpoint (a controller connecting for its own reasons)
                # Identify the speaker we just dialled. The URL is the reliable test and has to come
                # first: a speaker adopted ONCE stays in self.server.clients, so on every later
                # adopt it is already in `before`, and its handshake id (a MAC, for a Home Assistant
                # Voice PE) is not the mDNS name the GUI passes as player_id — so the other two
                # tests both miss and the adopt fails with "never connected" while the speaker is
                # sitting there connected. Measured on .7.204: adopted twice, then never again.
                registered = self.server.get_client_url(client.client_id)
                if registered == url or client.client_id == player_id or client.client_id not in before:
                    self.server.register_client_url(client.client_id, url)
                    await self.attach_player(source_id, client.client_id)
                    logger.info("[%s] adopted foreign speaker %s (%s)", source_id, client.client_id, url)
                    return True
            await asyncio.sleep(0.1)
        logger.warning("[%s] foreign speaker at %s never connected", source_id, url)
        return False

    async def release_foreign_client(self, source_id: str, player_id: str, url: str | None = None) -> None:
        """Hand a foreign speaker back: leave the group, cancel our dial, and drop the connection.

        Three steps, all needed. detach leaves our group; disconnect_from_client only cancels the
        server-initiated connection task for that URL, which by itself leaves the speaker sitting
        connected to us and unavailable to its own server; remove_client is what actually closes it
        out and forgets it. Verified on a real third-party speaker — without the last step it stayed
        connected and Music Assistant could not take it back.
        """
        await self.detach_player(source_id, player_id)
        if self.server is not None:
            target = url or self.server.get_client_url(player_id)
            if target:
                with contextlib.suppress(Exception):
                    self.server.disconnect_from_client(target)  # cancel OUR dial, so we don't re-take it
            client = self.server.get_client(player_id)
            if client is not None:
                # Actually hang up. Neither disconnect_from_client (which only cancels our dial's
                # bookkeeping) nor remove_client (registry only) nor detach_connection (internal
                # state) closes the live websocket — verified on hardware: the speaker stayed
                # ESTABLISHed to us and out of reach of its own server. SendspinConnection.disconnect
                # is the only thing that does, and 6.0.5 exposes it only via a private attribute.
                # TODO(upstream): ask aiosendspin for a public "hang up on this client".
                conn = getattr(client, "_connection", None)
                if conn is not None:
                    with contextlib.suppress(Exception):
                        await conn.disconnect(retry_connection=False)
                with contextlib.suppress(Exception):
                    client.detach_connection(GoodbyeReason.USER_REQUEST)
            with contextlib.suppress(Exception):
                await self.server.remove_client(player_id)
        logger.info("[%s] released foreign speaker %s", source_id, player_id)

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
            player_ids = [
                c.client_id
                for c in group.clients
                if not c.client_id.startswith(ANCHOR_PREFIX) and has_role_family("player", c.negotiated_roles)
            ]
            src_vol = self._source_volumes.get(source_id, {})
            sources.append(
                SourceState(
                    source_id=source_id,
                    group_id=group.group_id,
                    group_name=group.group_name,
                    streaming=group.has_active_stream,
                    player_ids=player_ids,
                    name=handle.name,
                    active=handle.feeder.is_active,
                    source_volume=src_vol.get("volume"),
                    source_muted=src_vol.get("muted"),
                    supports_source_volume=self._supports_source_volume(source_id),
                )
            )

        players: list[PlayerState] = []
        if self.server is not None:
            for client in self.server.clients:
                if client.client_id.startswith(ANCHOR_PREFIX):
                    continue  # anchors are group scaffolding, not render endpoints
                if not client.is_connected:
                    continue  # a disconnected client isn't a live endpoint here — e.g. a player
                    # that roamed to a peer leaves a stub; reporting it would make the mesh view
                    # (and the router's find_player) think the player is still on this unit.
                if not has_role_family("player", client.negotiated_roles):
                    continue  # controller/display clients (the GUI WS) are grouped for metadata, not players
                # The level the PLAYER reported (client/state), which is what the role object holds —
                # set_volume() alone does not move it, so this is the endpoint's real gain, not our
                # last command. See sendspin_player._publish_render_state.
                role = next((r for r in client.roles_by_family("player") if isinstance(r, PlayerV1Role)), None)
                players.append(
                    PlayerState(
                        player_id=client.client_id,
                        name=client.name or client.client_id,
                        connected=True,
                        group_id=client.group.group_id if client.group is not None else None,
                        url=self.server.get_client_url(client.client_id),
                        volume=int(getattr(role, "volume", 100)) if role is not None else 100,
                        muted=bool(getattr(role, "muted", False)) if role is not None else False,
                    )
                )

        return UnitSnapshot(unit_id=self.unit_id, name=self.unit_name, host=None, sources=sources, players=players)

    def start_airplay_metadata(self, source_id: str, metadata_fifo: str) -> None:
        """Attach the shairport metadata/artwork → Sendspin roles reader to a source's group."""
        handle = self.sources.get(source_id)
        if handle is None:
            raise KeyError(f"unknown source {source_id!r}")
        if source_id in self._metadata_readers:
            return
        reader = AirplayMetadataReader(handle.group, metadata_fifo)
        self._metadata_readers[source_id] = reader
        reader.start()
        logger.info("[%s] airplay metadata reader started (%s)", source_id, metadata_fifo)

    async def start_airplay_control(self, source_id: str, *, bus_address: str | None = None) -> None:
        """Wire GUI transport commands for an AirPlay source to shairport-sync over MPRIS.

        Advertises play/pause/next/previous on the source group's controller role and forwards the
        resulting controller events to the AirPlay sender (phone/Mac) via the MPRIS remote. Each
        endpoint has its own remote bound to its own session bus (`bus_address`) — the MPRIS name is
        fixed, so instances would otherwise fight over it on the system bus.

        The remote also reports the SENDER's volume (MPRIS Volume PropertiesChanged, i.e. the slider
        on the phone) into our source-volume cache — that is a different quantity from any endpoint's
        gain, and shairport applies it to the PCM it hands us (`ignore_volume_control = "no"`).
        """
        remote = AirplayRemote(
            bus_address=bus_address,
            on_source_volume=functools.partial(self.note_source_volume, source_id),
        )
        # Do NOT connect eagerly: with a private per-endpoint bus, the source comes up BEFORE the
        # bus daemon (the manager starts us first so the FIFO exists), so the socket may not exist
        # yet. The remote connects lazily on the first command and re-resolves after a restart.
        with contextlib.suppress(Exception):
            await remote.connect()
        remote.start_volume_watch()
        self._airplay_remotes[source_id] = remote
        self._wire_transport_control(source_id, remote)
        logger.info("[%s] airplay MPRIS transport control wired (%s)", source_id, bus_address or "system bus")

    async def start_airplay_source(self, instance: airplay_config.AirplayInstance) -> None:
        """Bring up an AirPlay endpoint as a source: group + feeder + metadata reader + MPRIS control.

        The feeder waits for shairport-sync to open the instance FIFO writer, so starting eagerly is
        safe even before the daemon is running (the manager starts us first precisely so the FIFO
        exists). Metadata/artwork ride the instance's own metadata pipe; transport rides its own
        private D-Bus session.
        """
        self.start_source(instance.source_id, instance.fifo_path, name=instance.device_name)
        self.start_airplay_metadata(instance.source_id, instance.metadata_fifo)
        await self.start_airplay_control(instance.source_id, bus_address=instance.bus_address)
        logger.info("[%s] airplay source up (name=%r port=%d)", instance.source_id, instance.device_name, instance.port)

    async def stop_airplay_source(self, source_id: str) -> None:
        """Tear down an AirPlay source: metadata reader, MPRIS remote, then the source itself."""
        reader = self._metadata_readers.pop(source_id, None)
        if reader is not None:
            await reader.stop()
        remote = self._airplay_remotes.pop(source_id, None)
        if remote is not None:
            await remote.close()
        self._source_remotes.pop(source_id, None)
        self._source_volumes.pop(source_id, None)
        await self.stop_source(source_id)
        logger.info("[%s] airplay source stopped", source_id)

    async def start_spotify_source(self, instance: spotify_config.SpotifyInstance) -> None:
        """Bring up a Spotify Connect endpoint as a source: group + feeder + go-librespot events/control.

        The feeder waits for go-librespot to open the instance FIFO writer, so starting eagerly is
        safe even before go-librespot is running. SpotifyGoLibrespot serves double duty — its run()
        loop pushes metadata/artwork to the group's roles from the WebSocket event stream, and it is
        also registered as the source's transport remote (play/pause/next/previous POST the HTTP API).
        """
        self.start_source(instance.source_id, instance.fifo_path, name=instance.device_name)
        handle = self.sources[instance.source_id]
        monitor = SpotifyGoLibrespot(
            handle.group,
            instance.instance_id,
            instance.api_base,
            on_source_volume=functools.partial(self.note_source_volume, instance.source_id),
        )
        await monitor.connect()
        monitor.start()
        self._spotify_monitors[instance.source_id] = monitor
        self._wire_transport_control(instance.source_id, monitor)
        logger.info(
            "[%s] spotify source up (name=%r api=%s)", instance.source_id, instance.device_name, instance.api_base
        )

    async def stop_spotify_source(self, source_id: str) -> None:
        """Tear down a Spotify source: stop its go-librespot monitor, drop its remote, stop the source."""
        monitor = self._spotify_monitors.pop(source_id, None)
        if monitor is not None:
            await monitor.stop()
        self._source_remotes.pop(source_id, None)
        self._source_volumes.pop(source_id, None)
        await self.stop_source(source_id)
        logger.info("[%s] spotify source stopped", source_id)

    async def start_bluetooth_source(self, instance: bluetooth_config.BluetoothInstance) -> None:
        """Bring up a Bluetooth endpoint as a source: group + feeder + BlueZ adapter + AVRCP.

        The feeder waits for a writer on the FIFO, so starting eagerly is safe long before any phone
        connects — the source simply sits idle (playback_state=stopped) until one does, exactly as
        an AirPlay endpoint does between sessions. BluetoothAdapter owns the radio and spawns the
        arecord capture child on connect; BluetoothAvrcp serves double duty as the metadata pump and
        the source's transport remote.
        """
        self.start_source(instance.source_id, instance.fifo_path, name=instance.device_name)
        handle = self.sources[instance.source_id]
        adapter = BluetoothAdapter(instance)
        # Album art is best-effort and entirely separable: it needs BlueZ >= 5.81 + Experimental +
        # a phone that supports AVRCP cover art, and it talks to obexd on this endpoint's private
        # session bus. If any of that is missing it logs once and stays quiet — audio, metadata and
        # transport never depend on it.
        cover_art = BluetoothCoverArt(handle.group, instance.instance_id, instance.obex_bus_address)
        avrcp = BluetoothAvrcp(
            handle.group,
            instance.instance_id,
            adapter,
            cover_art=cover_art,
            # Repeat/shuffle support is discovered per connected player, not known up front, so the
            # advertised command set has to be republished when it changes (see bluetooth_avrcp).
            on_commands_changed=functools.partial(self.refresh_source_commands, instance.source_id),
            on_source_volume=functools.partial(self.note_source_volume, instance.source_id),
        )
        avrcp.start()  # register the BlueZ listeners BEFORE start() replays existing objects
        await adapter.start()
        self._bluetooth_adapters[instance.source_id] = adapter
        self._bluetooth_avrcp[instance.source_id] = avrcp
        self._bluetooth_coverart[instance.source_id] = cover_art
        self._wire_transport_control(instance.source_id, avrcp)
        logger.info(
            "[%s] bluetooth source up (name=%r adapter=%s)",
            instance.source_id,
            instance.device_name,
            instance.adapter,
        )

    async def stop_bluetooth_source(self, source_id: str) -> None:
        """Tear down a Bluetooth source: AVRCP, then the adapter (which kills capture), then the source."""
        avrcp = self._bluetooth_avrcp.pop(source_id, None)
        if avrcp is not None:
            await avrcp.stop()
        cover_art = self._bluetooth_coverart.pop(source_id, None)
        if cover_art is not None:
            await cover_art.stop()
        adapter = self._bluetooth_adapters.pop(source_id, None)
        if adapter is not None:
            await adapter.stop()
        self._source_remotes.pop(source_id, None)
        self._source_volumes.pop(source_id, None)
        await self.stop_source(source_id)
        logger.info("[%s] bluetooth source stopped", source_id)

    async def update_bluetooth_source(self, instance: bluetooth_config.BluetoothInstance) -> None:
        """Apply an edited endpoint to the LIVE adapter (alias, discoverable, pairable)."""
        adapter = self._bluetooth_adapters.get(instance.source_id)
        if adapter is not None:
            await adapter.apply_settings(instance)

    def refresh_source_commands(self, source_id: str) -> None:
        """Re-publish a source's supported command set on its controller role.

        Most sources know their capabilities up front, so _wire_transport_control advertises once.
        Bluetooth doesn't: whether repeat/shuffle exist depends on the phone that just connected, so
        it calls this when that answer changes.
        """
        handle = self.sources.get(source_id)
        if handle is None:
            return
        controller = handle.group.group_role("controller")
        if controller is None:
            return
        with contextlib.suppress(Exception):
            controller.set_supported_commands(self._supported_commands_for(source_id))
            remote = self._source_remotes.get(source_id)
            if remote is not None and hasattr(remote, "push_modes"):
                remote.push_modes()

    def _wire_transport_control(self, source_id: str, remote: object) -> None:
        """Advertise transport commands on a source's controller role and route its events to `remote`.

        `remote` must expose async play/pause/next_track/previous_track. The event listener is bound
        to this source_id so multiple concurrent sources (AirPlay + N Spotify) each reach their own
        sender.
        """
        handle = self.sources.get(source_id)
        if handle is None:
            raise KeyError(f"unknown source {source_id!r}")
        self._source_remotes[source_id] = remote
        controller = handle.group.group_role("controller")
        if controller is not None:
            controller.set_supported_commands(self._supported_commands_for(source_id))
            if hasattr(remote, "push_modes"):
                remote.push_modes()  # seed the controller role with the source's repeat/shuffle
        # The source's anchor group persists across stop/start, so only register the listener once —
        # events dispatch via self._source_remotes[source_id], which we repopulate above on re-add.
        if source_id not in self._wired_sources:
            handle.group.add_event_listener(functools.partial(self._on_control_event, source_id))
            self._wired_sources.add(source_id)

    def _supported_commands_for(self, source_id: str) -> list[MediaCommand]:
        """The controller commands a source advertises. Every source with a transport remote honours
        play/pause/next/previous; a remote that flags `supports_repeat_shuffle` (go-librespot) adds
        the repeat/shuffle set. Advertising only what we honour keeps a conformant controller (and our
        own GUI) from showing controls the source can't action — which is how the GUI hides
        repeat/shuffle for AirPlay."""
        remote = self._source_remotes.get(source_id)
        commands = [MediaCommand.PLAY, MediaCommand.PAUSE, MediaCommand.NEXT, MediaCommand.PREVIOUS]
        if remote is not None and getattr(remote, "supports_repeat_shuffle", False):
            commands += [
                MediaCommand.REPEAT_OFF,
                MediaCommand.REPEAT_ONE,
                MediaCommand.REPEAT_ALL,
                MediaCommand.SHUFFLE,
                MediaCommand.UNSHUFFLE,
            ]
        return commands

    def _on_control_event(self, source_id: str, _group: SendspinGroup, event: object) -> None:
        """Forward a controller transport event to the source's sender via its remote (fire-and-forget).

        For AirPlay we ALSO reflect play/pause on the metadata role immediately (apply_command): the
        source takes seconds to confirm, so this is what makes every GUI on the group flip together
        instead of one reverting to playing mid buffer-drain. The remote call is what actually drives
        the phone/Mac; the optimistic reflection is purely for consistent, instant GUI feedback."""
        remote = self._source_remotes.get(source_id)
        if remote is None:
            return
        reader = self._metadata_readers.get(source_id)  # AirPlay only; None for Spotify
        if isinstance(event, ControllerPlayEvent):
            asyncio.ensure_future(remote.play())
            if reader is not None:
                reader.apply_command("play")
        elif isinstance(event, ControllerPauseEvent):
            asyncio.ensure_future(remote.pause())
            if reader is not None:
                reader.apply_command("pause")
        elif isinstance(event, ControllerNextEvent):
            asyncio.ensure_future(remote.next_track())
        elif isinstance(event, ControllerPreviousEvent):
            asyncio.ensure_future(remote.previous_track())
        elif isinstance(event, ControllerRepeatEvent):
            # Only sources whose remote can honour it advertise repeat (so a conformant controller
            # never sends this to AirPlay); guard anyway. Drive the source, and optimistically reflect
            # the new mode to every GUI on the group — the daemon's own event stream re-confirms it.
            if hasattr(remote, "set_repeat"):
                asyncio.ensure_future(remote.set_repeat(event.mode))
                controller = _group.group_role("controller")
                if controller is not None:
                    controller.set_repeat(event.mode)
        elif isinstance(event, ControllerShuffleEvent):
            if hasattr(remote, "set_shuffle"):
                asyncio.ensure_future(remote.set_shuffle(event.shuffle))
                controller = _group.group_role("controller")
                if controller is not None:
                    controller.set_shuffle(event.shuffle)

    def register_player(self, player_id: str, player_url: str) -> None:
        """Dial this unit's player so it is a live, routable client, but leave it IDLE — not attached
        to any source group.

        Where an idle player goes is owned by the autoSwitch settings (localActivity auto-route /
        follow) and explicit GUI routing — NOT a hardcoded home source, which would auto-play that
        source at boot regardless of the setting (the very thing localActivity is meant to gate).

        We still DIAL (connect) it, not merely register the URL: the intra-server route path
        (attach_player) assumes an already-connected client, so a route onto a LOCAL source would
        otherwise fail to find the player. Connected-but-ungrouped, it reports group_id=None and
        reads as idle in the GUI until something routes it.
        """
        assert self.server is not None
        self.server.register_client_url(player_id, player_url)
        self.server.connect_to_client(
            player_url, connection_reason=ConnectionReason.PLAYBACK, retry_initial_connection=True
        )

    def attach_local_player(self, source_id: str, player_id: str, player_url: str, *, supervise: bool = True) -> None:
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
        coro = (
            self._supervise_local_player(source_id, player_id, player_url)
            if supervise
            else self._attach_local_player_once(source_id, player_id, player_url)
        )
        self._local_player_tasks.append(asyncio.ensure_future(coro))

    async def _attach_local_player_once(self, source_id: str, player_id: str, player_url: str) -> None:
        """Dial the local player and attach it to its source exactly once (mesh-owned routing).

        Does not re-dial after a later disconnect: when the player roams to a peer it sends
        GoodbyeReason.ANOTHER_SERVER and the library stops retrying this URL, so there is nothing
        to fight. Initial connection is still retried (the player may not be up yet at boot).
        """
        assert self.server is not None
        self.server.connect_to_client(
            player_url, connection_reason=ConnectionReason.PLAYBACK, retry_initial_connection=True
        )
        if not await self._await_client_connected(player_id, timeout_s=30.0):
            return
        if not await self._await_source(source_id, timeout_s=30.0):
            logger.warning("[%s] source never appeared; local player left unattached", source_id)
            return
        with contextlib.suppress(Exception):
            await self.attach_player(source_id, player_id)
            logger.info("[%s] local player %s attached (mesh-owned)", source_id, player_id)

    async def _await_source(self, source_id: str, timeout_s: float) -> bool:
        """Poll until a source exists. Sources are brought up asynchronously by their manager, so
        the home source usually lands a beat after the player connects."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            if source_id in self.sources:
                return True
            await asyncio.sleep(0.2)
        return False

    def _dial_local_player(self, player_url: str) -> None:
        assert self.server is not None
        self.server.connect_to_client(
            player_url,
            connection_reason=ConnectionReason.PLAYBACK,
            retry_initial_connection=True,
            retry_indefinitely=True,
        )

    async def _supervise_local_player(self, source_id: str, player_id: str, player_url: str) -> None:
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
    # The name the user typed in Settings wins; the env var is what an unnamed unit boots with.
    env_unit_name = os.environ.get("PLUM_UNIT_NAME", unit_identity.DEFAULT_DEVICE_NAME)
    unit_name = unit_identity.device_name(env_unit_name)
    # Single-unit glue: auto-attach our own player to this source once it comes up. Empty URL
    # disables it (Phase 2: the mesh orchestrator drives routing instead).
    home_source = os.environ.get("PLUM_LOCAL_PLAYER_SOURCE", "airplay-1")
    local_player_id = os.environ.get("PLUM_LOCAL_PLAYER_ID", f"{unit_id}-player")
    local_player_url = os.environ.get("PLUM_LOCAL_PLAYER_URL", "ws://127.0.0.1:8928/sendspin")

    mesh_enabled = os.environ.get("PLUM_MESH_ENABLED", "1") != "0"

    srv = PlumSendspinServer(unit_id, unit_name)
    await srv.start()

    # Sources are config-driven: each manager owns a Sendspin source + the daemon process(es) per
    # enabled endpoint, reconciled from settings.json every few seconds, so GUI endpoint edits
    # (add/rename/enable/disable/remove) apply live. Nothing here is started from env any more.
    managers = [AirplayManager(srv), SpotifyManager(srv), BluetoothManager(srv)]
    for manager in managers:
        manager.start()

    if local_player_url:
        if mesh_enabled:
            # Register the player so peers can reclaim it and the mesh can route it, but leave it
            # IDLE. Where it goes is owned by the autoSwitch settings (localActivity auto-route /
            # follow via the FollowReconciler) and explicit GUI routing — NOT a hardcoded home
            # source, which would auto-play regardless of the setting.
            srv.register_player(local_player_id, local_player_url)
        else:
            # Single-unit glue (mesh off): keep the player attached to the home source, self-healing
            # across restarts. The source may not exist yet (its manager is still spinning it up) —
            # attach_local_player waits for it.
            srv.attach_local_player(home_source, local_player_id, local_player_url, supervise=True)

    # Phase 2: the mesh (discovery + aggregation + routing + REST). Local playback above stands
    # on its own; the mesh layers cross-unit roaming on top. Disable with PLUM_MESH_ENABLED=0.
    mesh = None
    if mesh_enabled:
        from mesh.orchestrator import MeshOrchestrator  # local import: avoids an import cycle

        mesh = MeshOrchestrator(
            srv,
            beacon_port=int(os.environ.get("PLUM_BEACON_PORT", "8929")),
            api_port=int(os.environ.get("PLUM_MESH_API_PORT", "5001")),
            local_player_id=local_player_id,
        )
        await mesh.start()

    # Auto-route-on-connect + auto-follow ("slave" mode) — only meaningful with the mesh present.
    follow = None
    if mesh is not None:
        from mesh.follow import FollowReconciler  # local import: avoids an import cycle

        follow = FollowReconciler(
            mesh.aggregator,
            mesh.router,
            local_unit_id=unit_id,
            local_player_id=local_player_id,
            peer_provider=mesh.discovery.get_peer,
            delegate=mesh.client.delegate_route,
            unroute_delegate=mesh.client.delegate_unroute,
        )
        follow.start()

    # Follow renames from Settings without a restart: the mesh snapshot reads srv.unit_name on every
    # request, so updating it is enough for the GUI and every peer's aggregated view, and the mDNS
    # record is re-advertised under the same service instance. The Sendspin server_name handed to
    # aiosendspin at construction keeps the boot-time value until the next restart (see unit_identity).
    async def _apply_unit_name(new_name: str) -> None:
        srv.unit_name = new_name
        if mesh is not None:
            await mesh.neighbourhood.rename(new_name)

    rename_watch = asyncio.ensure_future(unit_identity.watch_device_name(_apply_unit_name, fallback=env_unit_name))

    stop = asyncio.Event()
    try:
        await stop.wait()  # run forever; supervisord manages the process lifecycle
    except asyncio.CancelledError:
        pass
    finally:
        rename_watch.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await rename_watch
        if follow is not None:
            await follow.stop()
        if mesh is not None:
            await mesh.stop()
        for manager in managers:
            await manager.stop()  # kills the source daemons before their sources go away
        await srv.stop()


if __name__ == "__main__":
    asyncio.run(main())
