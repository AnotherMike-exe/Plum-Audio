#!/usr/bin/env python3
"""
Plum-Audio — in-process Sendspin server + per-source PushStream feeders.

Phase 1: AirPlay FIFO → in-process SendspinServer → local player, end-to-end.

Grounded in the verified aiosendspin 6.0.5 API (audited signatures; the ingest/re-route/
reclaim control plane is exercised on hardware by tests/Integration/t0_sendspin_protocol.py, and the
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
    anchor persist regardless, so the SOURCE stays routable across sessions. Every attached
    PLAYER does not: "none" is a true none (docs/ROUTING-MODEL.md rule 1) — going idle detaches
    every player-role client uniformly, and nothing auto-resumes it except autoSwitch.localActivity
    (this unit's own player) or follow.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import os
import socket
import time
from collections.abc import AsyncIterator, Awaitable, Callable

import sendspin_identity
import unit_identity
from aiosendspin.models.types import GoodbyeReason, MediaCommand, has_role_family
from aiosendspin.noise import decode_token
from aiosendspin.noise.keys import psk_id_for
from aiosendspin.noise.pairing import PairingAttempt
from aiosendspin.noise.trust_store import PairMethod, StagedPairingPsk
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
from aiosendspin.server.server import (
    ClientAddedEvent,
    ClientConnectedEvent,
    ClientDisconnectedEvent,
    ClientRemovedEvent,
    ClientUpdatedEvent,
    ConnectionReason,
    SendspinServer,
)
from lifecycle import install_shutdown_handlers
from mesh.model import PlayerState, SourceState, UnitSnapshot
from sources import airplay_config, bluetooth_config, spotify_config
from sources.airplay_manager import AirplayManager
from sources.airplay_metadata import AirplayMetadataReader
from sources.airplay_remote import AirplayRemote
from sources.bluetooth_adapter import BluetoothAdapter
from sources.bluetooth_avrcp import BluetoothAvrcp
from sources.bluetooth_coverart import BluetoothCoverArt
from sources.bluetooth_manager import BluetoothManager
from sources.spotify_golibrespot import SpotifyGoLibrespot
from sources.spotify_manager import SpotifyManager
from speaker_names import SpeakerNames

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
CANCEL_POLL_S = 0.25  # how long to wait between cancels of a dial task — see _stop_dialing
# How long a pairing attempt waits for the operator to type the PIN. Generous: they may be
# walking to a speaker to read it off a display. Bounded so an abandoned dialog cannot pin a
# client in the pairing state forever, which would leave it unable to play.
PAIRING_PIN_TIMEOUT_S = 180.0


def _security_of(client) -> str | None:
    """How a connected client's transport is secured: None (CLEARTEXT), "sentinel", or "long_term".

    `connection_security` is None both when the client is disconnected AND when it is connected over
    the legacy cleartext path — the legacy branch never resolves a PSK. Only connected clients reach
    the snapshot, so within it None means cleartext, and that is exactly the distinction the GUI
    needs: a cleartext device can never be paired and must never be offered a Pair button.

    Defensive against the library moving: a missing attribute reads as None (unknown/cleartext)
    rather than raising inside the snapshot, which every peer polls.
    """
    security = getattr(client, "connection_security", None)
    category = getattr(security, "psk_category", None)
    return getattr(category, "value", None)


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
    all of this, so the source stays routable and the next session just starts a new stream — but
    every attached player is detached at the same time (docs/ROUTING-MODEL.md rule 1, "true none"):
    membership does not survive an idle source, only the source itself does.
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
        # Held across a group membership change so the pump cannot re-acquire the stream underneath
        # it — see membership_change().
        self._change_lock = asyncio.Lock()
        # Awaited after this source detaches its players on going idle. The server sets it, so a
        # feeder stays testable without one; see PlumSendspinServer.release_local_player.
        self.on_idle: Callable[[], Awaitable[None]] | None = None

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

        Measured on .100.21 on 2026-08-04: with airplay-1 streaming, unrouting and re-routing the
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

    @contextlib.asynccontextmanager
    async def membership_change(self) -> AsyncIterator[None]:
        """Stop the stream for the duration of an add/remove, then re-acquire it once.

        Without this a client joining a live group is handed the stream we are about to replace, and
        the sequence on the wire is `stream/start` -> `stream/end` -> `stream/start` inside ~110 ms:
        `add_client` runs the library's late-join (`PushStream.on_role_join`) for every role with
        audio requirements, and `refresh_stream` then stops that stream and starts another.

        Measured on unit-7204 2026-08-10 with DEBUG on, for both an Esparagus HiFi board and a
        FutureProof Homes Satellite1 (both ESPHome / sendspin-cpp 0.7.0):

            20:53:32,567  StreamStartMessage   <- add_client's late-join
            20:53:32,570  StreamEndMessage     <- 3 ms later, refresh_stream replaces the stream
            20:53:32,664  StreamStartMessage   <- the stream it actually gets

        sendspin-cpp does not survive that. The first start sets `pending_start_` and requests
        play_uri; the end arrives mid-spin-up, and its STOP is explicitly ignored while a start is
        pending. `pending_start_` is only cleared inside `play_uri()`, which is never reached, so the
        PLAY_URI sits at the head of an `xQueuePeek`ed command queue and blocks every command behind
        it. That state survives re-routing — only a power cycle clears it, which matches the rig
        exactly: the Esparagus could not be recovered by any number of unroute/reroute cycles.

        Stopping first gives: `stream/end` to the members already listening, then a membership change
        against a group with no live stream (so no late-join, no spurious start), then ONE
        `stream/start` for everyone. Existing listeners see the same end/start pair they already saw;
        the joining client sees one start instead of three messages.

        This does NOT optimise `refresh_stream` away — membership is still fixed at `start_stream()`
        and the re-acquire still happens, it just no longer straddles the change. The lock is what
        stops `_pump` seeing a stopped stream mid-change and re-acquiring one that excludes the
        client being added.
        """
        async with self._change_lock:
            was_streaming = self.ps is not None and not self.ps.is_stopped
            if was_streaming:
                with contextlib.suppress(Exception):
                    self.group.stop_stream()  # transport only: clients stay logically PLAYING
                self.ps = None
            try:
                yield
            finally:
                if was_streaming:
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
            except TimeoutError:
                # Pipe still open, but the sender has gone quiet for a long time.
                await self._go_idle(f"no audio for {SOURCE_IDLE_TIMEOUT_S:.0f}s")
                continue

            if data:
                # Serialised against membership_change so we never re-acquire a stream that would
                # exclude the client currently being added. Waiting costs a few ms; guessing costs
                # a silent endpoint. The pacing sleep below is deliberately OUTSIDE the lock.
                async with self._change_lock:
                    if self.ps is None or self.ps.is_stopped:
                        self._acquire_stream()  # first audio of a session → playback_state=playing
                        logger.info("[%s] active: sender feeding us (playback_state=playing)", self.source_id)
                    self._last_data_at = time.monotonic()
                    assert self.ps is not None
                    self.ps.prepare_audio(data, self.fmt)
                    committed = False
                    try:
                        await self.ps.commit_audio()
                        committed = True
                    except StreamStoppedError:
                        pass
                if committed:
                    # Yield until we're back under the buffer target: real-time pacing.
                    await self.ps.sleep_to_limit_buffer(TARGET_BUFFER_US)
                else:
                    # Routing/membership churn stopped our stream — re-acquire and re-push this
                    # same chunk so no audio is dropped. Under the lock again, so a membership
                    # change that is still in flight finishes first and we re-push into ITS stream.
                    logger.info("[%s] stream stopped under feeder; re-acquiring", self.source_id)
                    await asyncio.sleep(REACQUIRE_BACKOFF_S)
                    async with self._change_lock:
                        if self.ps is None or self.ps.is_stopped:
                            self._acquire_stream()
                        with contextlib.suppress(StreamStoppedError):
                            self.ps.prepare_audio(data, self.fmt)
                            await self.ps.commit_audio()

            if eof:
                await self._go_idle("FIFO writer closed (session end)")
                return

    async def _go_idle(self, why: str) -> None:
        """Announce that nothing is playing on this source, per the spec, and detach every player.

        group.stop() sets playback_state=stopped and pushes a group/update to every client (and
        freezes the metadata progress anchor). Deliberately NOT stop_stream(), which keeps clients
        logically PLAYING for a stream-to-stream handover.

        "None" is a true none (docs/ROUTING-MODEL.md rule 1): a dead source holds no players,
        uniformly — this unit's own player, a roamed peer, or an adopted foreign speaker are all
        just endpoints and none of them auto-resume. group.remove_client() drops each one into its
        own solo group (aiosendspin never leaves client.group as None) — the same state a manual
        "set to none" already produces via PlumSendspinServer.detach_player, so the GUI already
        renders it correctly. Locked against _change_lock because this is now a membership change
        like any other, and can race attach_player's own membership_change() for the same source.
        The group and its anchor persist regardless — only player membership changes — so the
        source stays routable and the next session just starts a new stream that includes whoever
        is attached at the time.

        Nothing here auto-resumes anything: only autoSwitch.localActivity (this unit's own player,
        rising-edge — follow.py:159) or follow bring a player back, and both already treat "no
        group at all" as the normal idle precondition (follow.py:287, router.py:126-140).
        """
        if self._last_data_at is None and (self.ps is None or self.ps.is_stopped):
            return  # already idle
        self._last_data_at = None
        self.ps = None
        with contextlib.suppress(Exception):
            await self.group.stop()
        async with self._change_lock:
            players = [
                client
                for client in self.group.clients
                if not client.client_id.startswith(ANCHOR_PREFIX)
                and has_role_family("player", client.negotiated_role_ids)
            ]
            for player in players:
                with contextlib.suppress(Exception):
                    await self.group.remove_client(player)
        logger.info(
            "[%s] idle: %s (announced playback_state=stopped, detached %d player(s))",
            self.source_id,
            why,
            len(players),
        )
        # Detaching the player is not the same as letting go of it: we still hold its ONE websocket,
        # which is what stops a foreign server ever claiming this speaker. Released here, after the
        # membership change, so "idle" means idle to the whole network and not just to us.
        if self.on_idle is not None:
            with contextlib.suppress(Exception):
                await self.on_idle()


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

    def __init__(self, unit_id: str, unit_name: str, port: int = SERVER_PORT, *, has_player: bool = True) -> None:
        self.unit_id = unit_id
        self.unit_name = unit_name
        self.port = port
        # Our Sendspin-level id (identity.peer_id), filled in start(). Distinct from unit_id, which
        # is the mesh's key — 9.x derives this from a keypair, so they are separate namespaces now.
        self.server_id: str | None = None
        # False on an ingest/routing-only unit: no player process, no local speaker, and nothing for
        # a peer to route audio onto. Travels in the snapshot so peers can tell "no speaker here,
        # ever" from "no speaker connected right now" — see UnitSnapshot.has_player.
        self.has_player = has_player
        # Which unit this one is slaved to, published in the snapshot so a leader (or any third
        # unit) can tell a room locked to it from a room that merely joined the same stream by hand.
        # Written by FollowReconciler, which already reads the setting every tick.
        self.follows_unit_id: str | None = None
        # This unit's stored calibration map, republished in the snapshot so any unit can match a
        # group whose members were calibrated from a different unit's GUI. Written by
        # LoudnessReconciler, which already reads settings.json every tick.
        self.calibration_export: dict = {}
        self.server: SendspinServer | None = None
        self.sources: dict[str, SourceHandle] = {}
        # What each speaker calls itself over the protocol, keyed by listener URL and persisted.
        # Attached is the only time a handshake name is observable — see speaker_names.py.
        self._speaker_names = SpeakerNames()
        self._primary_source: str | None = None  # source group that controller-only clients join
        self._local_player_tasks: list[asyncio.Task] = []
        # Volume asked for while a player was released, applied when it reconnects.
        self._pending_volume: dict[str, tuple[int, bool]] = {}
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
        # Operator-driven pairing: the last outcome per client (polled by the GUI while it waits),
        # the futures a PIN submission resolves, and the in-flight attempts.
        self._pairing: dict[str, dict] = {}
        self._pending_pins: dict[str, asyncio.Future] = {}
        self._pairing_tasks: dict[str, asyncio.Task] = {}
        self._stop_evt = asyncio.Event()

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        # 9.x: the server id is not ours to choose — it is identity.peer_id, the X25519 public key.
        # `unit_id` stays the MESH key (discovery, topology, the API); the two are now different
        # namespaces and anything joining them has to do it explicitly. See mesh/follow.py.
        identity = sendspin_identity.load_or_create(sendspin_identity.SERVER_ROLE)
        self.server = SendspinServer(
            loop=loop,
            identity=identity,
            server_name=self.unit_name,
            pairing_store=await sendspin_identity.server_pairing_store(),
            # Cleartext clients. Permanent, not transitional — sendspin-cpp has no encryption in any
            # release, so this is what keeps every ESP32 speaker and Music Assistant able to connect.
            allow_unencrypted=sendspin_identity.allow_unencrypted(),
        )
        self.server_id = identity.peer_id
        # mDNS OFF: SendspinServer always constructs AsyncZeroconf; keep it from advertising
        # (5353 collides with our Avahi). We connect players by explicit URL via the orchestrator.
        await self.server.start_server(port=self.port, advertise_addresses=[], discover_clients=False)
        # Before the player process comes up (supervisord priority 20 against our 10), so its very
        # first handshake is already a pairing one and no re-handshake is ever needed here either.
        await self.stage_shared_psk(sendspin_identity.peer_id_of(sendspin_identity.PLAYER_ROLE))
        await self._trust_own_player()
        # Join controller-only clients (the GUI's metadata/artwork/controller WS) to a source group
        # so they receive its now-playing state — the server otherwise leaves them in a solo group.
        self.server.add_event_listener(self._on_server_event)
        logger.info(
            "Sendspin server up: %s (unit %s, peer %s) :%d", self.unit_name, self.unit_id, self.server_id, self.port
        )

    async def _trust_player(self, player_id: str) -> bool:
        """Approve a player for unpaired playback on THIS server. Idempotent.

        Trust is per-server and per-peer: `unpaired_access_enabled` on the client is only half, and
        our own player being trusted says nothing about a peer's. `trust_unpaired` re-activates a
        live connection too, so calling it late still recovers a client that is already attached
        and silent — which is what makes it safe to call on the routing path rather than only at
        startup.

        Never raises: trust bookkeeping must not be able to fail a route. A False return means the
        player will connect and stay silent, which is worth a warning naming the id, because that
        symptom is otherwise indistinguishable from a wiring fault.
        """
        assert self.server is not None
        try:
            await self.server.trust_unpaired(player_id)
        except Exception:  # noqa: BLE001 - see docstring
            logger.warning("could not trust player %s; it will connect but stay silent", player_id)
            return False
        return True

    async def _trust_own_player(self) -> None:
        """Trust this unit's own player, so its player role is ACTIVATED and not merely negotiated.

        This is the *unpaired* half — the sentinel escape hatch — and it only matters while unpaired
        access is on. Real pairing is `pair_via_shared_psk`, which runs when the player connects; once a
        pairing record exists this call is redundant but harmless.

        Best-effort: a unit whose player key does not exist yet is a playerless unit (headless mode
        never mints one), and a trust store that will not open must not stop the server from serving
        foreign speakers. Both are logged rather than raised.
        """
        player_peer = sendspin_identity.peer_id_of(sendspin_identity.PLAYER_ROLE)
        if not player_peer:
            logger.info("no local player identity — playerless unit, nothing to trust")
            return
        if not sendspin_identity.unpaired_access_enabled():
            # Off is the default now: our own player is PAIRED (pair_via_shared_psk), so it does not
            # need the sentinel path, and trusting it anyway would leave an unused approval sitting
            # in the store looking like policy.
            logger.info("unpaired access off — the local player relies on its pairing record")
            return
        if await self._trust_player(player_peer):
            logger.info("trusted local player %s", player_peer)

    # -- operator-driven pairing ---------------------------------------------

    def pairing_state(self, client_id: str | None = None) -> dict:
        """What pairing has been attempted here and how it went. Read by the GUI while it waits.

        `initiate_pairing` runs the whole exchange — a PAKE round and a re-handshake — so it cannot
        be awaited inside an HTTP handler without holding the request open for as long as an
        operator takes to read a PIN off a speaker. It runs as a task instead, and this is where its
        outcome lands. The GUI starts an attempt, then polls.
        """
        if client_id is not None:
            return dict(self._pairing.get(client_id) or {"state": "idle"})
        return {cid: dict(st) for cid, st in self._pairing.items()}

    def submit_pin(self, client_id: str, pin: str) -> bool:
        """Hand the operator's PIN to a waiting attempt. False if nothing is waiting for one.

        The other half of `_pin_provider_for`: the library asks for a PIN by awaiting the provider,
        and this is what completes that await. False here means the attempt already timed out or was
        cancelled — worth telling the operator, because retyping into a dead dialog is otherwise
        indistinguishable from a wrong PIN.
        """
        future = self._pending_pins.get(client_id)
        if future is None or future.done():
            return False
        future.set_result(pin)
        return True

    def _pin_provider_for(self, client_id: str):
        """A `PinProvider` — `() -> Awaitable[str]` — resolved by `submit_pin` from the API."""

        async def provider() -> str:
            loop = asyncio.get_running_loop()
            future: asyncio.Future[str] = loop.create_future()
            self._pending_pins[client_id] = future
            self._pairing.setdefault(client_id, {})["state"] = "awaiting_pin"
            try:
                return await asyncio.wait_for(future, timeout=PAIRING_PIN_TIMEOUT_S)
            finally:
                self._pending_pins.pop(client_id, None)

        return provider

    async def pair_client(self, client_id: str, method: str, token: str | None = None) -> None:
        """Start an operator-initiated pairing attempt with a CONNECTED client.

        `token` is the device's pairing token — the `SP:`-prefixed string it shows as a QR code or
        offers to copy — and is REQUIRED for the `pairing_psk` method, which is the no-interaction
        one: there is no PIN to type, so the secret has to arrive some other way. The PIN methods
        ignore it.

        Runs as a background task for the reason in `pairing_state`. Raises only on a bad request —
        an unknown method, a missing token, or a client that is not connected — so the API can
        answer 400 for those and let everything else surface through the polled state.
        """
        assert self.server is not None
        if self.server.get_client(client_id) is None:
            raise KeyError(f"{client_id!r} is not connected; a pairing attempt needs a live connection")
        try:
            pair_method = PairMethod(method)
        except ValueError as exc:
            raise ValueError(f"unknown pairing method {method!r}") from exc

        pairing_psk = None
        if pair_method is PairMethod.PAIRING_PSK:
            if not token:
                raise ValueError("the pairing_psk method needs the device's pairing token")
            try:
                pairing_psk = decode_token(token.strip()).pairing_psk
            except ValueError as exc:
                raise ValueError(f"that does not look like a pairing token: {exc}") from exc

        attempt = PairingAttempt(
            method=pair_method,
            # Only the PIN methods consult a provider, and only pairing_psk carries a secret —
            # keeping them exclusive makes an impossible combination impossible to construct.
            pin_provider=self._pin_provider_for(client_id) if pair_method is not PairMethod.PAIRING_PSK else None,
            pairing_psk=pairing_psk,
            owner="plum-gui",
        )
        self._pairing[client_id] = {"state": "pending", "method": method}
        self._pairing_tasks[client_id] = asyncio.ensure_future(self._run_pairing(client_id, attempt))

    async def _run_pairing(self, client_id: str, attempt) -> None:
        """Drive one attempt to completion and record how it ended.

        Every failure mode lands here as a message rather than a traceback, because the operator is
        looking at a dialog, not a log: a wrong PIN, a timeout waiting for one, and a speaker that
        hung up are all things they can act on, and they need different actions.
        """
        assert self.server is not None
        try:
            await self.server.initiate_pairing(client_id, attempt)
        except TimeoutError:
            self._pairing[client_id] = {"state": "failed", "error": "timed out waiting for the PIN"}
            logger.warning("pairing %s: timed out waiting for a PIN", client_id)
        except Exception as exc:  # noqa: BLE001 - the operator needs the reason, not a 500
            self._pairing[client_id] = {"state": "failed", "error": str(exc) or type(exc).__name__}
            logger.warning("pairing %s failed: %s", client_id, exc)
        else:
            self._pairing[client_id] = {"state": "paired"}
            logger.info("pairing %s: succeeded", client_id)
        finally:
            self._pairing_tasks.pop(client_id, None)

    async def cancel_pairing(self, client_id: str) -> None:
        """Abandon an attempt without finalising, leaving the connection up."""
        assert self.server is not None
        future = self._pending_pins.pop(client_id, None)
        if future is not None and not future.done():
            future.cancel()
        with contextlib.suppress(Exception):
            await self.server.end_pairing(client_id)
        self._pairing[client_id] = {"state": "cancelled"}

    async def unpair_client(self, client_id: str) -> None:
        """Drop the pairing record both ends hold. The client is told, and closes."""
        assert self.server is not None
        await self.server.unpair(client_id)
        self._pairing.pop(client_id, None)
        logger.info("unpaired %s", client_id)

    async def _ensure_client_connected(self, client_id: str, timeout_s: float = 10.0) -> bool:
        """Dial a client we hold a registered URL for, if it is not already connected. Idempotent.

        The counterpart to `release_local_player`: once a player is only connected when something
        wants it, every caller that needs a LIVE connection has to be able to ask for one. Returns
        False rather than raising — a caller that cannot get its player should say so in its own
        terms, not surface a dial failure.
        """
        if self.server is None or not client_id:
            return False
        client = self.server.get_client(client_id)
        if client is not None and client.is_connected:
            return True
        url = self.server.get_client_url(client_id)
        if not url:
            return False
        self.server.connect_to_client(url, connection_reason=ConnectionReason.PLAYBACK, retry_initial_connection=False)
        return await self._await_client_connected(client_id, timeout_s)

    async def release_local_player(self) -> None:
        """Let go of this unit's own player when it is attached to no source.

        Detaching a player from a group is not the same as releasing it: we still hold the ONE
        websocket a client allows, and `SendspinClient._should_admit_connection` keeps an incumbent
        that outranks the newcomer. So while we held it, a foreign server could never take this
        speaker — Music Assistant registered both units and immediately marked them
        `available=False`, because its dial was admitted and then dropped.

        Releasing costs nothing we want: routing, follow and `autoSwitch.localActivity` all reach an
        unattached player through `mesh.router`'s idle-speaker fallback, which dials it back in the
        same breath. Local intent wins the speaker back; idleness hands it to whoever wants it.

        Never raises and never releases a player that is playing: it is called from a source going
        idle, and another source on this unit may still hold it.
        """
        if self.server is None:
            return
        player_id = sendspin_identity.peer_id_of(sendspin_identity.PLAYER_ROLE)
        if not player_id:
            return
        client = self.server.get_client(player_id)
        if client is None or not client.is_connected:
            return
        # Attached to a real source group? Then it is in use — some OTHER source is feeding it.
        for handle in self.sources.values():
            if any(c.client_id == player_id for c in handle.group.clients):
                return
        url = self.server.get_client_url(player_id)
        if not url:
            return
        # Both suppressed: this runs on the idle path, where a bookkeeping failure must not
        # propagate into the audio loop. Stopping the dial FIRST matters — a live retry loop would
        # reconnect us straight back in and undo the release.
        with contextlib.suppress(Exception):
            await self._stop_dialing(url)
        with contextlib.suppress(Exception):
            self.server.disconnect_from_client(url)
        logger.info("released the local player %s — idle, and now claimable by any server", player_id)

    async def open_pairing_window(self, client_id: str) -> bool:
        """Open a pairing window on a client we are ALREADY PAIRED WITH, over the management role.

        This is the protocol's own answer to multi-server deployments, and the reason a unit pairs
        with its own player at startup: that record is what earns us `management` on it, and thus
        the right to stand in for the physical gesture. So a unit can open its own speaker up for a
        NEW unit to pair with, without anyone touching the hardware.

        Only ever called for our own player — management requires a long-term record, so it would
        fail for anything we have not paired with anyway, but the caller should not rely on that.

        **Management is a SESSION, and leaving it open makes the player unroamable.** A declared
        activity is part of what the client's arbitration ranks when a second server dials it, so a
        server still holding `management` outranks a peer asking for plain PLAYBACK: the peer's dial
        is accepted provisionally, handshakes, and is then rejected — it lands in the peer's registry
        as `(disconnected)` and its reclaim polls for a client that never comes up. Since nothing
        expired the session, that player could not be roamed for the rest of the process's lifetime,
        and a restart "fixed" it. Measured on `.7.122` 2026-08-13: roam worked 12/12, one
        `pairing-window` call, then failed on the very next attempt and every one after.

        So it is enabled for exactly the length of the call and always disabled again. The window
        itself is client-side state with its own 300 s deadline — it long outlives this session, and
        does not need us to hold management to stay open.
        """
        assert self.server is not None
        # A released player has no connection to manage, and `management` is a property of a live
        # one. Without this the window silently fails on exactly the units most likely to need it —
        # an idle unit being commissioned. Verified both ways on .7.122: ok:false while the player
        # was elsewhere, ok:true once it was back.
        await self._ensure_client_connected(client_id)
        try:
            connection = self.server.enable_management(client_id)
            try:
                result = await connection.open_pairing_window()
            finally:
                self.server.disable_management(client_id)
        except Exception as exc:  # noqa: BLE001 - a closed window is not worth a 500
            logger.warning("could not open a pairing window on %s: %s", client_id, exc)
            return False
        ok = getattr(result, "value", str(result)) == "ok"
        logger.info("pairing window on %s: %s", client_id, "open" if ok else result)
        return ok

    async def set_unpaired_access(self, enabled: bool) -> None:
        """Apply the unpaired-access policy to every peer we have trusted.

        The client half rides in `client/hello` and so is fixed for a connection's life, but the
        SERVER half is live: `trust_unpaired`/`untrust_unpaired` both re-activate a connected
        client's roles immediately. Without this, turning the setting off in the GUI would leave
        every already-trusted peer playing until its next reconnect — a policy change that appears
        to have applied and has not.
        """
        assert self.server is not None
        if enabled:
            return  # trust is granted per-peer on the routing path; nothing to grant up front
        for client in list(self.server.clients):
            with contextlib.suppress(Exception):
                await self.server.untrust_unpaired(client.client_id)
        logger.info("unpaired access off — revoked every sentinel-PSK approval")

    async def stage_shared_psk(self, client_id: str) -> bool:
        """Pre-authorise `client_id` to pair with us IN ITS NEXT HANDSHAKE, over the PSK we share.

        This is the library's own fleet-provisioning primitive — `StagedPairingPsk`, "an
        operator-staged Pairing PSK awaiting a client" — and it is strictly better than pairing a
        client after it connects, because of where the work lands. `_psk_provider` consults the
        staged PSK while choosing the handshake PSK, so the connection comes up already in
        `PskCategory.PAIRING` and `client/pair-finalize` follows immediately. Nothing is
        renegotiated.

        `initiate_pairing` on an already-connected client cannot do that. Its PSK is the sentinel,
        so `_rehandshake_for_pairing_if_needed` tears the Noise session down and rebuilds it
        mid-connection, redoing the hellos. That works in a quiet lab and loses the race on a real
        mesh: a peer's player is contended — its own server is dialling it too, and it may hold only
        ONE websocket — so the re-handshake finds the socket gone. Measured on .7.122 taking .7.204's
        player, both by roam and by adopt:

            could not pair player G2UChhEv…: expected Noise message 2 (TEXT), got CLOSE
            [airplay-1] reclaim of remote player G2UChhEv… timed out          (then, forever)

        Staged only for clients we ALREADY share a secret with — our own player, and a peer's player
        whose id came out of a mesh snapshot. Never for a speaker off the neighbourhood: staging is
        what turns the next handshake into a pairing one, and a pairing handshake against a cleartext
        client is aborted outright by the library ("pairing requires an encrypted connection"), which
        would take every ESP32 offline. Their ids are unknown here, so they resolve to the sentinel
        and are untouched — but the rule is the reason, not the accident.

        Never raises, and idempotent: an existing record or an existing staging is success.
        """
        assert self.server is not None
        if not client_id:
            return False
        store = self.server.pairing_store
        try:
            # Inside the try: this reads (and on first use mints) a file under /config, and staging
            # sits on the routing path — a store we cannot read must cost us a pairing, not a route.
            psk = sendspin_identity.local_pairing_psk()
            if psk is None:
                return False
            if await store.record_by_client_id(client_id) is not None:
                return True  # already paired for real; staging would be noise
            if await store.staged_pairing_psk(client_id) is not None:
                return True
            await store.stage_pairing_psk(client_id, StagedPairingPsk(psk_id_for(psk), psk))
        except Exception as exc:  # noqa: BLE001 - see docstring
            logger.warning("could not stage the shared PSK for %s: %s", client_id, exc)
            return False
        logger.info("staged the shared PSK for %s — it pairs on its next handshake", client_id)
        return True

    async def pair_via_shared_psk(self, client_id: str) -> bool:
        """Pair with a connected player over a Pairing PSK we already share. Idempotent, no operator.

        Runs when the player connects, not at start(): pairing needs a live connection, and the
        local player process comes up after us (supervisord priority 20 against our 10).

        The secret is `sendspin_identity.local_pairing_psk()` — either this unit's own per-unit
        value, for our OWN player (two processes on one device behind one `/config`, where making an
        operator pair a unit with itself would be pure ceremony), or the FLEET PSK when one is
        configured, which every unit accepts.

        What it buys beyond working audio is a real long-term record at trust level `user`. That is
        what grants this server the `management` activity on that client, and management is what lets
        us open its pairing window remotely later. Without it the provisioning window has nothing to
        stand on.

        Never raises. A unit whose pairing fails still serves cleartext clients perfectly well, and
        it retries on the next connection.
        """
        assert self.server is not None
        client = self.server.get_client(client_id)
        if client is not None and getattr(client, "is_paired", False):
            return True  # already has a long-term record
        try:
            await self.server.initiate_pairing(
                client_id,
                PairingAttempt(
                    method=PairMethod.PAIRING_PSK,
                    pairing_psk=sendspin_identity.local_pairing_psk(),
                    owner="plum-local",
                ),
            )
        except Exception as exc:  # noqa: BLE001 - see docstring
            logger.warning("could not pair player %s: %s", client_id, exc)
            return False
        logger.info("paired player %s via the shared PSK", client_id)
        return True

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
        feeder.on_idle = self.release_local_player
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
        if self._primary_source == source_id:
            # Hand the fallback on rather than leaving a dead source id behind: _maybe_group_controller
            # resolves it through self.sources, so a stale id makes every controller WITHOUT a
            # "ctrl:<source>:" hint silently stop being grouped — no error, just a GUI that never
            # sees a source. Insertion order gives the oldest survivor, which is what "first source"
            # meant when it was set.
            self._primary_source = next(iter(self.sources), None)
            logger.info(
                "[%s] was the primary source; controller fallback is now %s",
                source_id,
                self._primary_source or "(none — no sources left)",
            )
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
        # A player we are routing is a player that must not be evicted from the registry thirty
        # seconds from now by a timer some earlier teardown armed. See _cancel_pending_cleanup.
        self._cancel_pending_cleanup(player_id)
        if player.group is handle.group:
            return  # already on this source — idempotent, avoids a redundant re-group
        # The stream is stopped for the duration of the move and re-acquired once afterwards, so the
        # joining player is never handed the outgoing stream. add_client alone does not put an
        # already-connected player into a running stream either way — membership is fixed at
        # start_stream() — so the re-acquire is still what makes it audible.
        async with handle.feeder.membership_change():
            if player.group is not None:
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
            # Setting a player to "none" must release it, exactly as a source going idle does —
            # otherwise "idle" depends on HOW it got there, and an unrouted speaker stays invisible
            # to every other server while looking idle to us. release_local_player is a no-op for
            # anything that is not our own player, or that another source still holds.
            await self.release_local_player()

    def _on_server_event(self, _server: SendspinServer, event: object) -> None:
        """React to client lifecycle events. A controller-only client (the GUI's now-playing WS)
        connects into its own solo group by default, where it sees no source metadata — join it to
        the primary source group so its metadata/artwork/playback-state roles receive live state.

        This is also where our own player gets paired, because pairing needs a live connection and
        the player process starts after us."""
        # Client lifecycle is otherwise INVISIBLE: the library logs nothing when a client connects or
        # goes away, so a roam that dies between "the player left its old server" and "the new server
        # has it" leaves a hole in the record with a timeout at the end and nothing before it. That is
        # exactly the shape of the .122 -> .204 failure, and it cost hours of guessing. One line each.
        if isinstance(event, (ClientAddedEvent, ClientConnectedEvent)):
            client = self.server.get_client(event.client_id) if self.server else None
            logger.info(
                "client %s %s (security=%s paired=%s roles=%s)",
                event.client_id,
                "added" if isinstance(event, ClientAddedEvent) else "connected",
                _security_of(client) if client else "?",
                getattr(client, "is_paired", "?"),
                ",".join(getattr(client, "active_role_ids", []) or []) or "-",
            )
        elif isinstance(event, (ClientRemovedEvent, ClientDisconnectedEvent)):
            logger.info(
                "client %s %s", event.client_id, "removed" if isinstance(event, ClientRemovedEvent) else "disconnected"
            )
        if isinstance(event, (ClientAddedEvent, ClientUpdatedEvent)):
            asyncio.ensure_future(self._maybe_group_controller(event.client_id))
            asyncio.ensure_future(self._maybe_pair_via_shared_psk(event.client_id))
            asyncio.ensure_future(self._apply_pending_volume(event.client_id))

    async def _apply_pending_volume(self, client_id: str) -> None:
        """Send a level that was set while this player was released. Once, on reconnect."""
        pending = self._pending_volume.pop(client_id, None)
        if pending is None:
            return
        volume, muted = pending
        with contextlib.suppress(Exception):
            self.set_player_volume(client_id, volume, muted)
            logger.info("applied held volume %d%% muted=%s to %s", volume, muted, client_id)

    async def _maybe_pair_via_shared_psk(self, client_id: str) -> None:
        """Pair a connecting client automatically where we can do so without an operator.

        Two cases, and they are the same mechanism:

        **Our own player** — matched on the peer id read off our own `/config/identity`, never on a
        name or URL, so it cannot fire for a peer's player roaming here.

        **And only ENCRYPTED ones.** Pairing mixes a PSK into a Noise handshake, so there is nothing
        to pair over a legacy cleartext connection and `initiate_pairing` refuses it outright. Every
        ESP32 speaker on the segment is cleartext, so without this gate each one that connects earns
        a pairing attempt that can only fail — and on the rig that broke `adopt_foreign_client`
        outright: the speaker connected, the doomed pairing ran against it, and the adopt's 15 s wait
        then expired reporting "never connected" about a device whose MAC we had just logged. The
        following adopt of the same speaker succeeded, because by then it was already connected. That
        off-by-one is the signature. Measured on .7.122 against three boards, 2026-08-13.

        **A PEER's player is deliberately NOT handled here.** It used to be, on the same fleet PSK,
        and it was the wrong place: pairing a client that is already connected on the sentinel PSK
        forces a mid-connection re-handshake, and a peer's player is contended, so the re-handshake
        loses the race and takes the connection down with it. Peers are staged instead — see
        `stage_shared_psk`, called before the dial in `reclaim_remote_player` — so they pair inside
        the handshake and never renegotiate.
        """
        own = sendspin_identity.peer_id_of(sendspin_identity.PLAYER_ROLE)
        if not own or client_id != own:
            return
        client = self.server.get_client(client_id) if self.server else None
        if client is not None and _security_of(client) is None:
            return  # cleartext: activated straight from its negotiated roles, nothing to pair
        # Normally a no-op: start() stages this same PSK before the player process comes up, so it
        # arrives already paired. Kept for a unit whose store predates staging.
        await self.pair_via_shared_psk(client_id)

    async def _maybe_group_controller(self, client_id: str) -> None:
        """Join a controller-only client to the source group it asked for, or the best active one.

        A client holds exactly one websocket and therefore sits in exactly one group, so a single
        controller can only ever see ONE source's now-playing. The GUI opens one controller per
        source and names it "ctrl:<source_id>:<nonce>"; we honour that request here, even for an
        idle source — an explicit ask gets what it asked for. Without the hint (any other client
        id — i.e. a third-party Sendspin controller, since only our own GUI ever sends one) it
        falls back through _default_controller_source rather than blindly grouping into whatever
        _primary_source is: that used to hand a foreign controller a group with nothing playing and
        nothing in the protocol to tell it apart from a live one.
        """
        if self.server is None or self._primary_source is None:
            return
        if client_id.startswith(ANCHOR_PREFIX):
            return  # anchors are group scaffolding, not clients to regroup
        client = self.server.get_client(client_id)
        if client is None or not client.is_connected:
            return
        if has_role_family("player", client.negotiated_role_ids):
            return  # a player — the mesh orchestrator owns its routing, never regroup it here
        source_id = self._requested_source(client_id)
        if source_id is None:
            source_id = self._default_controller_source()
        if source_id is None:
            logger.debug("controller %s connected with nothing active; leaving it ungrouped", client_id)
            return
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

    def _default_controller_source(self) -> str | None:
        """Fallback source for a controller with no "ctrl:<source_id>:" hint.

        Only reached by a THIRD-PARTY Sendspin controller — our own GUI always sends the hint (see
        _requested_source), so an unhinted client here is someone else's controller (e.g. Music
        Assistant) with no way to name a source. Defaulting it into an idle source used to hand a
        foreign controller a group with nothing playing and nothing to tell it apart from a live
        one — prefer the primary source while it is actually active, else the first active source,
        else leave the client in its own solo group.
        """
        if self._primary_source is not None:
            primary = self.sources.get(self._primary_source)
            if primary is not None and primary.feeder.is_active:
                return self._primary_source
        return next((source_id for source_id, handle in self.sources.items() if handle.feeder.is_active), None)

    def set_player_volume(self, player_id: str, volume: int, muted: bool) -> None:
        """Set one player's volume (0-100) and mute — per-client, independent of its group.

        Drives the player's Sendspin volume/mute role commands; the player applies them as
        render-side gain (AlsaRenderer). Per-client so concurrent groups (and members within a
        group) each hold their own level.
        """
        assert self.server is not None
        client = self.server.get_client(player_id)
        if client is None or not client.is_connected:
            # A released player is idle, not gone. Dialling it just to move a slider would yank the
            # speaker back from whatever foreign server is playing to it — a nudge should never
            # steal a room mid-track. Remember the level and send it when it next connects.
            self._pending_volume[player_id] = (max(0, min(100, volume)), muted)
            logger.info("player %s not connected; volume %d%% muted=%s held until it does", player_id, volume, muted)
            return
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
        # A peer's player is a client we already share a secret with (the fleet PSK), it just has no
        # record here yet. Stage it BEFORE the dial so the reclaim's own handshake pairs it — doing
        # it after it lands would need a re-handshake, which loses the race against the server it is
        # roaming away from. `player_id` came from a peer snapshot, so this only ever names a Plum
        # player; see stage_shared_psk for why that restriction is load-bearing.
        await self.stage_shared_psk(player_id)
        # A PEER's player is an encrypted-but-unpaired client of ours, and trust is per-server:
        # trusting our own player at startup says nothing about anyone else's. Without this the
        # roam completes — the player detaches from its old server, reconnects here, joins the
        # group at the right volume — and is activated for NO roles, so the room goes silent with
        # nothing in either log. Trust-on-deploy, applied at the moment we decide to take it.
        await self._trust_player(player_id)
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
        # Already dialled, connected and answering? Then there is nothing to dial. Re-routing it is
        # attach_player's job, and attach_player is idempotent when the group is already right.
        # Redialing here instead would drop a working speaker mid-track for no reason — see
        # _stop_dialing for why a redial is never free.
        existing = self._connected_player_at(url)
        if existing is not None:
            await self.attach_player(source_id, existing)
            logger.info("[%s] foreign speaker %s (%s) already connected; kept the dial", source_id, existing, url)
            return True
        # Otherwise the registration is stale, and a stale one must be torn down before we redial:
        # connect_to_client is a NO-OP while a dial task for the URL exists, so without this a second
        # adopt does nothing and then times out blaming the speaker — "never connected" about a
        # device whose port is plainly open. Measured on .100.21 against a Home Assistant Voice PE,
        # seconds apart on the same URL: adopt alone -> ok:false; release (whose only extra step is
        # this teardown) then adopt -> ok:true.
        await self._stop_dialing(url)
        self.server.connect_to_client(url, connection_reason=ConnectionReason.PLAYBACK, retry_initial_connection=True)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            for client in self.server.clients:
                if not client.is_connected:
                    continue
                if not has_role_family("player", client.negotiated_role_ids):
                    continue  # not a render endpoint (a controller connecting for its own reasons)
                # Identify the speaker we just dialled. The URL is the reliable test and has to come
                # first: a speaker adopted ONCE stays in self.server.clients, so on every later
                # adopt it is already in `before`, and its handshake id (a MAC, for a Home Assistant
                # Voice PE) is not the mDNS name the GUI passes as player_id — so the other two
                # tests both miss and the adopt fails with "never connected" while the speaker is
                # sitting there connected. Measured on .100.21: adopted twice, then never again.
                registered = self.server.get_client_url(client.client_id)
                if registered == url or client.client_id == player_id or client.client_id not in before:
                    self.server.register_client_url(client.client_id, url)
                    await self.attach_player(source_id, client.client_id)  # defuses the eviction timer
                    logger.info("[%s] adopted foreign speaker %s (%s)", source_id, client.client_id, url)
                    return True
            await asyncio.sleep(0.1)
        logger.warning("[%s] foreign speaker at %s never connected", source_id, url)
        return False

    def _connected_player_at(self, url: str) -> str | None:
        """The client id of the render endpoint we already hold a live connection to at `url`.

        The registered URL is the only reliable identity: a speaker adopted once keeps its entry in
        server.clients forever, its handshake id is a MAC where mDNS named it by instance, and both
        differ from whatever the GUI passed as player_id. Controllers are skipped — only something
        that negotiated a player role is a speaker.
        """
        assert self.server is not None
        for client in self.server.clients:
            if not client.is_connected:
                continue
            if not has_role_family("player", client.negotiated_role_ids):
                continue
            if self.server.get_client_url(client.client_id) == url:
                return client.client_id
        return None

    def _cancel_pending_cleanup(self, client_id: str) -> None:
        """Defuse the library's registry-eviction timer for a client we are about to keep or drop.

        `SendspinClient._schedule_cleanup` assigns `_cleanup_handle` WITHOUT cancelling whatever was
        already there, so scheduling twice orphans the first timer: nothing holds a reference to it,
        so `attach_connection`'s "cancel pending cleanup on reconnect" can never reach it, and it
        fires anyway. `_do_cleanup` then calls `remove_client(self._client_id)` — which evicts
        whichever client currently holds that id, not the object the timer belonged to.

        Two schedules is the normal shape of a release: tearing the connection down has no goodbye
        reason, which arms a 30 s DELAYED cleanup, and the `USER_REQUEST` goodbye ~250 ms later arms
        an IMMEDIATE one on top of it. Measured on unit-7204 2026-08-10 with DEBUG on:

            20:53:32,209  Scheduling delayed cleanup in 30s (reason: None)
            20:53:32,507  Received client/hello          <- rerouted, reconnected
            20:53:32,571  attached player 98:A3:...      <- playing
            20:54:02,210  Cleaning up client from registry
            20:54:02,211  removing 98:A3:... from group  <- evicted mid-playback, 30 s after the UNROUTE

        That is the "reroute it, it plays for a few seconds, then drops back to idle" failure: the
        countdown starts when the speaker is unrouted and expires while it is happily streaming
        again. Across that session the library cancelled 3 pending cleanups and ran 13.

        So we cancel the handle ourselves at both points where we would otherwise leave one armed:
        before adding a second schedule (release), and once an adopt has confirmed the client is
        connected — a connected client must never have an eviction pending. Best-effort by design:
        this reaches into a private attribute, and a version that no longer has it should not break
        routing. See UPSTREAM §5.
        """
        if self.server is None:
            return
        client = self.server.get_client(client_id)
        handle = getattr(client, "_cleanup_handle", None)
        if handle is None:
            return
        with contextlib.suppress(Exception):
            handle.cancel()
            client._cleanup_handle = None  # noqa: SLF001
            logger.debug("cancelled a pending registry cleanup for %s", client_id)

    async def _stop_dialing(self, url: str, timeout_s: float = 2.0) -> bool:
        """Tear down a server-initiated dial and WAIT until the task is actually gone.

        `disconnect_from_client()` alone does not stop it. It cancels the dial task, but the task
        survives its own cancellation: `SendspinConnection._handle_client` awaits the message loop
        as a SEPARATE task, and `_run_message_loop` catches CancelledError and returns normally, so
        the cancel is consumed. The dialer sees a clean session end, backs off ~1 s and redials —
        and because a session that lasted 10 s resets the backoff, a real speaker keeps it alive
        forever. Worse, the doomed task's `finally` pops `_connection_tasks[url]` AFTER the caller's
        next `connect_to_client` has already put its own task there, so the new dial is unregistered
        and the adopt after that opens a THIRD dialer, and so on.

        Measured against 6.0.5 with a fake speaker counting sockets: six disconnect+reconnect pairs
        left six concurrent websockets to one speaker and six live dial tasks, with the server's
        registry claiming one. A Sendspin client holds exactly ONE websocket, so those dialers fight
        over it — audio starts, plays a few seconds, and dies with close_code=1006, repeatedly. That
        is the "route it to a foreign speaker and it drops back to idle" failure of 2026-08-10.

        So: cancel until it is genuinely done. The swallow only happens inside the message loop; a
        cancel that lands during connect or during the backoff sleep propagates normally, which is
        why re-cancelling terminates. Returns False if it outlived the timeout anyway — the caller
        still redials, because a stale registration that never dies is worse than a duplicate.
        """
        assert self.server is not None
        task = getattr(self.server, "_connection_tasks", {}).get(url)
        with contextlib.suppress(Exception):
            self.server.disconnect_from_client(url)  # public half: options, reason, registry entry
        if task is None:
            return True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while not task.done() and loop.time() < deadline:
            task.cancel()
            with contextlib.suppress(Exception):
                await asyncio.wait({task}, timeout=CANCEL_POLL_S)
        if not task.done():
            logger.warning("dial task for %s outlived %.1fs of cancellation; redialing anyway", url, timeout_s)
            return False
        return True

    async def release_foreign_client(self, source_id: str, player_id: str, url: str | None = None) -> None:
        """Hand a foreign speaker back: leave the group, cancel our dial, and drop the connection.

        Three steps, all needed. detach leaves our group; stopping the dial is what stops us taking
        it straight back, which by itself would leave the speaker sitting connected to us and
        unavailable to its own server; remove_client is what actually closes it out and forgets it.
        Verified on a real third-party speaker — without the last step it stayed connected and Music
        Assistant could not take it back.

        The dial teardown goes through `_stop_dialing`, not `disconnect_from_client`: the latter
        does not actually stop the dialer, so a release would hand the speaker back and then redial
        it about a second later, which reads as a release that silently did nothing.
        """
        await self.detach_player(source_id, player_id)
        if self.server is not None:
            target = url or self.server.get_client_url(player_id)
            if target:
                await self._stop_dialing(target)  # stop OUR dial, so we don't re-take it
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
                # Both steps above schedule a registry cleanup, and the second ORPHANS the first
                # rather than replacing it — an unreachable 30 s timer that later evicts whatever
                # client holds this id, including a re-routed speaker mid-playback. Defuse the
                # pending one so the USER_REQUEST goodbye leaves exactly one. See _cancel_pending_cleanup.
                self._cancel_pending_cleanup(player_id)
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
        # Name who we DO hold. A reclaim that times out is either "the player never arrived" or "it
        # arrived under an id we did not look up", and those need opposite fixes — this is the line
        # that tells them apart, and its absence is why the .122 -> .204 failure stayed opaque.
        logger.warning(
            "waited %.0fs for player %s; clients held: %s",
            timeout_s,
            player_id,
            ", ".join(f"{c.client_id}{'' if c.is_connected else '(disconnected)'}" for c in self.server.clients)
            or "none",
        )
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
                if not c.client_id.startswith(ANCHOR_PREFIX) and has_role_family("player", c.negotiated_role_ids)
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
                if not has_role_family("player", client.negotiated_role_ids):
                    continue  # controller/display clients (the GUI WS) are grouped for metadata, not players
                # The level the PLAYER reported (client/state), which is what the role object holds —
                # set_volume() alone does not move it, so this is the endpoint's real gain, not our
                # last command. See sendspin_player._publish_render_state.
                role = next((r for r in client.roles_by_family("player") if isinstance(r, PlayerV1Role)), None)
                client_url = self.server.get_client_url(client.client_id)
                # Remember what this speaker calls itself, against its listener URL. Attached is the
                # ONLY time a handshake name is visible, and mDNS gives a third-party device's bare
                # instance name once it goes idle — so without this the GUI has nothing to show but
                # "home-assistant-voice-a1b2c3". `learn` is a no-op unless the pair actually changed.
                self._speaker_names.learn(client_url, client.name)
                players.append(
                    PlayerState(
                        player_id=client.client_id,
                        name=client.name or client.client_id,
                        connected=True,
                        group_id=client.group.group_id if client.group is not None else None,
                        url=client_url,
                        volume=int(getattr(role, "volume", 100)) if role is not None else 100,
                        muted=bool(getattr(role, "muted", False)) if role is not None else False,
                        # What the server has ACTIVATED, not what this client negotiated. Under 9.x
                        # they diverge for an encrypted-but-unpaired client, which is admitted,
                        # grouped, and silent — publishing it is what makes that visible outside the
                        # audio process (the mesh API, the GUI, deploy.sh's verify).
                        active_roles=sorted(client.active_role_ids),
                        # None for a CLEARTEXT connection, which is how the GUI tells "needs
                        # pairing" from "never will" — see PlayerState.security.
                        security=_security_of(client),
                        paired=bool(client.is_paired),
                    )
                )

        return UnitSnapshot(
            unit_id=self.unit_id,
            name=self.unit_name,
            host=None,
            sources=sources,
            players=players,
            has_player=self.has_player,
            hostname=socket.gethostname(),
            # Our Sendspin id (an X25519 pubkey under 9.x), so peers can map the server a roamed
            # player reports itself attached to back onto a unit. Without this `follow` cannot tell
            # one of our servers from Music Assistant. None until start() has run.
            server_id=self.server_id,
            follows_unit_id=self.follows_unit_id,
            calibration=self.calibration_export,
        )

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

    def _note_airplay_playback_state(self, source_id: str, playing: bool) -> None:
        """Route an MPRIS play/pause observation to that source's metadata reader, if it has one."""
        reader = self._metadata_readers.get(source_id)
        if reader is not None:
            reader.note_external_state(playing)

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
            # shairport's MPRIS PlaybackStatus as the play/pause FALLBACK. Senders that emit the
            # ssnc state codes drive the reader directly and this simply agrees with them; senders
            # that do not (Music Assistant's AirPlay) would otherwise never report a state at all.
            on_playback_state=functools.partial(self._note_airplay_playback_state, source_id),
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
        """Register this unit's player as routable, WITHOUT holding a connection to it.

        Where an idle player goes is owned by the autoSwitch settings (localActivity auto-route /
        follow) and explicit GUI routing — NOT a hardcoded home source, which would auto-play that
        source at boot regardless of the setting (the very thing localActivity is meant to gate).

        **Registered, not dialled** — and the difference is the whole of third-party interop. A
        client holds exactly ONE websocket, and `SendspinClient._should_admit_connection` keeps the
        incumbent whenever it outranks the newcomer. So a resident PLAYBACK-ranked connection to our
        own player means a foreign server's discovery dial is admitted just long enough to register
        the speaker and is then dropped: Music Assistant listed both units and marked them
        `available=False`, which is not a state anyone can play out of. Measured 2026-08-13.

        Nothing needs the dial. The intra-server route path used to assume a connected client, but a
        player attached to nothing appears in no unit's `players` list, so `mesh.router` already
        takes the idle-speaker fallback (`_idle_player_url` -> reclaim), which dials. Routing, follow
        and `autoSwitch.localActivity` therefore all reclaim it in ~30 ms when they want it — the
        deliberate choice being that local intent always wins a speaker back.
        """
        assert self.server is not None
        self.server.register_client_url(player_id, player_url)

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


