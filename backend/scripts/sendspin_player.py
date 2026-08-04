#!/usr/bin/env python3
"""
Plum-Audio — the unit's hardware render endpoint (Sendspin PLAYER).

Phase 1: renders whatever group a server routes to it out the local ALSA device (onboard DAC
or HiFiBerry). Grew out of the validated DacPlayer in _resources/spike/handoff_probe.py.

Connection model — "servers dial the player" (the mesh model):
  - The player runs a ClientListener (mDNS OFF — 5353 collides with our Avahi) and waits for a
    server to dial it. It holds exactly ONE server connection at a time (SendspinClient allows
    only one attached websocket) — there is no idle "DISCOVERY pre-connect" to a second server
    (refuted on hardware; see ARCHITECTURE §2 and sendspin_server.reclaim_remote_player).
  - Another unit reclaims it by dialing (the ANOTHER_SERVER handoff). When a new server dials
    while we're attached to the old one, we release the old connection first, then attach the new
    — the player half of the reclaim. The roam is inaudible: no stream_clear/stream_end fires, so
    the jitter buffer keeps feeding the DAC straight through the reconnect.
  - PLUM_HOME_SERVER is an optional single-unit bring-up convenience: dial our home server on
    boot so audio flows immediately.

Render path:
  server stream --(WS, PCM)--> SendspinClient.audio_chunk --> AlsaRenderer jitter buffer
                                                          --> PortAudio callback --> hw:<card>
  We advertise PCM support so the server resamples/sends ready-to-play PCM (no client decode).

Sync note (the *why* of the current renderer): a jitter-buffer renderer is sample-correct for a
SINGLE unit (nothing to phase-align against) and reports ALSA xruns cleanly. True multi-room
phase-lock (schedule each chunk at compute_play_time(server_ts) against the DAC clock, with
drift resampling) is Phase-2 work and is isolated in AlsaRenderer — see TODO there.

Runs under supervisord as the `sendspin_player` program.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
import time

import numpy as np

from aiosendspin.client.client import SendspinClient
from aiosendspin.client.listener import ClientListener
from aiosendspin.models.core import DeviceInfo
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.artwork import ArtworkChannel, ClientHelloArtworkSupport
from aiosendspin.models.types import ArtworkSource, PictureFormat
from aiosendspin.models.visualizer import ClientHelloVisualizerSpectrum, ClientHelloVisualizerSupport
from aiosendspin.models.types import (
    AudioCodec,
    GoodbyeReason,
    MediaCommand,
    PlayerCommand,
    PlayerStateType,
    Roles,
)

try:
    import sounddevice as sd
except Exception:  # noqa: BLE001 - allows --probe-config / import without PortAudio present
    sd = None

from mesh.avahi import CLIENT_SERVICE, AvahiClient
from player_state import STATE_SAVE_DEBOUNCE_S, load_render_state, save_render_state, state_file_path

import unit_identity

logger = logging.getLogger("plum.sendspin_player")

DEFAULT_PORT = 8928
# Advertised to servers/controllers in client/hello device_info, so a foreign controller (Music
# Assistant) shows a real identity for this speaker rather than a bare id.
PLUM_SOFTWARE_VERSION = os.environ.get("PLUM_VERSION", "phase3-dev")
DEFAULT_RATE = 44100  # AirPlay-native; the server resamples other sources to this
DEFAULT_CHANNELS = 2
DEFAULT_BITS = 16
DAC_BLOCK_FRAMES = 480  # PortAudio callback block (~10 ms @ 48k); small → tight xruns
DEFAULT_TARGET_BUFFER_MS = 300  # jitter buffer depth we aim to hold ahead of the DAC
MAX_BUFFER_MS = 2000  # hard cap; drop oldest beyond this if the DAC falls behind
XRUN_LOG_EVERY = 50  # throttle xrun warnings


class AlsaRenderer:
    """Owns the PortAudio output stream and a thread-safe PCM jitter buffer.

    The PortAudio callback (a separate thread) drains the buffer at the DAC rate, padding with
    silence on underrun. The asyncio loop thread fills the buffer from received audio chunks.
    Volume/mute are applied here as software gain (the client receives raw PCM, so the endpoint
    must attenuate before the DAC).

    TODO(Phase 2 — multi-room sync): replace the free-running drain with timestamp-locked
    playback — map PortAudio's outputBufferDacTime to the client clock and pull the sample whose
    compute_play_time() matches, inserting silence / resampling to correct clock drift.
    """

    def __init__(self, rate: int, channels: int, bits: int, *, device: str | None, target_buffer_ms: int) -> None:
        self.rate = rate
        self.channels = channels
        self.bits = bits
        self.device = device
        self._bpf = channels * (bits // 8)
        self._target_bytes = self._bpf * rate * target_buffer_ms // 1000
        self._max_bytes = self._bpf * rate * MAX_BUFFER_MS // 1000

        self._buf = bytearray()
        self._lock = threading.Lock()
        self._stream = None

        # volume as software gain (0..100) + mute
        self._gain = 1.0
        self._muted = False

        # metrics
        self.xruns = 0
        self.starvations = 0
        self.starved_frames = 0
        self._playing = False  # gates idle silence from being logged as starvation
        self._xrun_since_log = 0
        # Unconditional silence accounting: every frame we had to pad, whether or not we thought
        # we were "playing". starved_frames alone hides a gap where stream_end/clear flipped us
        # idle (e.g. a cross-server roam) — this is the honest measure of audible dropout.
        self.pad_frames = 0

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if sd is None:
            raise RuntimeError("sounddevice/PortAudio unavailable — cannot open the DAC")
        self._stream = sd.RawOutputStream(
            samplerate=self.rate,
            channels=self.channels,
            dtype=f"int{self.bits}",
            blocksize=DAC_BLOCK_FRAMES,
            device=self.device,
            callback=self._callback,
        )
        self._stream.start()
        logger.info(
            "DAC open: device=%s %d:%d:%d block=%d target=%dms",
            self.device or "default",
            self.rate,
            self.bits,
            self.channels,
            DAC_BLOCK_FRAMES,
            self._target_bytes * 1000 // (self._bpf * self.rate),
        )

    def stop(self) -> None:
        if self._stream is not None:
            with contextlib.suppress(Exception):
                self._stream.stop()
                self._stream.close()
            self._stream = None

    # -- fill (asyncio thread) ----------------------------------------------

    def enqueue(self, pcm: bytes) -> None:
        with self._lock:
            self._playing = True
            self._buf.extend(pcm)
            if len(self._buf) > self._max_bytes:  # DAC behind → drop oldest to bound latency
                drop = len(self._buf) - self._max_bytes
                del self._buf[:drop]
                logger.warning("jitter buffer over %dms — dropped %d bytes", MAX_BUFFER_MS, drop)

    def flush(self) -> None:
        """Drop buffered audio (server sent stream_clear — discard pending)."""
        with self._lock:
            self._buf.clear()

    def mark_idle(self) -> None:
        """Stream ended; once the buffer drains we're idle (stop counting starvation)."""
        with self._lock:
            self._playing = False

    def set_volume(self, volume: int | None = None, muted: bool | None = None) -> None:
        if volume is not None:
            self._gain = max(0.0, min(100, volume)) / 100.0
        if muted is not None:
            self._muted = muted

    def stats(self) -> str:
        """Buffer + dropout counters. pad_ms is the true audible silence emitted so far."""
        with self._lock:
            buffered = len(self._buf)
        return (
            f"[buf={buffered * 1000 // (self._bpf * self.rate)}ms "
            f"xruns={self.xruns} starv={self.starvations} "
            f"pad_ms={self.pad_frames * 1000 // self.rate}]"
        )

    # -- drain (PortAudio thread) -------------------------------------------

    def _callback(self, outdata, frames, time_info, status) -> None:  # noqa: ANN001
        if status and getattr(status, "output_underflow", False):
            with self._lock:
                playing = self._playing
            if playing:
                self.xruns += 1
                self._xrun_since_log += 1
                if self._xrun_since_log >= XRUN_LOG_EVERY:
                    logger.warning("ALSA xruns: %d total", self.xruns)
                    self._xrun_since_log = 0
        need = frames * self._bpf
        with self._lock:
            take = min(need, len(self._buf))
            chunk = bytes(self._buf[:take])
            del self._buf[:take]
            playing = self._playing
            gain, muted = self._gain, self._muted
        if take < need:
            if playing:
                self.starvations += 1
                self.starved_frames += (need - take) // self._bpf
            self.pad_frames += (need - take) // self._bpf
            chunk += b"\x00" * (need - take)
        if muted or gain == 0.0:
            outdata[:] = b"\x00" * need
        elif gain != 1.0:
            samples = np.frombuffer(chunk, dtype="<i2").astype(np.float32) * gain
            outdata[:] = np.clip(samples, -32768, 32767).astype("<i2").tobytes()
        else:
            outdata[:] = chunk


