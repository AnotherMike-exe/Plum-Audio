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

Sync note: playback is PHASE-LOCKED. Every chunk carries a server timestamp; the player converts it
to client-clock microseconds with `compute_play_time()` and AlsaRenderer serves the frame that is
due when the block it is filling reaches the DAC. Before 2026-09-13 the renderer free-ran — it
played audio the moment it arrived — so a unit's phase was whatever its first chunk landed on, four
units sat a quarter to half a second apart, and the offset moved every session. Nothing about that
was a regression or a buffer-size problem: it had never been implemented. AlsaRenderer holds all of
it; `PLUM_SYNC_LOCK=0` returns the old free-running drain.

Runs under supervisord as the `sendspin_player` program.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import functools
import logging
import os
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import numpy as np
from aiosendspin.client.client import SendspinClient
from aiosendspin.client.listener import ClientListener
from aiosendspin.client.models import PairingSupport
from aiosendspin.clock import RawMonotonicClock
from aiosendspin.models.artwork import ArtworkChannel, ClientHelloArtworkSupport
from aiosendspin.models.core import DeviceInfo
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.types import (
    ArtworkSource,
    AudioCodec,
    MediaCommand,
    PictureFormat,
    PlayerCommand,
    Roles,
)
from aiosendspin.models.visualizer import ClientHelloVisualizerSpectrum, ClientHelloVisualizerSupport

try:
    import sounddevice as sd
except Exception:  # noqa: BLE001 - allows --probe-config / import without PortAudio present
    sd = None

import audio_devices
import sendspin_identity
import unit_identity
from lifecycle import install_shutdown_handlers
from mesh.avahi import CLIENT_SERVICE, AvahiClient
from player_state import (
    STATE_SAVE_DEBOUNCE_S,
    load_last_playing_server,
    load_render_state,
    save_active_output,
    save_health_report,
    save_last_playing_server,
    save_player_health,
    save_render_state,
    state_file_path,
)

logger = logging.getLogger("plum.sendspin_player")

DEFAULT_PORT = 8928
# Advertised to servers/controllers in client/hello device_info, so a foreign controller (Music
# Assistant) shows a real identity for this speaker rather than a bare id.
PLUM_SOFTWARE_VERSION = os.environ.get("PLUM_VERSION", "phase3-dev")

# Where an operator finds our static PIN, from aiosendspin's CLOSED vocabulary
# (client.models.SECRET_LOCATIONS = device | leaflet | operator), enforced in PairingSupport's
# __post_init__. A module constant so the canary test can assert the real value rather than a copy.
PAIR_SECRET_LOCATIONS = ("device", "operator")

DEFAULT_RATE = 44100  # AirPlay-native; the server resamples other sources to this
DEFAULT_CHANNELS = 2
DEFAULT_BITS = 16
DAC_BLOCK_FRAMES = 480  # PortAudio callback block (~10 ms @ 48k); small → tight xruns
DEFAULT_TARGET_BUFFER_MS = 300  # nominal jitter depth — see AlsaRenderer.target_buffer_ms
MAX_BUFFER_MS = 2000  # hard cap; drop oldest beyond this if the DAC falls behind
XRUN_LOG_EVERY = 50  # throttle xrun warnings
OVERRUN_LOG_EVERY = 50  # ditto for buffer overruns — enqueue() runs once per ~20ms chunk
# Frames STARVED (padded while the renderer believed it was playing) since the last state report
# that mean "we are not keeping up", i.e. report client/state error. ~50ms at 44.1k: comfortably
# past a single scheduling hiccup, well under anything a listener would call continuous. The spec's
# example of a client that cannot maintain sync is exactly buffer underrun, and reporting it is how
# a server learns to send more lead time. See _health for why this is not pad_frames.
ERROR_STARVED_FRAMES = DEFAULT_RATE // 20
HEALTH_POLL_S = 3.0  # how often we re-evaluate whether to report client/state error
# How long one attached session may run before it is worth a line in the log. Comfortably longer
# than any normal roam or listening session boundary, so a healthy player never trips it — this is
# only meant to make a connection nobody is on the other end of visible after the fact.
STUCK_SESSION_WARN_S = 900.0


# -- dead-peer watchdog ----------------------------------------------------------------------------
# A server that vanishes without closing (power cut, cable pulled, `docker pause`) leaves a half-open
# TCP session. aiosendspin's listener builds the inbound WebSocketResponse with NO heartbeat
# (client/listener.py:139; the server side has heartbeat=30 — server/connection.py:270,
# server/server.py:826), so nothing on our side ever notices, and the player sits inside
# attach_websocket on a peer that is not there: RUNNING, port open, undialable.
#
# What "silent" means is taken from what the protocol sends while a session is IDLE, because a
# parked, mute session is a normal steady state (CLAUDE.md: Music Assistant holds a released
# speaker for as long as it likes). An admitted connection runs a time-sync loop for its whole life:
# it sends client/time every 0.2 s until the filter converges and then every 0.5 to 3.0 s
# (client/connection.py:1506-1527), and a server answers each one with server/time, unconditionally
# (server/connection.py:1781-1791). So a live session delivers an inbound frame at least every 3 s,
# audio or no audio, and "no audio" is never the signal.
#
# The signal is therefore: we kept SENDING and nothing came back. Both halves are needed. The loop
# is paused during pairing (client/connection.py:521) and a pairing window legitimately waits up to
# 300 s for an operator — but then we send nothing either, so the count of unanswered frames stays
# near zero and the watchdog stays quiet.
#
# 30 s is ten consecutive unanswered time requests at the slowest cadence, and one full period of
# the heartbeat the server runs against us. 0 disables the watchdog.
def _env_seconds(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, default)))
    except ValueError:
        return default


PEER_SILENCE_TIMEOUT_S = _env_seconds("PLUM_PEER_SILENCE_TIMEOUT_S", 30.0)
# Frames we must have sent, unanswered, before silence counts. Five is 15 s of time requests at the
# slowest cadence and well above the single frame a pairing step leaves waiting for an operator.
PEER_SILENCE_MIN_UNANSWERED = 5
# aiohttp's close() waits up to 10 s for the peer's close frame; give it that, then abort the socket.
PEER_CLOSE_TIMEOUT_S = 12.0
# After close() returns, how long the library gets to unwind attach_websocket before we abort the
# TCP connection as well. The unwind is a few event-loop turns; this is generous.
PEER_DETACH_WAIT_S = 2.0
# How long after the close a session may still be attached before we stop trusting the close.
PEER_CLOSE_GRACE_S = 20.0
# "Audio is flowing" for the exit decision: a chunk arrived this recently, on any session.
PEER_AUDIO_RECENT_S = 3.0
EXIT_PEER_WEDGED = 4


class _WatchedWebSocket:
    """The inbound websocket, with the time of the last frame each way. Not a patch to the library.

    aiosendspin takes the websocket as an argument and uses five things on it: `receive`,
    `send_str` (the Noise handshake), `send_bytes` (everything after it — noise/wire.py:113),
    `close` and `closed`. This forwards all of them and notes when a frame passed. It is the one
    hook that sees EVERY inbound frame — time replies, chunks, metadata, handshake — where the
    client's listeners see only the message types they were registered for and never server/time.

    Only data frames count as inbound traffic. aiohttp answers a ping inside `receive()` without
    returning it, and CLOSE/CLOSING/CLOSED/ERROR are the session ending, not the peer talking.
    """

    def __init__(self, ws) -> None:  # noqa: ANN001 - aiohttp.web.WebSocketResponse
        self._ws = ws
        now = time.monotonic()
        self.opened = now
        self.last_rx = now
        self.rx_frames = 0
        self.tx_frames = 0
        self.unanswered_tx = 0  # frames sent since the last inbound frame
        self.first_unanswered_tx: float | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ws, name)  # closed, close_code, exception, close, ...

    async def receive(self, *args, **kwargs):
        msg = await self._ws.receive(*args, **kwargs)
        if getattr(msg.type, "name", "") in ("TEXT", "BINARY"):
            self.last_rx = time.monotonic()
            self.rx_frames += 1
            self.unanswered_tx = 0
            self.first_unanswered_tx = None
        return msg

    def _note_tx(self) -> None:
        # Counted BEFORE the send: a send that raises on a dying socket was still a frame the peer
        # did not answer.
        self.tx_frames += 1
        self.unanswered_tx += 1
        if self.first_unanswered_tx is None:
            self.first_unanswered_tx = time.monotonic()

    async def send_bytes(self, data, *args, **kwargs) -> None:
        self._note_tx()
        await self._ws.send_bytes(data, *args, **kwargs)

    async def send_str(self, data, *args, **kwargs) -> None:
        self._note_tx()
        await self._ws.send_str(data, *args, **kwargs)

    def silence(self, now: float, timeout: float) -> dict | None:
        """The evidence that this peer is dead, or None while it is not (or cannot be judged)."""
        if timeout <= 0 or self.first_unanswered_tx is None:
            return None
        if self.unanswered_tx < PEER_SILENCE_MIN_UNANSWERED:
            return None
        silent_for = now - self.last_rx
        unanswered_for = now - self.first_unanswered_tx
        if silent_for < timeout or unanswered_for < timeout:
            return None
        return {
            "silent_for": silent_for,
            "unanswered_tx": self.unanswered_tx,
            "unanswered_for": unanswered_for,
            "rx_frames": self.rx_frames,
            "held": now - self.opened,
            "remote": getattr(getattr(self._ws, "_req", None), "remote", None),
        }

    def abort(self) -> bool:
        """Drop the TCP connection without waiting for anything. True if there was one to drop.

        The last resort after close(): a transport with unsent bytes for a dead peer does not
        finish a graceful close, and abort() does not need it to.
        """
        transport = getattr(getattr(self._ws, "_req", None), "transport", None)
        if transport is None:
            return False
        transport.abort()
        return True


# Output-stream death. The PortAudio callback runs every ~10 ms for as long as the stream is open,
# playing or not, so a counter that did not move across this many HEALTH_POLL_S polls means the
# stream is dead even though PortAudio still calls it active. Two polls, so that at least one full
# 3 s interval passed with no callback at all — one late poll cannot trip it.
OUTPUT_STALL_POLLS = 2
# Failed re-opens in a row before the process exits and supervisord starts a new one. A new process
# is the complete PortAudio re-initialisation; three in-process tries cover a card that was only
# briefly away (a USB DAC re-enumerating, an ALSA suspend) without holding a dead unit for long.
OUTPUT_REOPEN_MAX = int(os.environ.get("PLUM_OUTPUT_REOPEN_MAX", "3"))
# Kill switch, for the same reason PLUM_SYNC_LOCK has one: this acts on the audio path. If the
# watchdog misreads an output on some rig, one env var and a restart turns it off without a rebuild.
OUTPUT_WATCHDOG = os.environ.get("PLUM_OUTPUT_WATCHDOG", "1") != "0"
# A re-open that SUCCEEDS and is followed by another fault, with no healthy poll between, did not
# fix anything. Those are retried with a doubling delay up to this cap, and never end in an exit:
# the process restart is for a card that will not OPEN, and restarting over a stream that opens and
# then stalls would only move the same loop into supervisord.
OUTPUT_FLAP_RETRY_MAX_S = 300.0
# How often the health report in player_state.json is refreshed while nothing changes. Its age is
# what the container healthcheck reads, so healthcheck.py must allow several of these.
HEALTH_REPORT_S = 30.0
# Exit codes for a deliberate self-restart. 2 is "configured for no output" (see main()).
EXIT_OUTPUT_LOST = 3
# Upper bound on one AlsaRenderer.recover() call. PortAudio's stop()/close() can block for ever on a
# USB DAC that was unplugged, and the health loop awaits the call: without a bound, the peer-silence
# check, the health report and request_exit all stop with it. 30 s is ten health polls, far above a
# real re-open (1-2 s). 0 disables the bound.
OUTPUT_RECOVER_TIMEOUT_S = _env_seconds("PLUM_OUTPUT_RECOVER_TIMEOUT_S", 30.0)
# How long AlsaRenderer.stop() waits for the re-open lock. A worker thread that is stuck inside
# PortAudio holds the lock and never gives it back, and stop() runs on the SIGTERM path, where
# supervisord sends SIGKILL after `stopwaitsecs`.
RENDERER_STOP_LOCK_TIMEOUT_S = 1.0