def local_player_config(env: dict) -> str | None:
    """This unit's own speaker's listener URL, or None if it has none.

    Extracted from main() because it is the hinge of headless mode and main() cannot be unit-tested.
    A unit with no audio output has no player process at all (output_gate.py decided that before
    supervisord started), so there is nothing to register, nothing to dial, and nothing for a peer to
    route audio onto.

    PLUM_PLAYER_ENABLED is the container-level answer, written by deploy.sh from the units.conf row,
    and the entrypoint sets it from the gate. The empty-URL check stays as a second way to say the
    same thing — it predates the flag and the dev rig still uses it.

    **This used to also return the player's ID, derived from PLUM_LOCAL_PLAYER_ID or `<unit>-player`.**
    Under aiosendspin 9.x a client id is the peer's X25519 public key, so it is no longer ours to
    name: main() reads it from the persisted keypair instead. What is left here is purely "is there a
    player, and where does it listen" — which is what every test on this function was really about.
    """
    if env.get("PLUM_PLAYER_ENABLED", "1") == "0":
        return None
    return env.get("PLUM_LOCAL_PLAYER_URL", "ws://127.0.0.1:8928/sendspin") or None


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
    local_player_url = local_player_config(os.environ)
    # The player's Sendspin id is its PUBLIC KEY under 9.x, so the server derives it from the stored
    # keypair rather than naming it. Minting here (not just reading) is deliberate: the server has
    # the lower supervisord priority and therefore starts first, so it is what creates both
    # identities on a fresh unit — and it needs the player's id before the player has ever connected,
    # to register its URL and to trust it. Both processes share /config inside one container.
    local_player_id = (
        sendspin_identity.load_or_create(sendspin_identity.PLAYER_ROLE).peer_id if local_player_url else None
    )

    mesh_enabled = os.environ.get("PLUM_MESH_ENABLED", "1") != "0"

    srv = PlumSendspinServer(unit_id, unit_name, has_player=local_player_id is not None)
    await srv.start()

    # Sources are config-driven: each manager owns a Sendspin source + the daemon process(es) per
    # enabled endpoint, reconciled from settings.json every few seconds, so GUI endpoint edits
    # (add/rename/enable/disable/remove) apply live. Nothing here is started from env any more.
    managers = [AirplayManager(srv), SpotifyManager(srv), BluetoothManager(srv)]
    for manager in managers:
        manager.start()

    if local_player_id is None:
        # Ingest/routing only. Skipping register_player matters beyond tidiness: it dials with
        # retry_initial_connection=True, so on a unit with no player that is a permanent reconnect
        # loop against a port nothing is listening on.
        logger.info("no local player on this unit — ingesting and routing only")
    elif local_player_url:
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
            local_player_url=local_player_url,
        )
        await mesh.start()

    # Auto-route-on-connect + auto-follow ("slave" mode) — only meaningful with the mesh present, and
    # only on a unit that HAS a speaker. Both paths end in routing local_player_id; with no player
    # that is a RouteError per source activation (localActivity) or, in slave mode, a re-route every
    # tick forever, because a failed route never sets _last_auto_target and the override guard then
    # compares None against None. A playerless unit still LEADS — peers read that from our snapshot.
    follow = None
    if mesh is not None and local_player_id is not None:
        from mesh.follow import FollowReconciler  # local import: avoids an import cycle

        follow = FollowReconciler(
            mesh.aggregator,
            mesh.router,
            local_unit_id=unit_id,
            local_player_id=local_player_id,
            peer_provider=mesh.discovery.get_peer,
            delegate=mesh.client.delegate_route,
            unroute_delegate=mesh.client.delegate_unroute,
            # Publish the follow target into our snapshot. Follow config lives on the follower, so
            # this is the only way a leader learns which rooms are locked to it — which is what
            # loudness matching's default scope keys off.
            on_master_change=lambda master: setattr(srv, "follows_unit_id", master),
        )
        follow.start()

    # Loudness matching. Runs on EVERY unit with the mesh up, including a playerless one: it drives
    # the players attached to THIS unit's own sources, which is the set `set_player_volume` can
    # actually resolve — so an ingest-only node still levels the speakers it is feeding. It is a
    # no-op until at least two endpoints in one group are calibrated, and it deliberately skips the
    # endpoint currently playing a calibration tone (whose level is unrelated to the group's).
    loudness = None
    if mesh is not None:
        from mesh.loudness import LoudnessReconciler  # local import: avoids an import cycle

        loudness = LoudnessReconciler(
            mesh.aggregator,
            mesh.router,
            local_unit_id=unit_id,
            tone_player_provider=lambda: mesh.api.tone_player_id,
            on_calibration_export=lambda table: setattr(srv, "calibration_export", table),
        )
        loudness.start()

    # Follow renames from Settings without a restart: the mesh snapshot reads srv.unit_name on every
    # request, so updating it is enough for the GUI and every peer's aggregated view, and the mDNS
    # record is re-advertised under the same service instance. The Sendspin server_name handed to
    # aiosendspin at construction keeps the boot-time value until the next restart (see unit_identity).
    async def _apply_unit_name(new_name: str) -> None:
        srv.unit_name = new_name
        if mesh is not None:
            await mesh.neighbourhood.rename(new_name)

    rename_watch = asyncio.ensure_future(unit_identity.watch_device_name(_apply_unit_name, fallback=env_unit_name))

    stop = install_shutdown_handlers()
    try:
        await stop.wait()  # run until SIGTERM; supervisord manages the process lifecycle
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