class SendspinPlayer:
    """The unit's roamable render endpoint: a PLAYER-role SendspinClient wired to an
    AlsaRenderer, reachable by servers via a ClientListener (mDNS off)."""

    def __init__(
        self,
        player_id: str,
        player_name: str,
        *,
        port: int,
        renderer: AlsaRenderer,
        rate: int,
        channels: int,
        bits: int,
        static_delay_ms: float,
        initial_volume: int,
        initial_muted: bool = False,
        state_file: str | None = None,
    ) -> None:
        self.player_id = player_id
        self.player_name = player_name  # friendly name; also the mDNS TXT `name`
        self.port = port
        self.renderer = renderer
        # Our own render level. The server cannot tell us what it is — see _publish_render_state.
        self._volume = max(0, min(100, initial_volume))
        self._muted = initial_muted
        self._state_file = state_file or state_file_path()
        self._save_task: asyncio.Task | None = None

        support = ClientHelloPlayerSupport(
            supported_formats=[
                SupportedAudioFormat(codec=AudioCodec.PCM, channels=channels, sample_rate=rate, bit_depth=bits)
            ],
            buffer_capacity=self.renderer._max_bytes,  # advertise our real buffer ceiling
            supported_commands=[PlayerCommand.VOLUME, PlayerCommand.MUTE],
        )
        viz_support = ClientHelloVisualizerSupport(
            buffer_capacity=1 << 16,
            rate_max=30,
            types=["spectrum", "loudness"],
            spectrum=ClientHelloVisualizerSpectrum(n_disp_bins=256, scale="log", f_min=40, f_max=16000),
        )
        art_support = ClientHelloArtworkSupport(
            channels=[ArtworkChannel(source=ArtworkSource.ALBUM, format=PictureFormat.JPEG,
                                     media_width=512, media_height=512)],
        )
        self.client = SendspinClient(
            client_id=player_id,
            client_name=player_name,
            device_info=DeviceInfo(
                product_name="Plum Audio",
                manufacturer="Plum Solutions",
                software_version=PLUM_SOFTWARE_VERSION,
            ),
            # PLAYER renders; METADATA/CONTROLLER/VISUALIZER make this speaker a full CONSUMER of
            # whatever group it plays in — so a foreign server (Music Assistant) serving to us can be
            # observed (title/art), controlled (transport/volume) and visualized, as a group member.
            roles=[Roles.PLAYER, Roles.METADATA, Roles.CONTROLLER, Roles.VISUALIZER, Roles.ARTWORK],
            player_support=support,
            visualizer_support=viz_support,
            artwork_support=art_support,
            static_delay_ms=static_delay_ms,
            initial_volume=self._volume,
            initial_muted=self._muted,
        )
        renderer.set_volume(volume=self._volume, muted=self._muted)

        self.client.add_audio_chunk_listener(self._on_audio)
        self.client.add_stream_start_listener(self._on_stream_start)
        self.client.add_stream_clear_listener(self._on_stream_clear)
        self.client.add_stream_end_listener(self._on_stream_end)
        self.client.add_server_command_listener(self._on_server_command)

        self._listener: ClientListener | None = None
        self._avahi: AvahiClient | None = None  # mDNS advert (system Avahi), see start()
        # Self-report: what THIS speaker thinks is happening, pushed to the local mesh API.
        # Without it a speaker claimed by another server simply vanishes from our GUI — the local
        # server can only report clients attached to itself, and a claimed player is attached
        # elsewhere by definition. Any Sendspin server can take this speaker (that is the point of
        # the standard), so the GUI has to learn about it from the speaker itself.
        self._state: dict = {"attached": False}
        self._report_task: asyncio.Task | None = None
        self.report_url = os.environ.get("PLUM_PLAYER_STATE_URL", "http://127.0.0.1:5001/api/mesh/player-state")
        # Consume relay: forward the group's controller state + visualizer frames (which we receive
        # as a MEMBER of whatever server serves us, foreign ones included) to our GUI, and take
        # transport commands back. Latest-wins coalescing so 30 fps viz never backs up.
        self._relay_ctrl: dict | None = None
        self._relay_ctrl_dirty = False
        self._relay_viz: dict | None = None  # {"s":[0-255 x N],"l":loudness}
        self._relay_art: dict | None = None  # {"t":"art","d":<data-url|null>}
        self._relay_art_dirty = False
        # Audio-flow truth for the play/pause boolean. A foreign server (Music Assistant) reports its
        # controller `state`/`speed` unreliably to a member player — observed 'stopped' while audio
        # was clearly flowing, and speed pinned at 0 while playing — so we derive "playing" from
        # whether audio chunks are actually arriving. Updated on every chunk; sampled in _relay_send.
        self._last_audio_mono = 0.0
        self._audio_flowing = False
        self._relay_task: asyncio.Task | None = None
        self.relay_url = os.environ.get("PLUM_PLAYER_RELAY_URL", "ws://127.0.0.1:5001/api/mesh/consume?role=player")
        self.client.add_group_update_listener(self._on_group_update)
        self.client.add_metadata_listener(self._on_metadata)
        self.client.add_controller_state_listener(self._on_controller_state)
        self.client.add_visualizer_listener(self._on_visualizer)
        self.client.add_artwork_listener(self._on_artwork)

    # -- self-report ---------------------------------------------------------

    def _on_group_update(self, payload) -> None:
        new_group_id = getattr(payload, "group_id", None)
        if new_group_id != self._state.get("group_id"):
            # A route/switch/roam just landed us in a different group: the previous group's
            # track/progress numbers are meaningless here and must not be relayed as if they
            # described this one. Cleared BEFORE the new group_id lands, so a frame emitted before
            # fresh metadata arrives for the new group reports "no data yet," not stale old data —
            # the GUI already renders a missing np as unknown/idle rather than a wrong timeline.
            for field in ("title", "artist", "album", "track_progress", "track_duration",
                          "playback_speed", "progress_ts_us"):
                self._state.pop(field, None)
        state = getattr(payload, "playback_state", None)
        self._state["group_id"] = new_group_id
        self._state["group_name"] = getattr(payload, "group_name", None)
        self._state["playback_state"] = getattr(state, "value", state)
        self._emit_ctrl()  # relay the play/pause/stop change to the GUI

    def _on_controller_state(self, payload) -> None:
        ctrl = getattr(payload, "controller", None)
        if ctrl is None:
            return
        cmds = getattr(ctrl, "supported_commands", None)
        self._state["commands"] = [getattr(c, "value", c) for c in (cmds or [])]
        self._state["volume"] = getattr(ctrl, "volume", None)
        self._state["muted"] = getattr(ctrl, "muted", None)
        repeat = getattr(ctrl, "repeat", None)
        self._state["repeat"] = getattr(repeat, "value", repeat)  # RepeatMode -> "off"/"one"/"all"
        self._state["shuffle"] = getattr(ctrl, "shuffle", None)
        self._emit_ctrl()

    def _emit_ctrl(self) -> None:
        """Build the consume-relay `ctrl` frame from the latest control + transport state and mark
        it for send if anything changed. Carries the full transport picture the GUI needs for a
        FOREIGN source (Music Assistant): supported commands + volume, the group playback_state, and
        the metadata-role progress snapshot (position/duration/speed). The GUI anchors extrapolation
        to its own receipt time, so no shared clock is needed. Deduped on value — progress only
        changes when the server sends a fresh snapshot, so a re-emit re-anchors the now-playing bar;
        a frozen (paused) position produces no re-emit."""
        st = self._state
        new = {
            "t": "ctrl",
            "commands": st.get("commands", []),
            "volume": st.get("volume"),
            "muted": st.get("muted"),
            "repeat": st.get("repeat"),      # "off" | "one" | "all" (foreign source, e.g. MA)
            "shuffle": st.get("shuffle"),
            # `playing` is the authoritative play/pause boolean (audio actually flowing); `state`/
            # `speed` are relayed for reference but are unreliable from Music Assistant.
            "playing": self._audio_flowing,
            "state": st.get("playback_state"),
            # Spec position at *this instant*, computed on the server clock the player shares; the GUI
            # anchors to its own receipt time and extrapolates onward at `speed`.
            "progress": self._live_progress_ms(),
            "duration": st.get("track_duration"),
            "speed": st.get("playback_speed"),
        }
        if new != self._relay_ctrl:
            self._relay_ctrl = new
            self._relay_ctrl_dirty = True

    def _live_progress_ms(self):
        """Position right now, per the spec formula
            pos = track_progress + (server_now_us - timestamp) * playback_speed / 1e6
        evaluated on the server clock this player is synced to. Falls back to the raw snapshot when
        the clock isn't synchronized or fields are missing. Returns None if we have no snapshot."""
        st = self._state
        prog = st.get("track_progress")
        if prog is None:
            return None
        ts = st.get("progress_ts_us")
        speed = st.get("playback_speed")
        if ts is None or not speed:  # no anchor, or paused (speed 0/None) → snapshot as-is
            return prog
        try:
            if not self.client.is_time_synchronized:
                return prog
            now_srv = self.client.compute_server_time(self.client.now_us())
            pos = prog + (now_srv - ts) * speed // 1_000_000
        except Exception:  # noqa: BLE001 - clock not ready / API shape; fall back to the snapshot
            return prog
        dur = st.get("track_duration")
        if dur:  # clamp to [0, duration] when duration is known (0 = live/unknown)
            return max(0, min(pos, dur))
        return max(0, pos)

    def _on_visualizer(self, frames) -> None:  # frames: list[VisualizerFrame]
        # Spectrum and loudness arrive as separate frames; merge into the latest {s,l}. Scale the
        # wire's uint16 spectrum to 0-255, the shape the GUI's canvas already consumes.
        viz = self._relay_viz or {"t": "viz"}
        for f in frames:
            spec = getattr(f, "spectrum", None)
            if spec is not None:
                viz["s"] = [min(255, v >> 8) for v in spec]
            loud = getattr(f, "loudness", None)
            if loud is not None:
                viz["l"] = round(loud / 65535, 4)
        self._relay_viz = viz

    def _on_artwork(self, channel: int, data: bytes) -> None:
        # Channel 0 = album art (the only channel we declared). Empty payload = cleared. Relay as a
        # data URL over the consume WS (per-track, low rate — no need for a binary channel).
        if channel != 0:
            return
        import base64

        url = f"data:image/jpeg;base64,{base64.b64encode(data).decode()}" if data else None
        self._relay_art = {"t": "art", "d": url}
        self._relay_art_dirty = True

    @staticmethod
    def _defined(value):
        # mashumaro's omit_default leaves absent fields as an UndefinedField sentinel (not None);
        # treat both as "no value" so we never store the sentinel or a stale field.
        return None if value is None or type(value).__name__ == "UndefinedField" else value

    def _on_metadata(self, payload) -> None:
        md = getattr(payload, "metadata", None)
        if md is None:
            return
        for field in ("title", "artist", "album"):
            value = self._defined(getattr(md, field, None))
            if value is not None:
                self._state[field] = value
        # Progress snapshot for the now-playing bar (foreign sources). Per the Sendspin spec the
        # metadata `progress` object gives track_progress/track_duration/playback_speed (ms; speed
        # x1000, 0 = paused) and the enclosing metadata carries a `timestamp` (server µs) marking
        # when the snapshot is valid. The spec position is:
        #   pos = track_progress + (server_now_us - timestamp) * playback_speed / 1e6
        # Only the player shares the server's clock, so we anchor on that server timestamp here and
        # compute the live position at relay time in _emit_ctrl (see there).
        prog = self._defined(getattr(md, "progress", None))
        if prog is not None:
            self._state["track_progress"] = getattr(prog, "track_progress", None)
            self._state["track_duration"] = getattr(prog, "track_duration", None)
            self._state["playback_speed"] = getattr(prog, "playback_speed", None)
            self._state["progress_ts_us"] = self._defined(getattr(md, "timestamp", None))
        self._emit_ctrl()

    def _snapshot_state(self) -> dict:
        info = self.client.server_info
        attached = self.client.connected and info is not None
        state = {
            "player_id": self.player_id,
            "name": self.player_name,
            "url": f"ws://{self._host_hint()}:{self.port}/sendspin",
            "attached": bool(attached),
        }
        if attached and info is not None:
            reason = getattr(info.connection_reason, "value", info.connection_reason)
            # Only advertise a current track while audio is actually flowing. A foreign server (MA)
            # holds our player as a group member even when idle, and its last metadata title lingers
            # in _state — reporting it while stopped makes the "Idle Devices" chip claim a track is
            # playing when none is. audio-flow is the reliable truth (MA's playback_state is not).
            playing = self._audio_flowing
            state.update(
                server_id=info.server_id, server_name=info.name, connection_reason=reason,
                group_id=self._state.get("group_id"), group_name=self._state.get("group_name"),
                playback_state=("playing" if playing else "stopped"),
                playing=playing,
                title=self._state.get("title") if playing else None,
                artist=self._state.get("artist") if playing else None,
                album=self._state.get("album") if playing else None,
            )
        return state

    @staticmethod
    def _host_hint() -> str:
        from mesh.avahi import local_ip

        return local_ip() or "127.0.0.1"

    async def _relay_loop(self) -> None:
        """Producer side of the consume relay: push ctrl + viz to the mesh API, take commands back.
        Best-effort; the audio path never waits on it."""
        import aiohttp

        while True:
            try:
                async with aiohttp.ClientSession() as session, session.ws_connect(self.relay_url, heartbeat=30) as ws:
                    logger.info("consume relay connected (%s)", self.relay_url)
                    sender = asyncio.ensure_future(self._relay_send(ws))
                    try:
                        async for msg in ws:
                            if msg.type is aiohttp.WSMsgType.TEXT:
                                with contextlib.suppress(Exception):
                                    await self._relay_command(msg.json())
                    finally:
                        sender.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await sender
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - mesh API down / restarting; retry
                logger.debug("consume relay error; retrying", exc_info=True)
            await asyncio.sleep(2.0)

    async def _relay_send(self, ws) -> None:
        """~30 fps: emit the latest ctrl (on change) + latest viz frame (coalesced)."""
        tick = 0
        while True:
            # Stall safety-net: `playing` is driven by stream start/end, but if a stream stalls
            # (chunks stop for >1.5 s without a stream_end), fall back to paused. Only ever turns
            # flowing OFF — stream_start owns turning it ON.
            if self._audio_flowing and (time.monotonic() - self._last_audio_mono) > 1.5:
                self._audio_flowing = False
                self._emit_ctrl()
            # Refresh ~1/s so the cached ctrl (and any late-joining GUI) always gets a current spec
            # position — the live progress advances continuously, so this re-emits while playing and
            # is a no-op (deduped) while paused. Cheap: one small JSON frame per second.
            tick += 1
            if tick >= 30:
                tick = 0
                self._emit_ctrl()
            if self._relay_ctrl_dirty and self._relay_ctrl is not None:
                self._relay_ctrl_dirty = False
                with contextlib.suppress(Exception):
                    await ws.send_json(self._relay_ctrl)
            if self._relay_art_dirty and self._relay_art is not None:
                self._relay_art_dirty = False
                with contextlib.suppress(Exception):
                    await ws.send_json(self._relay_art)
            if self._relay_viz is not None:
                frame, self._relay_viz = self._relay_viz, None
                with contextlib.suppress(Exception):
                    await ws.send_json(frame)
            await asyncio.sleep(1 / 30)

    async def _relay_command(self, data: dict) -> None:
        """A transport command from the GUI → send it to the server we are a member of."""
        if data.get("t") != "cmd":
            return
        try:
            cmd = MediaCommand(data.get("command"))
        except ValueError:
            return
        kwargs = {}
        if cmd is MediaCommand.VOLUME and data.get("volume") is not None:
            kwargs["volume"] = int(data["volume"])
        if cmd is MediaCommand.MUTE and data.get("mute") is not None:
            kwargs["mute"] = bool(data["mute"])
        with contextlib.suppress(Exception):
            await self.client.send_group_command(cmd, **kwargs)
            logger.info("consume relay: sent %s to server", cmd.value)

    async def _report_loop(self) -> None:
        """Push our state to this unit's mesh API. Best-effort: the audio path never waits on it."""
        import aiohttp

        async with aiohttp.ClientSession() as session:
            while True:
                with contextlib.suppress(Exception):
                    await session.post(self.report_url, json=self._snapshot_state(), timeout=aiohttp.ClientTimeout(total=3))
                await asyncio.sleep(3.0)

    # -- lifecycle -----------------------------------------------------------

    async def start(self, home_server_url: str | None = None, *, advertise: bool = True) -> None:
        self.renderer.start()

        async def on_connection(ws) -> None:
            # attach_websocket raises if we're already attached to another server — that means a
            # reclaim handoff: release the old connection (goodbye ANOTHER_SERVER) then attach
            # the new one. This is the player half of cross-server roaming.
            try:
                await self.client.attach_websocket(ws)
            except RuntimeError:
                with contextlib.suppress(Exception):
                    await self.client.send_goodbye(GoodbyeReason.ANOTHER_SERVER)
                with contextlib.suppress(Exception):
                    await self.client.disconnect()
                try:
                    await self.client.attach_websocket(ws)
                except Exception:  # noqa: BLE001 - transient during rapid reclaim; server retries
                    logger.debug("attach race during reclaim", exc_info=True)
                    return
            disc = asyncio.Event()
            self.client.add_disconnect_listener(disc.set)
            logger.info("attached to a server %s", self.renderer.stats())
            await disc.wait()
            logger.info("detached from server %s", self.renderer.stats())

        self._listener = ClientListener(
            client_id=self.player_id, on_connection=on_connection, port=self.port, advertise_mdns=False
        )
        await self._listener.start()
        logger.info("player listening on :%d (id=%s)", self.port, self.player_id)
        if self.report_url:
            self._report_task = asyncio.ensure_future(self._report_loop())
        if self.relay_url:
            self._relay_task = asyncio.ensure_future(self._relay_loop())

        # Advertise ourselves the way the spec says a server-dialed client should: _sendspin._tcp,
        # port 8928, TXT path + name. Published through the system Avahi rather than aiosendspin's
        # own zeroconf, which would bind 5353 against the host daemon (see mesh/avahi.py). This is
        # what lets ANY Sendspin server — a peer unit, Music Assistant, anything — find this
        # speaker without being told about it.
        if advertise:
            self._avahi = AvahiClient()
            await self._avahi.publish(
                self.player_id, CLIENT_SERVICE, self.port,
                {"path": "/sendspin", "name": self.player_name},
            )

        if home_server_url:
            # Spec: "Do not manually connect to servers if you are advertising _sendspin._tcp."
            # A client picks ONE direction; ours is server-dialed (the mesh reclaims players by
            # URL, which requires it). So an explicit home-server dial is only honoured with
            # advertising off — single-unit bring-up, or a deliberately client-initiated setup.
            if advertise:
                logger.warning(
                    "ignoring home server %s: we advertise %s, so servers dial us (spec)",
                    home_server_url, CLIENT_SERVICE,
                )
            else:
                with contextlib.suppress(Exception):
                    await self.client.connect(home_server_url)
                    logger.info("dialed home server %s", home_server_url)

    async def stop(self) -> None:
        for task_attr in ("_report_task", "_relay_task"):
            task = getattr(self, task_attr)
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                setattr(self, task_attr, None)
        if self._avahi is not None:
            with contextlib.suppress(Exception):
                await self._avahi.close()
            self._avahi = None
        with contextlib.suppress(Exception):
            await self.client.disconnect()
        if self._listener is not None:
            await self._listener.stop()
        self.renderer.stop()

    # -- server → player events ---------------------------------------------

    def _on_stream_start(self, message) -> None:  # noqa: ANN001
        # Stream lifecycle is the definitive play/pause signal for a foreign server: MA opens a
        # stream on play/resume and ends it on pause. Drive `playing` off that, not raw chunk timing
        # (which would flicker True while the jitter buffer drains after a pause).
        self._audio_flowing = True
        self._last_audio_mono = time.monotonic()
        self._emit_ctrl()

    def _on_audio(self, server_ts_us: int, pcm: bytes, fmt) -> None:  # noqa: ANN001
        self._last_audio_mono = time.monotonic()  # for the stall safety-net only (never flips ON)
        self.renderer.enqueue(pcm)

    def _on_stream_clear(self, channels) -> None:  # noqa: ANN001
        logger.info("stream_clear -> flush (buffered audio discarded) %s", self.renderer.stats())
        self.renderer.flush()

    def _on_stream_end(self, channels) -> None:  # noqa: ANN001
        logger.info("stream_end -> idle %s", self.renderer.stats())
        self._audio_flowing = False  # stream ended → paused/stopped
        self._last_audio_mono = 0.0
        self.renderer.mark_idle()
        self._emit_ctrl()

    def _on_server_command(self, payload) -> None:  # noqa: ANN001
        # Volume/mute arrive nested: ServerCommandPayload.player -> PlayerCommandPayload(volume, mute).
        # A command carries ONE of them (a group volume change lands as vol=N mute=None immediately
        # followed by vol=None mute=False), so merge into our tracked state rather than replacing it.
        cmd = getattr(payload, "player", None)
        if cmd is None:
            return
        volume = getattr(cmd, "volume", None)
        muted = getattr(cmd, "mute", None)
        if volume is None and muted is None:
            return
        if volume is not None:
            self._volume = max(0, min(100, int(volume)))
        if muted is not None:
            self._muted = bool(muted)
        self.renderer.set_volume(volume=volume, muted=muted)
        logger.info("server volume/mute applied: vol=%s mute=%s", volume, muted)
        self._publish_render_state()

    def _publish_render_state(self) -> None:
        """Echo our level back to the server, and persist it (debounced).

        The echo is load-bearing, not cosmetic. `PlayerV1Role.set_volume()` only SENDS the command —
        it does not update the server's own view of this player. That view moves solely on
        `client/state`, and the client library sends exactly one of those, at connect, carrying
        `initial_volume`. Without this echo every level in the mesh view stays pinned at the
        connect-time value, the group average (`controller.volume`) reads 100 forever, and the
        library's group-volume redistribution computes its delta from a baseline that is not real.
        """
        if self.client.connected:
            asyncio.ensure_future(self._send_render_state())
        # Also keep the values the client reports on its NEXT connect in step: after a roam the
        # library re-sends its constructor-time initial_volume, which would otherwise hand the new
        # server a stale level.
        self.client._initial_volume = self._volume  # noqa: SLF001
        self.client._initial_muted = self._muted  # noqa: SLF001
        if self._save_task is None or self._save_task.done():
            self._save_task = asyncio.ensure_future(self._save_render_state())

    async def _send_render_state(self) -> None:
        with contextlib.suppress(Exception):  # a state report must never break the render path
            await self.client.send_player_state(
                state=PlayerStateType.SYNCHRONIZED, volume=self._volume, muted=self._muted
            )

    async def _save_render_state(self) -> None:
        await asyncio.sleep(STATE_SAVE_DEBOUNCE_S)
        save_render_state(self._state_file, self._volume, self._muted)