# Upper bound on each network step of stop(). Three of them fit inside the supervisord
# `stopwaitsecs` with room for the DAC release that follows.
STOP_STEP_TIMEOUT_S = 2.5
# Consume-relay reconnect delay: doubles from MIN on each consecutive failure, capped at MAX.
RELAY_RETRY_MIN_S = 2.0
RELAY_RETRY_MAX_S = 30.0


# -- multi-room phase lock -------------------------------------------------------------------------
# Off returns the renderer to the free-running drain it had from Phase 1 to 2026-09-13: audio is
# played the moment it arrives and this unit's phase is whatever its first chunk happened to land
# on. Kept as a kill switch because this is the audio path — if locked playback misbehaves on a rig,
# one env var and a restart gets the old behaviour back without a rebuild.
SYNC_LOCK = os.environ.get("PLUM_SYNC_LOCK", "1") != "0"
# Error past which we STEP (pad silence, or drop frames) instead of trimming. 30 ms is about 10 m of
# path difference — well past anything a room's geometry explains, and the point where a listener
# hears two speakers rather than one. Every normal session crosses it exactly once, at the first
# chunk, which is the alignment this whole mechanism exists to perform.
SYNC_HARD_US = int(float(os.environ.get("PLUM_SYNC_HARD_MS", "30")) * 1000)
# Smoothed error we tolerate without correcting. 1.5 ms is ~0.5 m of path difference: inaudible as
# an echo, and comfortably above the jitter in PortAudio's own DAC-time report, so a unit sitting
# still does not trim forever.
SYNC_DEADBAND_US = int(float(os.environ.get("PLUM_SYNC_DEADBAND_MS", "1.5")) * 1000)
# Callbacks between single-frame trims. One frame per 4 blocks is ~520 ppm at 480 frames/10 ms —
# roughly 5x the worst crystal error between two Pis, and slow enough that the correction never
# outruns the EMA that drives it (which is what makes a trim loop oscillate).
SYNC_TRIM_EVERY = int(os.environ.get("PLUM_SYNC_TRIM_EVERY", "4"))
# Smoothing on the measured error. 0.01 per ~10 ms block is a ~1 s time constant: long enough to
# ignore the block-to-block jitter in snd_pcm_delay, short enough that real drift is acted on before
# it becomes audible.
SYNC_EMA_ALPHA = 0.01
SYNC_STEP_LOG_GAP_S = 2.0  # throttle on the step log line, NOT on the step itself
# Largest DAC lead we will believe from PortAudio. A host API that does not fill the time info
# reports 0, and a bogus huge value would schedule audio minutes out; both fall back to the latency
# the stream reported at open, and only if THAT is unusable do we free-run.
MAX_DAC_LEAD_US = 1_000_000


# Every background task this process starts. asyncio keeps only a WEAK reference to a task, so one
# created with a bare ensure_future and never awaited can be collected mid-flight — and one that
# raises keeps its exception until it is collected, which for a task held in a local is "never".
# Both happened here in silence: the settings watchers and the render-state echo.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _relay_backoff(failures: int) -> float:
    """Seconds to wait before relay attempt `failures + 1`: 2, 4, 8, 16, then 30 from there on."""
    return min(RELAY_RETRY_MAX_S, RELAY_RETRY_MIN_S * 2 ** min(max(0, failures - 1), 16))


def _spawn(coro, *, name: str) -> asyncio.Task:
    """Start `coro` as a task that is referenced until it ends and says so if it dies."""
    task = asyncio.ensure_future(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(functools.partial(_task_done, name))
    return task


def _task_done(name: str, task: asyncio.Task) -> None:
    _BACKGROUND_TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        # ERROR, and with the traceback: a watcher that died stays dead, so from here on a rename or
        # an output change in Settings does nothing until the process restarts.
        logger.error("background task %r died: %r — it does not restart", name, exc, exc_info=exc)


@dataclass(slots=True)
class _Chunk:
    """One received PCM chunk, and when its first UNPLAYED frame is due.

    `play_us` is client-clock microseconds — already through `compute_play_time()`, so it stays
    valid across a server change: the server's clock domain is converted away at enqueue, which is
    what keeps a cross-server roam inaudible (the buffer drains on the old timeline while the new
    connection's time filter converges).

    None means "no schedule": the time filter had not converged yet, or locking is off. Such a chunk
    plays contiguously with whatever precedes it, exactly as every chunk did before phase lock.
    """

    pcm: bytes
    play_us: int | None
    pos: int = 0  # bytes already handed to the DAC


class PlayerHealth(StrEnum):
    """Whether this renderer is keeping up. OURS, deliberately — no longer a protocol state.

    Until aiosendspin 9.x the spec had `client/state.state` as a REQUIRED enum, and `error` was its
    documented way for a client that "cannot maintain sync" (buffer underrun is the spec's own
    example) to tell a server to give it more lead time. We implemented that, and worked around the
    library dropping the field (UPSTREAM §0).

    **9.x deleted the concept.** `ClientStateType` is gone tree-wide and `ClientStatePayload.state`
    became `available: bool`, which is NOT a substitute: `send_available`'s own docstring says "an
    active source stream ends before the client reports unavailable", so reporting a transient xrun
    as `available=False` would tear the stream down — converting a brief dropout into a real one.
    There is no remaining wire field that means "struggling but still playing".

    So the detection stays and the reporting moves. The logic below was expensive to get right (two
    hardware-caught false positives: idle padding, and a paused source) and it is still the only
    thing that knows this speaker is dropping out — it now goes to the log and to player_state.json,
    where the mesh API can surface it, instead of onto the wire. That is strictly more queryable
    than before and strictly less conformant; the loss is the server-side reaction, which no server
    can perform any more anyway.
    """

    SYNCHRONIZED = "synchronized"
    ERROR = "error"
    # The output stream itself stopped: nothing reaches the DAC, however healthy the buffer looks.
    # Distinct from ERROR because the remedy differs — ERROR is "give me more lead time", this is
    # "my card went away", and only this one ends in a process restart.
    OUTPUT_LOST = "output_lost"


class AlsaRenderer:
    """Owns the PortAudio output stream and a thread-safe queue of TIMESTAMPED PCM chunks.

    The PortAudio callback (a separate thread) drains the queue at the DAC rate, padding with
    silence on underrun. The asyncio loop thread fills it from received audio chunks. Volume/mute
    are applied here as software gain (the client receives raw PCM, so the endpoint must attenuate
    before the DAC).

    **Phase lock.** Each chunk carries the client-clock time its first frame is due
    (`compute_play_time(server_ts)`, converted in the loop thread). Every callback asks PortAudio
    when the block it is filling will actually reach the DAC — `outputBufferDacTime - currentTime`,
    which is snd_pcm_delay — adds that to the client clock, and serves the frame that is due then:
    silence while the head chunk is still in the future, dropped frames while it is in the past.

    Only the DIFFERENCE between PortAudio's two timestamps is used, never either one on its own.
    PortAudio's clock on Linux is not the client's (it is the ALSA status tstamp; ours is
    CLOCK_MONOTONIC_RAW), so the two domains must never be compared — but the gap between them is
    a duration, and a duration is domain-free.

    What this replaced, and why it could never work: the drain was free-running. Audio played the
    moment it arrived, so a unit's phase was set by when its first chunk landed, and every padded
    underrun after that pushed it permanently later. `target_buffer_ms` did not gate it — that value
    only ever reached a log line. Two units 300 ms apart stayed 300 ms apart for the whole session.

    Correction of DAC-vs-client clock drift is a single dropped or duplicated frame every
    `SYNC_TRIM_EVERY` callbacks while the smoothed error sits outside `SYNC_DEADBAND_US`. One frame
    at 44.1 kHz is 23 us — below the threshold of hearing, and cheaper than a resampler on every
    block of every unit forever. A step correction (`SYNC_HARD_US`) is audible and deliberate: it
    happens at the first chunk of a session, which is the alignment, and otherwise says something
    changed that drift cannot explain.
    """

    def __init__(self, rate: int, channels: int, bits: int, *, device: str | None, target_buffer_ms: int) -> None:
        self.rate = rate
        self.channels = channels
        self.bits = bits
        self.device = device  # what we were ASKED for (settings/env spec)
        self.open_device: str | None = None  # what we actually OPENED, as a stable card id
        self._bpf = channels * (bits // 8)
        # Nominal, and nothing gates on it. Under phase lock the depth we actually hold is set by
        # the server's send-ahead (min_buffer + static_delay), not by us; before phase lock nothing
        # gated on it either — it reached one log line, which is why a "buffer sizing" explanation
        # for the offsets was never going to hold. Kept as the figure we would declare if we ever
        # stop taking the library's 250 ms min_buffer default.
        self.target_buffer_ms = target_buffer_ms
        self._max_bytes = self._bpf * rate * MAX_BUFFER_MS // 1000

        # The jitter buffer: chunks in arrival order, each with the play time of its first
        # unplayed frame. `_buffered` is the sum of what is left in them, kept incrementally so the
        # callback never walks the queue to size it.
        self._chunks: collections.deque[_Chunk] = collections.deque()
        self._buffered = 0
        self._lock = threading.Lock()
        self._stream = None
        # Output latency PortAudio reported at open, as the fallback DAC lead. Used only when the
        # per-callback time info is missing or implausible: a constant estimate still phase-locks
        # this unit to the rest (the error is fixed, so the per-endpoint delay knob absorbs it),
        # where free-running does not.
        self._latency_us = 0

        # Client clock. MUST be the same one aiosendspin converts play times into, or the two ends
        # of `compute_play_time` are in different domains — SendspinPlayer hands us the client's own
        # `now_us`. The default keeps a hand-built renderer (tests, the dev rig) working.
        self._now_us = RawMonotonicClock().now_us

        # volume as software gain (0..100) + mute
        self._gain = 1.0
        self._muted = False

        # Stream liveness. `callbacks` counts every PortAudio callback and is the only proof the
        # stream is really running: `stream.active` stays True for a stream whose device stopped
        # asking for audio. `stream_finished` is PortAudio telling us it stopped the stream itself.
        self.callbacks = 0
        self.stream_finished = False
        self._closing = False  # our own stop() is in progress — a finished callback is expected
        # Serialises every stop/open pair. An output switch and a stream recovery both tear the
        # stream down and re-open it, both from worker threads, and `_open` re-initialises
        # PortAudio's process-global state on the way: two of them at once is the SIGSEGV that
        # CLAUDE.md warns about. Reentrant because reopen() and recover() call stop().
        self._reopen_lock = threading.RLock()
        self.reopening = False  # a stop/open pair is in flight; liveness is meaningless until it ends
        # The last spec that actually opened. recover() goes back to it when a failed switch left
        # `device` as None, so a recovery never lands on PortAudio's default device by accident.
        self._last_good_spec = device

        # metrics
        self.xruns = 0
        self.starvations = 0
        self.starved_frames = 0
        self._playing = False  # gates idle silence from being logged as starvation
        self._xrun_since_log = 0
        self.overruns = 0  # buffer pinned at MAX_BUFFER_MS — the DAC is behind, we dropped oldest
        self._overrun_since_log = 0
        self.dropped_bytes = 0
        # Phase-lock telemetry. `sync_err_us` is the last measured error (+ = we are early, the
        # audio is due later than the block we are filling); `sync_ema_us` is the smoothed value the
        # trim acts on; None for either means "not locked right now".
        self.sync_err_us: int | None = None
        self.sync_ema_us: float | None = None
        self.locks = 0  # times this renderer reached the deadline — one per session is normal
        self.steps = 0  # steps that BROKE an alignment: the acquisition itself is not counted
        self.trims = 0  # single-frame drift corrections
        self.locked = False  # last callback scheduled against a timestamp
        self._trim_tick = 0
        self._last_step_log = 0.0
        self._dac_time_warned = False
        # Whether we have actually reached the deadline yet. False means the next scheduled block
        # aligns exactly rather than settling for "within SYNC_HARD_US" — see _take.
        self._aligned = False
        # Unconditional silence accounting: every frame we had to pad, whether or not we thought
        # we were "playing". starved_frames alone hides a gap where stream_end/clear flipped us
        # idle (e.g. a cross-server roam) — this is the honest measure of audible dropout.
        self.pad_frames = 0
        # Silence emitted ON PURPOSE, because the head chunk was not due yet. Separate from
        # pad_frames so that "how much dropout has this speaker had" stays answerable: a session
        # start now legitimately emits a few hundred ms of it while the DAC waits for the deadline.
        self.hold_frames = 0

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if sd is None:
            raise RuntimeError("sounddevice/PortAudio unavailable — cannot open the DAC")
        self._open(self.device)

    def _open(self, spec: str | None) -> None:
        """Open the PortAudio stream for `spec`, which is a device spec, not an ALSA address.

        Resolution goes through audio_devices so a stored `<card_name>:<device>` survives the card
        renumbering that a reboot can do. If that lookup finds nothing usable we still hand the raw
        spec to sounddevice: it substring-matches PortAudio's own names, which is how PLUM_DAC_DEVICE
        has always worked, and losing that fallback would strand a unit whose `aplay -l` is somehow
        unavailable. MUST NOT be called with a stream already open — resolution re-initialises
        PortAudio, which would pull that stream out from under the callback.
        """
        if audio_devices.is_no_output(spec):
            # Unreachable under the gate — a playerless unit has no player process — but reachable by
            # hand on the dev rig, which has no supervisord. Raising here rather than falling through
            # matters: `device="none"` would go to PortAudio's name matcher, which raises anyway but
            # only after the caller has already torn a working stream down.
            raise ValueError("this unit is configured for no output; there is no device to open")

        device_arg: str | int | None = spec
        resolved = None
        if spec:
            index, resolved = audio_devices.resolve_portaudio_index(spec)
            if index is not None:
                device_arg = index
                logger.info("output %r -> PortAudio index %d (%s)", spec, index, resolved.hw_id if resolved else "?")
            else:
                logger.warning("output %r did not resolve; letting PortAudio match it by name", spec)

        self._stream = sd.RawOutputStream(
            samplerate=self.rate,
            channels=self.channels,
            dtype=f"int{self.bits}",
            blocksize=DAC_BLOCK_FRAMES,
            device=device_arg,
            callback=self._callback,
            finished_callback=self._on_stream_finished,
        )
        self.stream_finished = False
        self._closing = False
        self._stream.start()
        # Fallback DAC lead for a callback whose time info PortAudio did not fill. `latency` is what
        # the host API settled on, in seconds; sounddevice reports it per stream after start.
        try:
            self._latency_us = max(0, int(float(self._stream.latency) * 1_000_000))
        except Exception:  # noqa: BLE001 - a host API that reports no latency just loses the fallback
            self._latency_us = 0
        # A different card has a different output latency, so the error we measured against the old
        # one says nothing about this one. Start the average again rather than trim toward a number
        # that belonged to another device.
        with self._lock:
            self.sync_ema_us = None
            self.sync_err_us = None
            self.locked = False
            self._aligned = False
            self._dac_time_warned = False
        self.device = spec
        self._last_good_spec = spec
        # What we actually OPENED, as the stable <card_name>:<device> identity — distinct from the
        # spec we were ASKED for. `pending` upstream is "choice vs echo"; echoing the request back
        # compares a string to itself and is structurally unable to notice that resolution landed on
        # a different card than intended (a stale hw:C,D after a renumber does exactly that). None
        # when resolution found nothing and PortAudio name-matched the raw spec — in which case we
        # genuinely do not know which card we are on, and saying so beats inventing an answer.
        self.open_device = resolved.id if (spec and resolved is not None) else None
        logger.info(
            "DAC open: device=%s %d:%d:%d block=%d latency=%dms lock=%s",
            spec or "default",
            self.rate,
            self.bits,
            self.channels,
            DAC_BLOCK_FRAMES,
            self._latency_us // 1000,
            "on" if SYNC_LOCK else "OFF (free-running)",
        )

    def reopen(self, spec: str | None) -> bool:
        """Move playback to a different output. Returns True if `spec` is what is now open.

        Called from a worker thread (never the event loop) because both PortAudio teardown and the
        `aplay -l` behind resolution block. Buffered audio and the software gain are kept: the user
        asked to change speakers, not to restart the track, and the jitter buffer refills from the
        server on its own.

        A failed switch restores the previous device rather than leaving the room silent — the whole
        point of this slice is that a bad output must be obvious, not inaudible. If even that fails
        we are genuinely silent, so it is logged at error and the state file records the truth.
        """
        if audio_devices.is_no_output(spec):
            # Refuse BEFORE the teardown below. Switching to "No output" takes effect at the next
            # container start (output_gate.py decides the player's existence, not its device), so
            # tearing this stream down now would go silent early for no benefit and then fail anyway.
            # Keeping the stream also keeps the echo where it is, which is what makes the API report
            # restart_required rather than claiming the switch already happened.
            logger.info("output 'none' selected — keeping %r open until this unit restarts", self.device)
            return False

        with self._reopen_lock:
            if spec == self.device and self._stream is not None:
                return True

            self.reopening = True
            try:
                previous = self.device
                self.stop()
                try:
                    self._open(spec)
                    return True
                except Exception:  # noqa: BLE001 - any PortAudio/ALSA failure means "did not switch"
                    logger.error("could not open output %r — restoring %r", spec, previous, exc_info=True)

                try:
                    self._open(previous)
                except Exception:  # noqa: BLE001
                    logger.error("restoring output %r ALSO failed — this unit is now silent", previous, exc_info=True)
                    self.device = None
                    self.open_device = None
                return False
            finally:
                self.reopening = False

    def recover(self) -> bool:
        """Re-open the output after the stream died. True if a stream is running again.

        The same stop/open pair as reopen(), under the same lock, so a recovery and an output
        switch can never run their PortAudio re-initialisation at the same time. Called from a
        worker thread for the same reason reopen() is: teardown and `aplay -l` both block.

        Buffered audio and the gain are kept, as in reopen(). `_open` resets the phase-lock state,
        so the first block after a recovery aligns to its deadline again instead of resuming at
        whatever offset the dead stream left.
        """
        with self._reopen_lock:
            self.reopening = True
            try:
                spec = self.device if self.device is not None else self._last_good_spec
                self.stop()
                try:
                    self._open(spec)
                    return True
                except Exception:  # noqa: BLE001 - the card is still away; the caller counts this
                    logger.error("could not re-open output %r after a stream failure", spec, exc_info=True)
                    return False
            finally:
                self.reopening = False

    def stream_fault(self) -> str | None:
        """Why the output stream is not running, or None while it is (or while a re-open is busy).

        Covers what PortAudio will ADMIT to. A stream whose device stopped pulling audio still
        reports active, and for that the caller watches `callbacks` — see
        SendspinPlayer._check_output.
        """
        if self.reopening:
            return None
        stream = self._stream
        if stream is None:
            return "no output stream is open"
        if self.stream_finished:
            return "PortAudio finished the stream (the callback raised, or the device was removed)"
        try:
            active = bool(stream.active)
        except Exception as exc:  # noqa: BLE001 - a stream PortAudio cannot describe is not running
            return f"the stream state is unreadable ({exc!r})"
        return None if active else "the stream is open but not active"

    def _on_stream_finished(self) -> None:
        """PortAudio's finished callback: the stream went inactive. Runs in the audio thread.

        It also fires for our own stop(), which is not a fault — `_closing` tells the two apart.
        Only a flag is set here: the audio thread must not re-open anything, and the health loop
        owns the recovery.
        """
        if not self._closing:
            self.stream_finished = True

    def stop(self) -> bool:
        """Close the output stream. False if another thread holds the re-open lock and keeps it.

        The lock is re-entrant, so reopen() and recover() pass straight through. A caller on a
        different thread waits RENDERER_STOP_LOCK_TIMEOUT_S at most: the holder can be a worker
        that PortAudio never returns to, and this is the last step of a SIGTERM shutdown.
        """
        if not self._reopen_lock.acquire(timeout=RENDERER_STOP_LOCK_TIMEOUT_S):
            logger.error(
                "stop: a re-open of %r did not release the output lock in %.1fs — the stream stays open",
                self.device,
                RENDERER_STOP_LOCK_TIMEOUT_S,
            )
            return False
        try:
            if self._stream is not None:
                self._closing = True
                with contextlib.suppress(Exception):
                    self._stream.stop()
                    self._stream.close()
                self._stream = None
            return True
        finally:
            self._reopen_lock.release()

    # -- fill (asyncio thread) ----------------------------------------------

    def set_clock(self, now_us) -> None:  # noqa: ANN001 - a zero-arg callable returning int us
        """Use `now_us` as the client clock. MUST be the clock aiosendspin converts play times into.

        Passing the client's own bound `now_us` rather than making a second RawMonotonicClock is not
        pedantry: `SendspinClient` takes an injectable clock, and a renderer holding a different one
        would compare two domains and schedule against a constant offset nobody can see.
        """
        self._now_us = now_us

    def enqueue(self, pcm: bytes, play_us: int | None = None) -> None:
        """Queue a received chunk. `play_us` is when its first frame is due, on the client clock.

        None means unscheduled — the time filter has not converged, or locking is off — and such a
        chunk plays contiguously with what precedes it, which is what every chunk did before phase
        lock. A mixed queue is normal and correct: the head chunk decides, one block at a time.
        """
        with self._lock:
            self._playing = True
            self._chunks.append(_Chunk(pcm, play_us))
            self._buffered += len(pcm)
            if self._buffered > self._max_bytes:  # DAC behind → drop oldest to bound latency
                drop = self._buffered - self._max_bytes
                self._discard_oldest(drop)
                self.overruns += 1
                self.dropped_bytes += drop
                # Throttled like the xrun path above, and for the same reason: enqueue runs once
                # per received chunk (~20ms), so a buffer that stays pinned logged ~50 lines/second
                # at WARNING — which no log level suppresses, and which rotated the lines that
                # explain the underlying stall out of the file within minutes.
                self._overrun_since_log += 1
                if self._overrun_since_log >= OVERRUN_LOG_EVERY:
                    logger.warning(
                        "jitter buffer over %dms — %d overruns, %d bytes dropped total",
                        MAX_BUFFER_MS,
                        self.overruns,
                        self.dropped_bytes,
                    )
                    self._overrun_since_log = 0

    def _discard_oldest(self, nbytes: int) -> None:
        """Drop `nbytes` from the head of the queue. Caller holds the lock."""
        while nbytes > 0 and self._chunks:
            head = self._chunks[0]
            remaining = len(head.pcm) - head.pos
            if remaining > nbytes:
                head.pos += nbytes
                self._buffered -= nbytes
                return
            self._chunks.popleft()
            self._buffered -= remaining
            nbytes -= remaining

    def flush(self) -> None:
        """Drop buffered audio (server sent stream_clear — discard pending)."""
        with self._lock:
            self._chunks.clear()
            self._buffered = 0
            # The timeline we were tracking is gone with the audio. Clearing the smoothed error
            # stops the next stream inheriting a trim direction from the last one, and clearing the
            # alignment makes the next one line up exactly rather than settle for SYNC_HARD_US.
            self.sync_ema_us = None
            self.sync_err_us = None
            self.locked = False
            self._aligned = False

    def mark_idle(self) -> None:
        """Stream ended; once the buffer drains we're idle (stop counting starvation)."""
        with self._lock:
            self._playing = False
            # The next stream aligns from scratch. Its first chunk is a fresh deadline, not a
            # continuation of this one, and treating it as one would let a whole session start up to
            # SYNC_HARD_US out of phase.
            self._aligned = False
            self.locked = False  # nothing is being scheduled any more; do not report that it is

    def set_volume(self, volume: int | None = None, muted: bool | None = None) -> None:
        if volume is not None:
            self._gain = max(0.0, min(100, volume)) / 100.0
        if muted is not None:
            self._muted = muted

    def stats(self) -> str:
        """Buffer + dropout counters. pad_ms is the true audible silence emitted so far."""
        with self._lock:
            buffered = self._buffered
            ema = self.sync_ema_us
        sync = "free" if ema is None else f"{ema / 1000:+.1f}ms"
        return (
            f"[buf={buffered * 1000 // (self._bpf * self.rate)}ms "
            f"xruns={self.xruns} starv={self.starvations} "
            f"pad_ms={self.pad_frames * 1000 // self.rate} "
            f"sync={sync} steps={self.steps} trims={self.trims}]"
        )

    def sync_report(self) -> dict:
        """Phase-lock state for player_state.json and the mesh self-report.

        This is how four units get compared without a microphone: read `sync_err_ms` off each one
        while they play the same source. `locked=False` on a unit that should be playing is the
        first thing to look at — it means that unit is free-running and nothing below it applies.
        """
        with self._lock:
            err = self.sync_err_us
            ema = self.sync_ema_us
            return {
                "locked": self.locked,
                "aligned": self._aligned,
                "sync_err_ms": None if err is None else round(err / 1000, 2),
                "sync_avg_ms": None if ema is None else round(ema / 1000, 2),
                "locks": self.locks,
                "steps": self.steps,
                "trims": self.trims,
            }

    # -- drain (PortAudio thread) -------------------------------------------

    def _frames_to_us(self, frames: int) -> int:
        return frames * 1_000_000 // self.rate

    def _us_to_frames(self, us: int) -> int:
        return us * self.rate // 1_000_000

    def _block_play_time(self, time_info) -> int | None:  # noqa: ANN001 - PaStreamCallbackTimeInfo
        """When the first frame of the block we are filling will reach the DAC, on the client clock.

        `outputBufferDacTime - currentTime` is the only thing read from PortAudio, and it is a
        DURATION (snd_pcm_delay, plus whatever the host API adds). Neither timestamp is comparable
        with ours — PortAudio's Linux clock is the ALSA status tstamp, the client's is
        CLOCK_MONOTONIC_RAW — but the gap between them belongs to no clock domain at all.

        None means "do not schedule this block": locking is off, or we have no idea where the DAC
        is. The caller then serves audio the instant it has it, which is the pre-2026-09-13
        behaviour and is never worse than guessing.
        """
        if not SYNC_LOCK:
            return None
        try:
            ahead_us = int((time_info.outputBufferDacTime - time_info.currentTime) * 1_000_000)
        except Exception:  # noqa: BLE001 - a host API that fills no time info must not kill audio
            ahead_us = 0
        if not 0 < ahead_us <= MAX_DAC_LEAD_US:
            if not self._dac_time_warned:
                self._dac_time_warned = True
                logger.warning(
                    "PortAudio reported no usable DAC time (lead=%d us) — scheduling against the "
                    "stream's own latency of %d ms instead; this unit stays phase-locked but with a "
                    "FIXED offset, so trim it out with the per-endpoint delay",
                    ahead_us,
                    self._latency_us // 1000,
                )
            ahead_us = self._latency_us
        return self._now_us() + ahead_us if ahead_us > 0 else None

    def _observe(self, err_us: int) -> None:
        """Record the block's timing error and fold it into the average the trim acts on."""
        self.sync_err_us = err_us
        if abs(err_us) > SYNC_HARD_US:
            # A step is about to remove this error. Seeding the average with it would leave the trim
            # chasing something that no longer exists, for the ~1 s the filter takes to forget it.
            self.sync_ema_us = 0.0
        elif self.sync_ema_us is None:
            self.sync_ema_us = float(err_us)
        else:
            self.sync_ema_us += SYNC_EMA_ALPHA * (err_us - self.sync_ema_us)

    def _trim_decision(self) -> int:
        """+1 duplicate one frame (we are early), -1 drop one frame (we are late), 0 leave it."""
        ema = self.sync_ema_us
        if ema is None:
            return 0
        self._trim_tick += 1
        if self._trim_tick < SYNC_TRIM_EVERY:
            return 0
        self._trim_tick = 0
        if ema > SYNC_DEADBAND_US:
            return 1
        if ema < -SYNC_DEADBAND_US:
            return -1
        return 0

    def _note_step(self, err_us: int) -> None:
        """Count and log a step. Counted ONLY when it breaks an alignment we had already reached.

        Acquiring the lock at the start of a session is a run of steps — one per callback for as
        long as the hold lasts, which is however far ahead the server sends. Counting those would
        put ~50 on the board before a note is played and make `steps` useless as the signal it
        exists to be: something moved a unit that was already in phase.

        So a step also ENDS the alignment, which is both true and what makes the count honest: the
        rest of that correction runs at the deadband threshold and is not counted again, and the
        `locks` counter picks it up when the unit lands. Measured on the rig 2026-09-13 before this:
        one 150 ms re-alignment after a membership change read as 16 steps on two units.
        """
        if self._aligned:
            self.steps += 1
            self._aligned = False
        now = time.monotonic()
        if now - self._last_step_log >= SYNC_STEP_LOG_GAP_S:
            self._last_step_log = now
            logger.info(
                "phase step %+.1f ms — %s",
                err_us / 1000,
                "holding the DAC until the audio is due" if err_us > 0 else "skipping overdue audio",
            )

    def _pad(self, nbytes: int, *, starved: bool) -> None:
        """Account for silence. Caller holds the lock.

        `starved` separates the two reasons we emit silence, and they mean opposite things. A
        DEADLINE hold is the mechanism working — the audio is not due yet — and counting it as a
        dropout would report every session start as a fault. Running dry while playing is the
        dropout, and that is what `_health` reads.
        """
        frames = nbytes // self._bpf
        if starved:
            if self._playing:
                self.starvations += 1
                self.starved_frames += frames
            self.pad_frames += frames
        else:
            self.hold_frames += frames

    def _take(self, need: int, block_play_us: int | None) -> bytes:
        """Serve exactly `need` bytes for the block that plays at `block_play_us`. Holds the lock.

        The loop is the phase lock. For each head chunk it asks "is the frame I am about to hand the
        DAC the frame that is due when this block reaches it?" and answers with silence, a skip, or
        the audio itself. Everything else in this class exists to make that question answerable.
        """
        out = bytearray()
        written = 0
        measured = False
        stepped = False
        while written < need and self._chunks:
            head = self._chunks[0]
            remaining = len(head.pcm) - head.pos
            if remaining <= 0:
                self._chunks.popleft()
                continue

            if head.play_us is None or block_play_us is None:
                self.locked = False
                take = min(need - written, remaining)
                out += head.pcm[head.pos : head.pos + take]
                head.pos += take
                self._buffered -= take
                written += take
                continue

            self.locked = True
            cursor_us = block_play_us + self._frames_to_us(written // self._bpf)
            due_us = head.play_us + self._frames_to_us(head.pos // self._bpf)
            err_us = due_us - cursor_us  # + we are early (due later), - we are late (overdue)
            if not measured:
                self._observe(err_us)
                measured = True

            # How close is close enough, and it is not one number.
            #
            # While ALIGNING — the first audio of a session, or the far side of a step already begun
            # in this block — the target is the deadband. Stopping at SYNC_HARD_US instead would
            # leave up to 30 ms of error, and the trim removes 30 ms in about a minute of playback:
            # every track would start out of phase and only settle halfway through. A skip is capped
            # by the chunk it skips into, so a 300 ms correction takes dozens of iterations here.
            #
            # Once ALIGNED the target is SYNC_HARD_US, because the question changes. A few ms of
            # error is drift, which the trim removes inaudibly; 30 ms is not drift, and stepping is
            # the only honest answer to it.
            threshold = SYNC_HARD_US if (self._aligned and not stepped) else SYNC_DEADBAND_US

            # Both corrections below move by WHOLE frames, so an error smaller than one frame
            # (23 us) is not correctable and falls through to the copy. Checking that here rather
            # than clamping inside each branch is what guarantees the loop always advances.
            off_by = self._us_to_frames(abs(err_us)) * self._bpf

            if err_us > threshold and off_by >= self._bpf:  # due later — hold the DAC with silence
                pad = min(need - written, off_by)
                out += bytes(pad)
                written += pad
                self._pad(pad, starved=False)
                if not stepped:
                    self._note_step(err_us)
                    stepped = True
                continue

            if err_us < -threshold and off_by >= self._bpf:  # overdue — skip to the frame due now
                skip = min(remaining, off_by)
                head.pos += skip
                self._buffered -= skip
                if not stepped:
                    self._note_step(err_us)
                    stepped = True
                continue

            if abs(err_us) <= SYNC_DEADBAND_US and not self._aligned:
                self._aligned = True
                self.locks += 1
                logger.info("phase locked: %+.2f ms off the deadline", err_us / 1000)

            trim = self._trim_decision()
            if trim < 0 and remaining > self._bpf:  # late — lose one frame (23 us at 44.1 kHz)
                head.pos += self._bpf
                self._buffered -= self._bpf
                remaining -= self._bpf
                self.trims += 1
            elif trim > 0:  # early — serve one frame twice
                out += head.pcm[head.pos : head.pos + self._bpf]
                written += self._bpf
                self.trims += 1
                if written >= need:
                    break

            take = min(need - written, remaining)
            out += head.pcm[head.pos : head.pos + take]
            head.pos += take
            self._buffered -= take
            written += take

        if written < need:
            self._pad(need - written, starved=True)
            out += bytes(need - written)
        # Defensive, and cheap: the callback assigns this straight into PortAudio's buffer, where a
        # length mismatch raises inside the audio thread and takes the stream down. Every path above
        # moves in whole frames, so this can only bite on a server chunk that is not frame-aligned.
        return bytes(out[:need])

    def _callback(self, outdata, frames, time_info, status) -> None:  # noqa: ANN001
        self.callbacks += 1  # liveness for the health loop; written here only, read anywhere
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
        # Read the DAC clock BEFORE taking the lock: the fill thread holds it for the length of an
        # enqueue, and a play time measured on the far side of that wait is a play time that has
        # already moved.
        block_play_us = self._block_play_time(time_info)
        with self._lock:
            chunk = self._take(need, block_play_us)
            gain, muted = self._gain, self._muted
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
        identity=None,
        pairing_store=None,
    ) -> None:
        # `player_id` is the LISTENER id — what we advertise over mDNS and what a server dials. It is
        # NOT the id a server knows us by after the handshake: 9.x derives that from `identity`
        # (identity.peer_id, the X25519 public key) and the client hello no longer carries a chosen
        # id at all. Two ids, and as with the two NAMES, the listener URL is the join between them.
        self.player_id = player_id
        self.player_name = player_name  # friendly name; also the mDNS TXT `name`
        self.port = port
        self.renderer = renderer
        # Our own render level. The server cannot tell us what it is — see _publish_render_state.
        self._volume = max(0, min(100, initial_volume))
        self._muted = initial_muted
        self._state_file = state_file or state_file_path()
        self._save_task: asyncio.Task | None = None
        # Health reporting: pad_frames at the last client/state, so _health measures the delta
        # rather than a lifetime total that would pin us at error forever after one dropout.
        self._last_starved_frames = 0
        self._last_reported_state = PlayerHealth.SYNCHRONIZED
        # Spec: persist the server that most recently had us playing. Loaded so a restart does not
        # re-write the same value, and so the id survives for the arbitration that will use it.
        self._last_playing_server = load_last_playing_server(self._state_file)

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
            channels=[
                ArtworkChannel(source=ArtworkSource.ALBUM, format=PictureFormat.JPEG, media_width=512, media_height=512)
            ],
        )
        self.client = SendspinClient(
            identity=identity or sendspin_identity.load_or_create(sendspin_identity.PLAYER_ROLE),
            client_name=player_name,
            # Required in 9.x. Carries the Pairing PSK this unit's own server pairs with, and the
            # unpaired_access flag (both set in sendspin_identity).
            pairing_store=pairing_store,
            # What makes this speaker pairable BY A HUMAN, and it is the presence of the wiring that
            # decides which methods we can offer at all: a gesture_prompt enables static PIN, and
            # either PIN out-channel enables dynamic PIN. A headless Pi has no screen, but every
            # unit serves a web GUI and Sendspin needs a network regardless — so the GUI IS the
            # display and the button, and both channels route to it over the consume relay.
            pairing_support=PairingSupport(
                gesture_prompt=self._pair_gesture_prompt,
                pin_display=self._pair_show_pin,
                offer_static_pin=True,
                # NOT free text — see PAIR_SECRET_LOCATIONS. A descriptive string raises here, at
                # construction, and the player crash-loops before it ever opens a card.
                secret_locations=PAIR_SECRET_LOCATIONS,
            ),
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
        # One clock for both ends of compute_play_time(). See AlsaRenderer.set_clock.
        renderer.set_clock(self.client.now_us)
        # Whether the LAST chunk carried a schedule, so the transition either way gets exactly one
        # log line instead of one per 20 ms chunk.
        self._locked = False

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
        # Server connections OVERLAP. A single "current session" field was wrong: 9.x brings an
        # incoming connection up provisionally before deciding whether to keep it, so short-lived
        # dials open and close while an older connection is still held — and each one clobbered the
        # start time, producing durations like "-0.0s" and, far worse, hiding the very thing this
        # was added to catch. Measured on the rig: session 306 stayed open while 307-311 came and
        # went. Keyed by sequence number so each session carries its own start.
        self._session_seq = 0
        self._sessions: dict[int, float] = {}
        self._sessions_warned: set[int] = set()
        self._report_task: asyncio.Task | None = None
        self._health_task: asyncio.Task | None = None
        self._init_recovery_state()
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
        # Pairing frame for the relay (a live PIN and/or a pending gesture request), latest-wins
        # like ctrl. Not part of the 3 s state POST: a PIN must reach the operator immediately, and
        # the relay is loopback-only so it never leaves the unit.
        self._relay_pair: dict | None = None
        self._relay_pair_dirty = False
        self._relay_failures = 0  # consecutive failed relay connections — drives backoff + logging
        self._relay_down_since = 0.0
        self.relay_url = os.environ.get("PLUM_PLAYER_RELAY_URL", "ws://127.0.0.1:5001/api/mesh/consume?role=player")
        self.client.add_group_update_listener(self._on_group_update)
        self.client.add_metadata_listener(self._on_metadata)
        self.client.add_controller_state_listener(self._on_controller_state)
        self.client.add_visualizer_listener(self._on_visualizer)
        self.client.add_artwork_listener(self._on_artwork)

    # -- self-recovery -------------------------------------------------------

    def _init_recovery_state(self) -> None:
        """State for the watchdogs in _health_loop. Separate so a test can build it on a fake."""
        # Set when the process must be replaced. main() waits on it beside SIGTERM and returns
        # `exit_code`, so supervisord (autorestart) starts a new process.
        self.exit_requested = asyncio.Event()
        self.exit_code: int | None = None
        # Output-stream watchdog.
        self._output_state = "ok"  # ok | recovering | lost
        self._output_fault: str | None = None
        self._output_reopens = 0  # successful recoveries since start
        self._output_failed_reopens = 0  # consecutive failures — reaches OUTPUT_REOPEN_MAX, then exit
        self._callbacks_seen = -1
        self._callback_stalls = 0
        self._output_unproven = 0  # successful re-opens in a row with no healthy poll after them
        self._output_retry_after = 0.0  # monotonic time before which a flapping output is left alone
        # Monotonic time of the last report write. -inf is "never": the monotonic clock starts near
        # 0 at boot, so 0.0 read as "written just now" for the first HEALTH_REPORT_S of uptime.
        self._health_report_at = float("-inf")
        # False when stop() could not release the DAC because a worker thread holds the re-open
        # lock. main() then leaves with os._exit, because asyncio.run() would join that thread.
        self.renderer_released = True
        # Dead-peer watchdog: the watched websocket of every open session, keyed like _sessions, and
        # when we started closing the ones found silent.
        self._session_ws: dict[int, _WatchedWebSocket] = {}
        self._silence_closing: dict[int, float] = {}
        self._silence_close_waiting: set[int] = set()  # close overdue but audio flowing — logged once
        self._peer_silence_closes = 0

    def request_exit(self, code: int, reason: str) -> None:
        """Ask main() to shut down and exit non-zero, so supervisord replaces this process."""
        if self.exit_code is not None:
            return
        self.exit_code = code
        logger.error("exiting with code %d so supervisord restarts the player: %s", code, reason)
        self.exit_requested.set()

    def _check_peer_silence(self) -> None:
        """Close any session whose peer stopped answering. Called on every health poll.

        See the note on PEER_SILENCE_TIMEOUT_S for what is measured and why. Every open session is
        checked, not the newest: connections overlap under 9.x, and the dead one is by definition
        the old one that newer dials come and go beside.

        Closing is enough to free the player: the library's reader is blocked in `receive()`, the
        close wakes it, the connection tears itself down, `attach_websocket` returns and the listener
        is ready for the next dial. If the session is STILL attached PEER_CLOSE_GRACE_S later, the
        close did not work and the process exits for supervisord to replace — but only while no
        audio is arriving, because another session may be playing and a restart would cut it.
        """
        if PEER_SILENCE_TIMEOUT_S <= 0:
            return
        now = time.monotonic()
        for session, watched in list(self._session_ws.items()):
            closing_since = self._silence_closing.get(session)
            if closing_since is not None:
                if now - closing_since >= PEER_CLOSE_GRACE_S:
                    self._peer_close_overdue(session, now - closing_since)
                continue
            evidence = watched.silence(now, PEER_SILENCE_TIMEOUT_S)
            if evidence is None:
                continue
            self._silence_closing[session] = now
            self._peer_silence_closes += 1
            logger.warning(
                "session %d is held by a SILENT peer (%s): no inbound frame for %.0fs (threshold %.0fs) while "
                "%d frames we sent went unanswered over %.0fs; held %.0fs, %d frames received in total, "
                "%d sessions open, client.connected=%s, audio_flowing=%s — closing it so a new dial can attach",
                session,
                evidence["remote"] or "address unknown",
                evidence["silent_for"],
                PEER_SILENCE_TIMEOUT_S,
                evidence["unanswered_tx"],
                evidence["unanswered_for"],
                evidence["held"],
                evidence["rx_frames"],
                len(self._session_ws),
                self.client.connected,
                self._audio_flowing,
            )
            _spawn(self._close_silent_session(session, watched), name=f"close silent session {session}")

    async def _close_silent_session(self, session: int, watched: _WatchedWebSocket) -> None:
        """Close one silent session: a websocket close first, then a TCP abort if it is still held."""
        try:
            # 1001 "going away". close() wakes the reader at once; what can take time is its wait
            # for the dead peer's close frame, which is why this runs as its own task.
            await asyncio.wait_for(watched.close(code=1001, message=b"peer silent"), PEER_CLOSE_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - a close that fails is handled by the abort below
            logger.warning("session %d: the websocket close did not complete (%r)", session, exc)
        # close() returning is not yet the session ending: the reader has to wake, the connection
        # has to tear itself down and attach_websocket has to return. Let that happen before
        # deciding the close was not enough.
        deadline = time.monotonic() + PEER_DETACH_WAIT_S
        while session in self._session_ws and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        if session not in self._session_ws:
            logger.info("session %d detached after the close; the listener can take a new dial", session)
        elif watched.abort():
            logger.warning("session %d was still attached after the close — aborted its TCP connection", session)

    def _peer_close_overdue(self, session: int, waited: float) -> None:
        """A silent session survived the close. Exit, unless audio is arriving on another session."""
        if time.monotonic() - self._last_audio_mono < PEER_AUDIO_RECENT_S:
            if session not in self._silence_close_waiting:
                self._silence_close_waiting.add(session)
                logger.warning(
                    "session %d is still attached %.0fs after the close, but audio is flowing — "
                    "not restarting; the check repeats when the audio stops",
                    session,
                    waited,
                )
            return
        self.request_exit(
            EXIT_PEER_WEDGED,
            f"session {session} is held by a silent peer and is still attached {waited:.0f}s after the close",
        )

    async def _check_output(self) -> None:
        """Detect a dead output stream and re-open it. Called on every health poll.

        Two signals, because they fail in different ways. `stream_fault()` is what PortAudio admits
        to: finished, inactive, or gone. The callback counter is what it does not: an ALSA device
        that stopped asking for audio leaves the stream "active" and silent, and the only evidence
        is that the callback is no longer being called. The stream is open for the whole life of
        the process, playing or idle, so a counter that stops is a fault in both cases.

        The re-open goes through AlsaRenderer.recover(), which shares one lock with an output
        switch — never a second PortAudio re-initialisation beside a running one. After
        OUTPUT_REOPEN_MAX failures in a row the process exits: a fresh process is the complete
        re-initialisation, and supervisord applies a growing delay between starts while the card
        stays away.
        """
        if not OUTPUT_WATCHDOG:
            return
        renderer = self.renderer
        if renderer.reopening:
            self._callbacks_seen = -1  # an output switch is in flight; start the count again after it
            self._callback_stalls = 0
            return
        fault = renderer.stream_fault()
        count = renderer.callbacks
        if fault is None:
            if count == self._callbacks_seen:
                self._callback_stalls += 1
                if self._callback_stalls >= OUTPUT_STALL_POLLS:
                    fault = (
                        f"the audio callback counter stalled at {count} for "
                        f"{self._callback_stalls * HEALTH_POLL_S:.0f}s while PortAudio reports the stream active"
                    )
            else:
                self._callback_stalls = 0
                if self._callbacks_seen >= 0 and self._output_unproven:
                    # The counter moved across a whole poll: the last re-open really took.
                    logger.info("output stream is stable again on %r", renderer.device)
                    self._output_unproven = 0
                    if self._output_state != "ok":
                        self._set_output_state("ok", None)
            self._callbacks_seen = count
        if fault is None:
            return
        now = time.monotonic()
        if now < self._output_retry_after:
            return  # flapping: the last re-open did not hold, so the next one waits its turn

        logger.warning(
            "output stream is DEAD on %r: %s — re-opening (failed opens %d of %d, re-opens that did not " "hold %d) %s",
            renderer.device,
            fault,
            self._output_failed_reopens,
            OUTPUT_REOPEN_MAX,
            self._output_unproven,
            renderer.stats(),
        )
        self._set_output_state("recovering", fault)
        # shield: a timeout must not cancel the worker's future, only stop the wait for it. The
        # thread cannot be interrupted, and `renderer.reopening` stays True while it is stuck, so
        # the early return above keeps this method from starting a second re-open beside it.
        attempt = asyncio.get_running_loop().run_in_executor(None, renderer.recover)
        try:
            recovered = await asyncio.wait_for(asyncio.shield(attempt), OUTPUT_RECOVER_TIMEOUT_S or None)
        except TimeoutError:
            self._set_output_state("lost", fault)
            self.request_exit(
                EXIT_OUTPUT_LOST,
                f"the output re-open did not return in {OUTPUT_RECOVER_TIMEOUT_S:.0f}s ({fault})",
            )
            return
        self._callbacks_seen = -1
        self._callback_stalls = 0
        if not recovered:
            self._output_failed_reopens += 1
            if self._output_failed_reopens >= OUTPUT_REOPEN_MAX:
                self._set_output_state("lost", fault)
                self.request_exit(
                    EXIT_OUTPUT_LOST,
                    f"the output stream did not re-open after {self._output_failed_reopens} attempts ({fault})",
                )
            return

        self._output_reopens += 1
        self._output_failed_reopens = 0
        self._output_unproven += 1
        if self._output_unproven == 1:
            # First recovery: believe it. The next healthy poll confirms it; the next fault says not.
            logger.warning("output stream re-opened on %r (recovery %d)", renderer.device, self._output_reopens)
            self._set_output_state("ok", None)
            return
        wait = min(OUTPUT_FLAP_RETRY_MAX_S, HEALTH_POLL_S * 2 ** min(self._output_unproven, 16))
        self._output_retry_after = time.monotonic() + wait
        logger.warning(
            "output stream re-opened on %r but the last %d re-opens did not hold — next attempt in %.0fs at the "
            "earliest; the output stays reported as 'recovering'",
            renderer.device,
            self._output_unproven - 1,
            wait,
        )

    def _set_output_state(self, state: str, fault: str | None) -> None:
        """Record the output state and publish it at once — a transition must not wait 30 s."""
        self._output_state = state
        self._output_fault = fault
        health = PlayerHealth.SYNCHRONIZED if state == "ok" else PlayerHealth.OUTPUT_LOST
        if health is not self._last_reported_state:
            self._last_reported_state = health
            save_player_health(self._state_file, health.value)
        self._write_health_report()

    def _health_report(self) -> dict:
        """What player_state.json `health` holds. `ts` is wall-clock: another process reads its age."""
        return {
            "ts": round(time.time(), 1),
            "player_health": self._last_reported_state.value,
            "output": {
                "state": self._output_state,
                "fault": self._output_fault,
                "device": self.renderer.open_device or self.renderer.device,
                "reopens": self._output_reopens,
                "failed_reopens": self._output_failed_reopens,
            },
            "sessions_open": len(self._sessions),
            "peer_silence_closes": self._peer_silence_closes,
        }

    def _report_startup_health(self) -> None:
        """Write the first report of this process, at once, after the output opened.

        A process that exits on a lost output leaves `output.state: lost` in the file. Without
        this write the healthcheck reads that old verdict from the NEW process until its first
        periodic report, HEALTH_REPORT_S later. `player_health` is written on a transition only,
        so the old `output_lost` stays there too. Inline: nothing is playing yet.
        """
        save_player_health(self._state_file, self._last_reported_state.value)
        self._write_health_report()

    def _write_health_report(self) -> None:
        self._health_report_at = time.monotonic()
        save_health_report(self._state_file, self._health_report())

    async def _refresh_health_report(self) -> None:
        """Refresh the report every HEALTH_REPORT_S, in a worker thread.

        The write is an fsync on the unit's SD card. That can take tens of milliseconds, and this
        loop also carries the audio feed — so the periodic write stays off it. A transition writes
        inline (_set_output_state): it is rare, and it must be on disk before a possible exit.
        """
        if time.monotonic() - self._health_report_at < HEALTH_REPORT_S:
            return
        self._health_report_at = time.monotonic()
        report = self._health_report()
        await asyncio.get_running_loop().run_in_executor(None, save_health_report, self._state_file, report)

    # -- self-report ---------------------------------------------------------

    def _on_group_update(self, payload) -> None:
        new_group_id = getattr(payload, "group_id", None)
        if new_group_id != self._state.get("group_id"):
            # A route/switch/roam just landed us in a different group: the previous group's
            # track/progress numbers are meaningless here and must not be relayed as if they
            # described this one. Cleared BEFORE the new group_id lands, so a frame emitted before
            # fresh metadata arrives for the new group reports "no data yet," not stale old data —
            # the GUI already renders a missing np as unknown/idle rather than a wrong timeline.
            for field in (
                "title",
                "artist",
                "album",
                "track_progress",
                "track_duration",
                "playback_speed",
                "progress_ts_us",
            ):
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
            "repeat": st.get("repeat"),  # "off" | "one" | "all" (foreign source, e.g. MA)
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
    def _defined[T](value: T) -> T | None:
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
            # The PEER id, not the listener id. This self-report is joined against
            # `UnitSnapshot.players[]`, which the server keys on `client.client_id` — the id a
            # client presents at the handshake, and under 9.x that is the X25519 public key.
            # Reporting the listener id here instead splits one speaker across two namespaces:
            # the GUI synthesises a SECOND row for it (sendspinDataService's `clients.find`
            # misses), and `router._idle_player_url` hands the listener id to a reclaim that
            # resolves clients by peer id — so an idle speaker dials fine and then times out at
            # 10 s, every time. They were the same string before the 9.x bump.
            "player_id": self.client.identity.peer_id,
            # Kept alongside for display and debugging: this is what we advertise over mDNS and
            # what a server dials. See the note on `player_id` in __init__.
            "listener_id": self.player_id,
            "name": self.player_name,
            "url": f"ws://{self._host_hint()}:{self.port}/sendspin",
            "attached": bool(attached),
            # Phase lock, so four units can be compared over the mesh API instead of by ear. Always
            # reported, attached or not: `locked: false` on a unit that is playing is the whole
            # answer, and it is invisible from anywhere else.
            "sync": self.renderer.sync_report(),
            # ok | recovering | lost. A unit whose DAC went away still answers on every port, so
            # without this the mesh view shows a healthy speaker that renders nothing.
            "output_state": self._output_state,
        }
        if attached and info is not None:
            # 9.x trimmed ServerInfo to (server_id, name) — `connection_reason` and `version` are
            # gone. Its successor is the connection's ACTIVITIES, which is also what the library's
            # new multi-server arbiter ranks on, so it is the more useful thing to surface anyway.
            # Nothing consumed the old field; this is telemetry for the mesh view and the GUI.
            activities = sorted(getattr(a, "value", a) for a in (self.client.activities or []))
            # Only advertise a current track while audio is actually flowing. A foreign server (MA)
            # holds our player as a group member even when idle, and its last metadata title lingers
            # in _state — reporting it while stopped makes the "Idle Devices" chip claim a track is
            # playing when none is. audio-flow is the reliable truth (MA's playback_state is not).
            playing = self._audio_flowing
            state.update(
                server_id=info.server_id,
                server_name=info.name,
                activities=activities,
                group_id=self._state.get("group_id"),
                group_name=self._state.get("group_name"),
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
        Best-effort; the audio path never waits on it.

        Retries with a doubling delay, RELAY_RETRY_MIN_S to RELAY_RETRY_MAX_S. The mesh API lives in
        the server process, so "down" is either a restart (seconds) or a server that is not coming
        back (hours) — a flat 2 s retry is right for the first and is 43 000 connection attempts a
        day for the second. One WARNING per outage and one INFO when it ends: this relay carries the
        pairing PIN and the transport controls, so a dead one must be visible at the default level,
        and at DEBUG it was not.
        """
        while True:
            try:
                await self._relay_session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - mesh API down / restarting; retry
                self._relay_failures += 1
                if self._relay_failures == 1:
                    self._relay_down_since = time.monotonic()
                    logger.warning(
                        "consume relay to %s failed: %r — retrying with backoff up to %.0fs; the GUI gets "
                        "no transport state, visualizer or pairing PIN from this player until it returns",
                        self.relay_url,
                        exc,
                        RELAY_RETRY_MAX_S,
                    )
                else:
                    logger.debug("consume relay still down (attempt %d)", self._relay_failures, exc_info=True)
            else:
                logger.info("consume relay closed by the mesh API; reconnecting")
            await asyncio.sleep(_relay_backoff(max(1, self._relay_failures)))

    def _relay_connected(self) -> None:
        """The relay websocket is up: log it, and end the outage if there was one."""
        if self._relay_failures:
            logger.info(
                "consume relay recovered after %d failed attempt(s) over %.0fs (%s)",
                self._relay_failures,
                time.monotonic() - self._relay_down_since,
                self.relay_url,
            )
        else:
            logger.info("consume relay connected (%s)", self.relay_url)
        self._relay_failures = 0

    async def _relay_session(self) -> None:
        """One relay connection, for its whole life. Returns on a clean close, raises on a failure."""
        import aiohttp

        async with aiohttp.ClientSession() as session, session.ws_connect(self.relay_url, heartbeat=30) as ws:
            self._relay_connected()
            sender = _spawn(self._relay_send(ws), name="consume relay sender")
            try:
                async for msg in ws:
                    if msg.type is not aiohttp.WSMsgType.TEXT:
                        continue
                    try:
                        await self._relay_command(msg.json())
                    except Exception as exc:  # noqa: BLE001 - one bad command must not end the relay
                        # A transport button that does nothing is a user-visible fault, and the
                        # blanket suppress that was here left no trace of it.
                        logger.warning("consume relay: command %.200r failed: %r", msg.data, exc)
            finally:
                sender.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await sender

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
            if self._relay_pair_dirty and self._relay_pair is not None:
                self._relay_pair_dirty = False
                with contextlib.suppress(Exception):
                    await ws.send_json(self._relay_pair)
            if self._relay_art_dirty and self._relay_art is not None:
                self._relay_art_dirty = False
                with contextlib.suppress(Exception):
                    await ws.send_json(self._relay_art)
            if self._relay_viz is not None:
                frame, self._relay_viz = self._relay_viz, None
                with contextlib.suppress(Exception):
                    await ws.send_json(frame)
            await asyncio.sleep(1 / 30)

    # -- pairing: the GUI is this speaker's display and its button ------------

    async def _pair_show_pin(self, pin: str | None) -> None:
        """`pin_display`: surface a derived dynamic PIN, or clear it when the exchange ends.

        Called with the PIN when one is derived and with None on success OR failure, so the GUI can
        take the dialog down either way. Pushed over the consume relay rather than ridden along on
        the 3 s player-state POST: a PIN the operator is waiting to read must appear immediately,
        and the relay is loopback-only, so the PIN never leaves the unit.
        """
        self._emit_pair(pin=pin)
        logger.info("pairing: %s", "PIN displayed" if pin else "PIN cleared")

    async def _pair_gesture_prompt(self, needed: bool) -> None:
        """`gesture_prompt`: ask the operator to confirm, or withdraw the ask.

        Its mere presence is what lets us offer static-PIN pairing, which the spec gesture-gates on
        EVERY attempt — so this is not a one-off confirmation, it is the thing that makes a static
        PIN safe to have. The GUI answers it by calling `open_pairing_window()` through the mesh API.
        """
        self._emit_pair(gesture=needed)
        logger.info("pairing: gesture %s", "requested" if needed else "withdrawn")

    def _emit_pair(self, *, pin: str | None = None, gesture: bool | None = None) -> None:
        """Latest-wins pairing frame for the relay, in the same `t`-tagged shape as ctrl/viz/art.

        Merges rather than replaces: a PIN and a gesture request can be live at once (static-PIN
        pairing asks for both), and each callback only knows about its own half.
        """
        frame = dict(self._relay_pair or {"t": "pair"})
        if pin is not None or "pin" in frame:
            frame["pin"] = pin
        if gesture is not None:
            frame["gesture"] = gesture
        self._relay_pair = frame
        self._relay_pair_dirty = True

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
        # Raises on purpose: _relay_session logs the failure with the command that caused it.
        await self.client.send_group_command(cmd, **kwargs)
        logger.info("consume relay: sent %s to server", cmd.value)

    async def _health_loop(self) -> None:
        """Report client/state whenever our health changes, not only when the volume moves.

        The spec wants `state: 'error'` while a client cannot maintain sync; that is a condition
        with a duration, so it needs its own cadence. Only sends on a TRANSITION — a healthy player
        keeps sending its one connect-time state and nothing more, which is what the spec asks for
        ("whenever any state changes thereafter").
        """
        while True:
            await asyncio.sleep(HEALTH_POLL_S)
            try:
                await self._health_tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - this loop IS the watchdog; one bad tick must not end it
                logger.exception("health tick failed; the watchdogs keep running")

    async def _health_tick(self) -> None:
        """One pass of everything that watches this player. Order: local faults first."""
        await self._check_output()
        self._check_peer_silence()
        await self._refresh_health_report()
        self._remember_playing_server()
        self._warn_if_session_is_stuck()
        if not self.client.connected:
            return
        if self._output_state != "ok":
            return  # OUTPUT_LOST is already recorded; _health() must not overwrite it with a buffer verdict
        state = self._health()
        if state is not self._last_reported_state:
            with contextlib.suppress(Exception):  # never break the render path over a report
                await self._send_client_state(state)

    def _warn_if_session_is_stuck(self) -> None:
        """Say so when a server connection has been held implausibly long.

        `attach_websocket` blocks for the whole life of a connection, so "detached from server"
        only prints when it RETURNS. A wedge observed on the rig showed `stream_end -> idle` and
        then that line never arriving, while the server had already dropped the client and every
        later dial timed out — a client holds exactly ONE websocket, so nothing could get in.

        That points at the player still sitting inside `attach_websocket` on a connection the other
        end has abandoned: a half-open socket we never noticed. This does not fix it and does not
        guess; it makes the state visible in the log, once per session, so a soak run can be read
        afterwards. `connected` is reported alongside because the interesting case is the library
        still believing it has a live peer.

        Checks EVERY open session, not just the newest. Connections overlap — 9.x brings an incoming
        one up provisionally before deciding whether to keep it — so the stuck one is by definition
        the old one that newer, short-lived dials would otherwise hide.
        """
        now = time.monotonic()
        for session, started in sorted(self._sessions.items()):
            if session in self._sessions_warned:
                continue
            held = now - started
            if held < STUCK_SESSION_WARN_S:
                continue
            self._sessions_warned.add(session)
            logger.warning(
                "session %d has been attached for %.0fs without detaching (%d sessions open, "
                "client.connected=%s, audio_flowing=%s) — if dials are timing out, this is the "
                "websocket holding them off",
                session,
                held,
                len(self._sessions),
                self.client.connected,
                self._audio_flowing,
            )

    def _remember_playing_server(self) -> None:
        """Persist the server_id of whoever most recently had us actually playing.

        The spec makes this storage a MUST, as the input to multi-server arbitration: a client is
        meant to accept a second server's handshake, compare it against the server it last played
        for, and then decide which to keep. We cannot do the deciding half yet — aiosendspin's
        attach_websocket blocks on server_hello inside the attach, so connection_reason is not
        readable until we have already committed (docs/UPSTREAM-AIOSENDSPIN.md). Storing it is
        implementable now, costs one key, and makes the rest a small delta later.

        Keyed on audio actually flowing rather than on the reported playback_state: a foreign server
        holds us as a group member while idle, and Music Assistant's playback_state is not reliable
        for this — the same reason _snapshot_state prefers _audio_flowing.
        """
        info = self.client.server_info
        if not (self.client.connected and info is not None and self._audio_flowing):
            return
        server_id = getattr(info, "server_id", None)
        if server_id and server_id != self._last_playing_server:
            self._last_playing_server = server_id
            _spawn(
                asyncio.get_running_loop().run_in_executor(None, save_last_playing_server, self._state_file, server_id),
                name="last playing server save",
            )
            logger.info("now playing for server %s (persisted)", server_id)

    async def _report_loop(self) -> None:
        """Push our state to this unit's mesh API. Best-effort: the audio path never waits on it."""
        import aiohttp

        async with aiohttp.ClientSession() as session:
            while True:
                with contextlib.suppress(Exception):
                    await session.post(
                        self.report_url, json=self._snapshot_state(), timeout=aiohttp.ClientTimeout(total=3)
                    )
                await asyncio.sleep(3.0)

    # -- lifecycle -----------------------------------------------------------

    async def start(self, home_server_url: str | None = None, *, advertise: bool = True) -> None:
        self.renderer.start()
        self._report_startup_health()

        async def on_connection(ws) -> None:
            """Serve one incoming server connection for its whole life.

            **Both workarounds this used to carry are gone**, and it is worth being explicit about
            why, because between them they were the player half of cross-server roaming:

            1. *The reclaim dance.* 6.0.5's `attach_websocket` raised RuntimeError if we already held
               a server, so we caught it, sent `goodbye: ANOTHER_SERVER`, disconnected, and re-attached
               — yielding to the newest dialer unconditionally. That was UPSTREAM §1, and it was wrong
               against a foreign server running a discovery sweep: it handed over a *playing* speaker.
               9.x brings the incoming connection up provisionally, completes the handshake, and then
               arbitrates by activity rank with the persisted last-playback server as the tiebreak —
               the "accept both, then decide" the spec asks for and we could not express. Displacing
               a holder emits ANOTHER_SERVER itself.
            2. *The conformant re-send.* We followed the library's connect-time `client/state` with
               our own because its was missing the spec's REQUIRED top-level field (UPSTREAM §0). 9.x
               replaced that field with `available` and sets it itself, so the duplicate is now pure
               noise on the mandatory message.

            `attach_websocket` blocks until the connection closes, which is what drives this handler,
            so the old disconnect-listener/Event pair is redundant too.
            """
            # Don't charge pre-attach idle padding to this session's health. Deliberately before the
            # attach: everything up to now is idle by definition, and the attach does not return
            # until the connection is over.
            self._last_starved_frames = self.renderer.starved_frames
            self._session_seq += 1
            session = self._session_seq
            self._sessions[session] = time.monotonic()
            logger.info(
                "server dialed us [session %d, %d open] %s", session, len(self._sessions), self.renderer.stats()
            )
            # The library gets the websocket through a wrapper that notes when a frame passed in
            # each direction — the dead-peer watchdog's only input. See _WatchedWebSocket.
            watched = _WatchedWebSocket(ws)
            self._session_ws[session] = watched
            try:
                await self.client.attach_websocket(watched)
            finally:
                started = self._sessions.pop(session, None)
                self._sessions_warned.discard(session)
                self._session_ws.pop(session, None)
                self._silence_closing.pop(session, None)
                self._silence_close_waiting.discard(session)
                held = time.monotonic() - started if started is not None else float("nan")
                logger.info(
                    "detached from server [session %d after %.1fs, %d still open] %s",
                    session,
                    held,
                    len(self._sessions),
                    self.renderer.stats(),
                )

        self._listener = ClientListener(
            client_id=self.player_id, on_connection=on_connection, port=self.port, advertise_mdns=False
        )
        await self._listener.start()
        logger.info("player listening on :%d (id=%s)", self.port, self.player_id)
        self._health_task = _spawn(self._health_loop(), name="health loop")
        if self.report_url:
            self._report_task = _spawn(self._report_loop(), name="state report")
        if self.relay_url:
            self._relay_task = _spawn(self._relay_loop(), name="consume relay")

        # Advertise ourselves the way the spec says a server-dialed client should: _sendspin._tcp,
        # port 8928, TXT path + name. Published through the system Avahi rather than aiosendspin's
        # own zeroconf, which would bind 5353 against the host daemon (see mesh/avahi.py). This is
        # what lets ANY Sendspin server — a peer unit, Music Assistant, anything — find this
        # speaker without being told about it.
        if advertise:
            self._avahi = AvahiClient()
            await self._avahi.publish(
                self.player_id,
                CLIENT_SERVICE,
                self.port,
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
                    home_server_url,
                    CLIENT_SERVICE,
                )
            else:
                with contextlib.suppress(Exception):
                    await self.client.connect(home_server_url)
                    logger.info("dialed home server %s", home_server_url)

    async def stop(self) -> None:
        """Shut down. Every step runs even if the one before it raised.

        Order is by what a failure costs. The level goes to disk FIRST: it is the only step whose
        loss outlives the process, it needs nothing else to be alive, and supervisord sends SIGKILL
        `stopwaitsecs` after SIGTERM — so anything that can block must come after it. The DAC is
        released LAST and in a `finally`, because a listener that failed to stop used to skip it.

        The network steps are bounded. `client.disconnect()` sends a goodbye and then waits for the
        peer's close frame, and on a half-open connection that wait is the full aiohttp close
        timeout; unbounded, it uses the whole SIGTERM grace period on a peer that is not there.
        """
        try:
            self._flush_render_state()
            for task_attr in ("_report_task", "_relay_task", "_health_task"):
                task = getattr(self, task_attr)
                if task is not None:
                    task.cancel()
                    # Exception too: a task that died earlier re-raises here, and that must not
                    # abort the shutdown. _task_done already logged it.
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
                    setattr(self, task_attr, None)
            if self._avahi is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self._avahi.close(), STOP_STEP_TIMEOUT_S)
                self._avahi = None
            try:
                await asyncio.wait_for(self.client.disconnect(), STOP_STEP_TIMEOUT_S)
            except Exception as exc:
                logger.warning("stop: the client did not disconnect cleanly: %r", exc)
            if self._listener is not None:
                try:
                    await asyncio.wait_for(self._listener.stop(), STOP_STEP_TIMEOUT_S)
                except Exception as exc:
                    logger.warning("stop: the listener did not stop cleanly: %r", exc)
                self._listener = None
        finally:
            # False only from AlsaRenderer.stop() when it could not take the lock.
            self.renderer_released = self.renderer.stop() is not False

    def _flush_render_state(self) -> None:
        """Write a pending level NOW. Synchronous on purpose: this is the shutdown path.

        `_publish_render_state` debounces the save by STATE_SAVE_DEBOUNCE_S so a slider drag is one
        write. A SIGTERM inside that window used to cancel nothing and save nothing — the process
        exited with the task still asleep, and the unit came back at the level from before the drag.
        """
        task = self._save_task
        self._save_task = None
        if task is None or task.done():
            return  # nothing pending: the last change is already on disk
        task.cancel()
        save_render_state(self._state_file, self._volume, self._muted)
        logger.info("stop: saved the pending level (volume=%d muted=%s)", self._volume, self._muted)

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
        play_us = None
        if SYNC_LOCK and self.client.is_time_synchronized():
            # Convert HERE, in the loop thread, not in the PortAudio callback. Two reasons, both
            # load-bearing: the time filter is only safe to read from the thread that updates it,
            # and a play time in CLIENT microseconds survives a server change — so the buffer keeps
            # draining on the right timeline while a roam's new connection re-converges its filter,
            # which is what keeps a roam inaudible.
            play_us = self.client.compute_play_time(server_ts_us)
        if play_us is None and self._locked:
            logger.info("time sync lost — the renderer is free-running until it converges again")
        elif play_us is not None and not self._locked:
            logger.info("time sync converged — phase-locked playback %s", self.renderer.stats())
        self._locked = play_us is not None
        self.renderer.enqueue(pcm, play_us)

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
            _spawn(self._send_render_state(), name="render state echo")
        # Also keep the values the client reports on its NEXT connect in step: after a roam the
        # library re-sends its constructor-time initial_volume, which would otherwise hand the new
        # server a stale level.
        self.client._initial_volume = self._volume  # noqa: SLF001
        self.client._initial_muted = self._muted  # noqa: SLF001
        if self._save_task is None or self._save_task.done():
            self._save_task = _spawn(self._save_render_state(), name="render state save")

    def _health(self) -> PlayerHealth:
        """SYNCHRONIZED, or ERROR while we are demonstrably failing to keep up.

        The spec asks a client that cannot maintain sync — buffer underrun is its own example — to
        report `state: 'error'`, which is how a server learns to give this player more lead time.
        AlsaRenderer has counted xruns, starvations and pad_frames since Phase 1 and none of it had
        ever left the process: _send_render_state hardcoded SYNCHRONIZED, so a speaker that was
        audibly dropping out reported itself perfectly healthy to Plum's server and to any foreign
        one.

        Measured on `starved_frames`, NOT `pad_frames`. Both count padded silence, but only
        starved_frames is gated on the renderer actually believing it is playing — pad_frames is
        deliberately unconditional, so an ATTACHED BUT IDLE player (no stream flowing, callback
        padding every block) accumulates a full second of it per second and trips any threshold
        instantly. Using pad_frames here reported `error` for every idle speaker in the mesh, which
        is both wrong and exactly the kind of false signal that teaches you to ignore the real one.
        Caught on hardware 2026-08-05: `state=error` with `starv=0`.
        """
        starved = self.renderer.starved_frames
        delta = starved - self._last_starved_frames
        self._last_starved_frames = starved

        # A PAUSED source starves the renderer without anything being broken, and it does not look
        # idle from in here: an AirPlay pause drops the metadata playback_speed to 0 while the
        # group's playback_state stays 'playing' and no stream_end fires, so AlsaRenderer._playing
        # stays True and every padded block counts as starvation. Measured on the rig 2026-08-05 —
        # ~12s of "starvation" on two units at once that was just the user pressing pause.
        # The baseline is still advanced above, so the pause's padding is not charged to the resume.
        if self._state.get("playback_speed") == 0:
            return PlayerHealth.SYNCHRONIZED

        return PlayerHealth.ERROR if delta >= ERROR_STARVED_FRAMES else PlayerHealth.SYNCHRONIZED

    async def _send_render_state(self) -> None:
        with contextlib.suppress(Exception):  # a state report must never break the render path
            await self._send_client_state(self._health())

    async def _send_client_state(self, state: PlayerHealth) -> None:
        """Send client/state, and record health where something can still act on it.

        `available` is NOT the old `state` field renamed. It means "can participate at all", and the
        server ends an active stream before honouring `available=False` — so a struggling-but-playing
        renderer reports `available=True`, always. A running player has an open output device by
        construction (AlsaRenderer.start() raises otherwise and SendspinPlayer.start() calls it before
        the listener), so there is currently no path here that should report False.

        The library now builds the whole payload, including the lead-time and buffer fields we used
        to reach into private attributes for — that workaround, and UPSTREAM §0's builder, are gone.

        The health signal itself survives in the log and in player_state.json. See PlayerHealth.
        """
        await self.client.send_player_state(available=True, volume=self._volume, muted=self._muted)
        if state is not self._last_reported_state:
            # WARNING rather than INFO on the way into ERROR: this is the only remaining surface for
            # "this speaker is dropping out", now that the wire cannot carry it.
            logger.warning("player health=%s %s", state.value, self.renderer.stats())
            self._last_reported_state = state
            # In a worker thread: the write is an fsync, and this path runs exactly when the
            # renderer is already short of audio — the loop that feeds it must not wait on a disk.
            await asyncio.get_running_loop().run_in_executor(None, save_player_health, self._state_file, state.value)

    async def _save_render_state(self) -> None:
        await asyncio.sleep(STATE_SAVE_DEBOUNCE_S)
        # In a worker thread, for the fsync. A stop() during it is covered: _flush_render_state sees
        # the task as not done and writes the level itself.
        await asyncio.get_running_loop().run_in_executor(
            None, save_render_state, self._state_file, self._volume, self._muted
        )


def _hard_exit_if_stuck(player) -> None:  # noqa: ANN001 - SendspinPlayer
    """Leave with os._exit when a worker thread is stuck inside PortAudio.

    asyncio.run() joins the default executor before it returns, and the interpreter joins its
    threads again at exit. A thread inside PortAudio's stop()/close() on a removed USB DAC never
    returns, so the process stays up until SIGKILL and supervisord never sees the exit code.
    player.stop() ran before this: the level and the health report are on disk.
    """
    if player.renderer_released:
        return
    code = player.exit_code if player.exit_code is not None else 0
    logger.error("a worker thread holds the output lock — leaving with os._exit(%d)", code)
    logging.shutdown()  # flush the log handlers; os._exit runs no cleanup
    os._exit(code)


async def main() -> int | None:
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
    # settings.json > PLUM_DAC_DEVICE, same precedence as the device name: env is what a unit boots
    # with before anyone has chosen an output, not an override of a choice they have made.
    device = audio_devices.configured_output_spec()
    if audio_devices.is_no_output(device):
        # This unit is configured for no output, so it should have no player process: output_gate.py
        # removes sendspin_player.ini from supervisord's runtime set before any of this starts. Being
        # here means the gate was bypassed — a hand-run on the dev rig, or a stale program set. Exit
        # rather than construct a renderer that cannot open anything. Under supervisord this is a
        # failed start, retried with a delay that grows by 1 s each time (see the `startretries`
        # note in supervisord/conf.d/sendspin_player.ini), and the container healthcheck reports
        # the program as not RUNNING for as long as it lasts.
        logger.error("this unit is configured for no output — the player should not have been started")
        return 2
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
    # Normally already on disk — the server mints both identities at startup and it has the lower
    # supervisord priority. load_or_create rather than a plain read so a hand-run player on the dev
    # rig (no supervisord) still works, and so the two can never disagree about which key is ours.
    identity = sendspin_identity.load_or_create(sendspin_identity.PLAYER_ROLE)
    # settings.json > PLUM_UNPAIRED_ACCESS > off. Applied at construction because the flag rides in
    # `client/hello`, so it is fixed for the life of a connection — a change takes effect on the
    # player's next restart, the same deliberate trade as a device rename.
    pairing_store = await sendspin_identity.client_pairing_store(
        unpaired_access=sendspin_identity.unpaired_access_enabled()
    )
    logger.info("player identity: peer_id=%s (listener id %s)", identity.peer_id, player_id)
    player = SendspinPlayer(
        player_id,
        player_name,
        identity=identity,
        pairing_store=pairing_store,
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

    rename_watch = _spawn(
        unit_identity.watch_device_name(_apply_player_name, fallback=env_player_name), name="device name watch"
    )

    # Follow an output change from Settings. The swap runs in a worker thread: PortAudio teardown
    # and the `aplay -l` behind device resolution both block, and stalling the event loop would
    # stall the feed from the server as well as the switch.
    async def _apply_output(spec: str | None) -> bool:
        if audio_devices.is_no_output(spec):
            # Return without touching the renderer OR the echo. "No output" is applied by the gate at
            # the next container start, not here — this unit has a player precisely because it did
            # not have that setting when it booted. Leaving the echo on the real device is what makes
            # the API report restart_required instead of claiming the change already took effect, and
            # it keeps the room playing in the meantime, which is what the GUI banner promises.
            logger.info("output 'none' selected — applies at the next restart; still on %r", renderer.device)
            return True  # nothing to retry: this one genuinely waits for a container restart

        applied = await asyncio.get_running_loop().run_in_executor(None, renderer.reopen, spec)
        # Echo what we ACTUALLY have open, not what was asked for — the config API reports this back
        # to the GUI, which is the only way a switch that failed can be told from one that worked.
        # open_device is the RESOLVED card id, so this also catches "opened, but the spec resolved to
        # a different card than intended" — which echoing the request back could never detect.
        save_active_output(state_file, renderer.open_device or renderer.device)
        if not applied:
            logger.error("output stayed on %r; %r could not be opened", renderer.device, spec)
        return applied  # False keeps watch_output_device retrying this target

    save_active_output(state_file, renderer.open_device or renderer.device)
    output_watch = _spawn(audio_devices.watch_output_device(_apply_output), name="output device watch")

    stop = install_shutdown_handlers()
    # Run until SIGTERM, or until a watchdog asks for a new process (player.request_exit).
    waiters = [
        _spawn(stop.wait(), name="shutdown signal"),
        _spawn(player.exit_requested.wait(), name="exit request"),
    ]
    try:
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        pass
    finally:
        for task in (*waiters, rename_watch, output_watch):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await player.stop()
    _hard_exit_if_stuck(player)
    # None on SIGTERM (exit 0). A watchdog's code otherwise, so supervisord logs an unexpected exit.
    return player.exit_code


if __name__ == "__main__":
    # SystemExit, not a bare asyncio.run: main() returns a non-zero code when it must not run at all
    # (this unit is configured for no output), and a discarded return value would exit 0 and leave
    # supervisord restarting it forever.
    raise SystemExit(asyncio.run(main()))