async def main() -> None:
    logging.basicConfig(
        level=os.environ.get("PLUM_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    unit_id = os.environ.get("PLUM_UNIT_ID", "unit-local")
    player_id = os.environ.get("PLUM_PLAYER_ID", f"{unit_id}-player")
    # A unit and its speaker are one thing to the user, so the player carries the unit's configured
    # device name. Env is the pre-naming default only (see unit_identity).
    env_player_name = os.environ.get(
        "PLUM_PLAYER_NAME", os.environ.get("PLUM_UNIT_NAME", unit_identity.DEFAULT_DEVICE_NAME)
    )
    player_name = unit_identity.device_name(env_player_name)
    port = int(os.environ.get("PLUM_PLAYER_PORT", DEFAULT_PORT))
    device = os.environ.get("PLUM_DAC_DEVICE") or None
    rate = int(os.environ.get("PLUM_PLAYER_RATE", DEFAULT_RATE))
    channels = int(os.environ.get("PLUM_PLAYER_CHANNELS", DEFAULT_CHANNELS))
    bits = int(os.environ.get("PLUM_PLAYER_BITS", DEFAULT_BITS))
    static_delay_ms = float(os.environ.get("PLUM_STATIC_DELAY_MS", "0"))
    target_buffer_ms = int(os.environ.get("PLUM_TARGET_BUFFER_MS", DEFAULT_TARGET_BUFFER_MS))
    home_server = os.environ.get("PLUM_HOME_SERVER") or None
    # Env is only what an un-adjusted speaker boots with; once its level has been set, the persisted
    # value wins — a restart (or a container rebuild) must not silently return the room to 100%.
    state_file = state_file_path()
    initial_volume, initial_muted = load_render_state(
        state_file, default_volume=int(os.environ.get("PLUM_INITIAL_VOLUME", "100"))
    )

    renderer = AlsaRenderer(rate, channels, bits, device=device, target_buffer_ms=target_buffer_ms)
    player = SendspinPlayer(
        player_id,
        player_name,
        port=port,
        renderer=renderer,
        rate=rate,
        channels=channels,
        bits=bits,
        static_delay_ms=static_delay_ms,
        initial_volume=initial_volume,
        initial_muted=initial_muted,
        state_file=state_file,
    )
    logger.info("render state: volume=%d muted=%s (%s)", initial_volume, initial_muted, state_file)
    await player.start(home_server_url=home_server)

    # Follow a rename from Settings: update the friendly name we report in player state and
    # re-advertise it over mDNS, so the GUI, peer units and third-party servers all see the new
    # name. The Sendspin client name presented at handshake is fixed for the life of the
    # connection — it catches up on the next restart rather than dropping the audio to rename.
    async def _apply_player_name(new_name: str) -> None:
        player.player_name = new_name
        if player._avahi is not None:
            await player._avahi.republish(
                player.player_id, CLIENT_SERVICE, player.port, {"path": "/sendspin", "name": new_name}
            )

    rename_watch = asyncio.ensure_future(
        unit_identity.watch_device_name(_apply_player_name, fallback=env_player_name)
    )

    stop = asyncio.Event()
    try:
        await stop.wait()  # run forever; supervisord manages the process lifecycle
    except asyncio.CancelledError:
        pass
    finally:
        rename_watch.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await rename_watch
        await player.stop()


if __name__ == "__main__":
    asyncio.run(main())
