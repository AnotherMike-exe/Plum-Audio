#!/usr/bin/env python3
"""
Plum-Audio — the calibration tone: a signal played from exactly ONE endpoint, at a known level.

WHY THIS IS NOT A `speaker-test` SHELL-OUT. The measurement is of the endpoint's own gain stage, so
the tone MUST travel the same path the music does: ingest -> encode -> websocket -> jitter buffer ->
the player's volume multiply -> the DAC. Plum-Snapcast wrote the tone straight to the server's ALSA
device with sox, which bypassed the volume stage entirely -- so every measurement read the same SPL
no matter what volume was requested, the fitted slope was zero, and the inverse produced NaN. That
is the single defect this module exists to not repeat. `audio_devices.test_device` is no help
either: it deliberately refuses the card the player already holds, which is precisely the card being
calibrated.

So the tone is a real, transient Sendspin source (`cal:<player_id>`) with the target player alone in
its group. That makes it work uniformly for this unit's own speaker, a peer's speaker (the router's
cross-server reclaim) and an adopted third-party speaker -- none of which a local ALSA write could
reach. The cost is that routing the player pulls it off whatever it was playing; the session
remembers where it was and puts it back.

WHY PINK NOISE BY DEFAULT. A single sine at one listening position sits in whatever standing-wave
pattern the room has at that frequency, so moving the meter a foot can change the reading by more
than the effect being measured. Pink noise averages across the band and is what SPL meters are
designed to integrate. A sine is offered because it is easier to hear as "still playing", not
because it measures better.

The generated buffer is DETERMINISTIC (fixed seed). Every sample in a calibration -- and every
endpoint in the mesh -- must hear an identically-shaped signal, or the differences being measured
are partly differences in the noise, not in the speakers.
"""

from __future__ import annotations

import array
import asyncio
import contextlib
import errno
import logging
import math
import os
import random
import time
from dataclasses import dataclass

logger = logging.getLogger("plum.calibration_tone")

# Source-id namespace for a live calibration tone. The GUI filters it out of the stream lists --
# it is deliberately still present in the mesh snapshot, because `Router.route_player` resolves the
# source through the view and hiding it there would make a peer's speaker unreachable.
CAL_SOURCE_PREFIX = "cal:"

TONE_PINK = "pink"
TONE_SINE = "sine"
TONE_TYPES = (TONE_PINK, TONE_SINE)

DEFAULT_TONE_SECONDS = 120.0
# Hard ceiling. A browser that navigates away mid-wizard leaves nothing to press Stop, and an
# unattended speaker playing noise is both antisocial and a way to lose a rig for an afternoon.
MAX_TONE_SECONDS = 300.0
DEFAULT_SINE_HZ = 1000.0

# How long start() waits for the feeder to open the FIFO read end before giving up. Generous next to
# the feeder's own retry loop, short enough that a broken source fails the request rather than
# reporting a tone nobody can hear.
WRITER_OPEN_TIMEOUT_S = 6.0

# Seconds of audio synthesized once and then looped. Long enough that the loop point is not audible
# as a rhythm, short enough that generating it costs a fraction of a second on a Pi.
LOOP_SECONDS = 5.0
# Peak amplitude, full scale = 1.0. Well below clipping (the encoder and the player's own gain both
# sit downstream) but loud enough to stay above a room's noise floor at the LOW end of the sweep,
# which is where a contaminated reading does the most damage to the fit.
PINK_PEAK = 0.6
SINE_PEAK = 0.35

_PINK_ROWS = 16  # Voss-McCartney octave generators
_SEED = 0x9E3779B9  # fixed: the tone must be identical everywhere, always

_WRITE_CHUNK_MS = 20  # matches the feeder's commit cadence
_PIPE_FULL_SLEEP_S = 0.005


def _frame_bytes(channels: int, bit_depth: int) -> int:
    return channels * (bit_depth // 8)


def generate_sine(sample_rate: int, channels: int, seconds: float, freq: float, peak: float) -> bytes:
    """A constant-amplitude sine, whole cycles only so the loop point does not click."""
    total = int(sample_rate * seconds)
    # Round to a whole number of cycles so looping the buffer is seamless.
    cycles = max(1, round(freq * total / sample_rate))
    total = max(1, round(cycles * sample_rate / freq))

    buf = array.array("h")
    step = 2.0 * math.pi * freq / sample_rate
    amp = peak * 32767.0
    for i in range(total):
        value = int(amp * math.sin(step * i))
        for _ in range(channels):
            buf.append(value)
    return buf.tobytes()


def generate_pink(sample_rate: int, channels: int, seconds: float, peak: float) -> bytes:
    """
    Voss-McCartney pink noise, normalised to `peak`, identical on every call.

    Voss rather than a filtered-white approach because it needs no numpy -- the audio process does
    not import it, and adding a numpy dependency to make a test tone would be a poor trade.
    """
    rng = random.Random(_SEED)
    total = int(sample_rate * seconds)

    rows = [rng.uniform(-1.0, 1.0) for _ in range(_PINK_ROWS)]
    running = sum(rows)

    samples = [0.0] * total
    largest = 1e-9
    for i in range(total):
        counter = i + 1
        # Index of the lowest set bit: row k is refreshed every 2^k samples, which is what gives
        # the 1/f slope.
        k = (counter & -counter).bit_length() - 1
        if k < _PINK_ROWS:
            running -= rows[k]
            rows[k] = rng.uniform(-1.0, 1.0)
            running += rows[k]
        value = (running + rng.uniform(-1.0, 1.0)) / (_PINK_ROWS + 1)
        samples[i] = value
        if abs(value) > largest:
            largest = abs(value)

    scale = (peak / largest) * 32767.0
    buf = array.array("h")
    for value in samples:
        # Clamp defensively: `scale` is derived from the observed peak, so this cannot trigger, but
        # a sample outside int16 would raise inside array.append and take the audio loop with it.
        pcm = max(-32768, min(32767, int(value * scale)))
        for _ in range(channels):
            buf.append(pcm)
    return buf.tobytes()


_TONE_CACHE: dict[tuple, bytes] = {}


def build_tone(tone_type: str, sample_rate: int, channels: int, freq: float = DEFAULT_SINE_HZ) -> bytes:
    """The looped tone buffer, synthesized once per process.

    Cached because the output is deterministic by design — and because generating it is not free.
    `generate_pink` is a quarter of a million iterations of pure Python, which on a Pi takes on the
    order of a second and holds the GIL for most of it: an executor thread moves it off the loop but
    does NOT stop it competing with the feeder's 20 ms commit cadence for interpreter slices. Paying
    that once per process rather than once per Play is worth a few hundred KB.
    """
    key = (tone_type, sample_rate, channels, freq if tone_type == TONE_SINE else None)
    cached = _TONE_CACHE.get(key)
    if cached is not None:
        return cached
    if tone_type == TONE_SINE:
        pcm = generate_sine(sample_rate, channels, LOOP_SECONDS, freq, SINE_PEAK)
    else:
        pcm = generate_pink(sample_rate, channels, LOOP_SECONDS, PINK_PEAK)
    _TONE_CACHE[key] = pcm
    return pcm


@dataclass
class ToneState:
    """What the GUI polls while the wizard is open."""

    player_id: str
    source_id: str
    volume: int
    tone_type: str
    started_at: float
    seconds: float
    restore_source_id: str | None
    restore_volume: int | None
    # Set when WE dialled this speaker to tone it. Letting go of an adopted speaker is not the same
    # operation as unrouting one of our own: detaching drops it from the group but leaves the
    # websocket up, so its real server (Music Assistant) can never take it back. See _stop_locked.
    adopted_url: str | None = None
    # Carried rather than recomputed from `player_id`: on the adoption path the id is REPLACED by
    # the one the handshake gave, so deriving the path at teardown would unlink a name that never
    # existed and leak a FIFO per third-party calibration.
    fifo_path: str = ""

    def to_dict(self) -> dict:
        elapsed = max(0.0, time.monotonic() - self.started_at)
        return {
            "playing": True,
            "playerId": self.player_id,
            "sourceId": self.source_id,
            "volume": self.volume,
            "toneType": self.tone_type,
            "elapsed": round(elapsed, 1),
            "remaining": round(max(0.0, self.seconds - elapsed), 1),
        }


class ToneError(RuntimeError):
    """A tone request that cannot be honoured — surfaced to the GUI as a 400, not a 500."""


class CalibrationToneController:
    """
    Owns the one calibration tone a unit may be playing, and the state needed to undo it.

    One session at a time, deliberately: two tones at once are two speakers making noise while the
    user is trying to read a meter, and the second would silently replace the first's restore
    record. Starting a tone while one is running stops the first properly, restore included.

    Everything here runs in the audio event loop. Tone synthesis is the one exception -- it is a
    tight Python loop over a quarter of a million samples, which on a Pi is long enough to stall the
    feeder's 20 ms commit cadence, so it is pushed to an executor thread.
    """

    def __init__(self, engine, router, view_provider, *, refresh_view=None, fifo_dir: str = "/tmp") -> None:
        self._engine = engine
        self._router = router
        self._view = view_provider
        # The aggregated mesh view is a CACHE, rebuilt on a 2 s poll — and `Router.route_player`
        # resolves a source through it. A source created moments ago is therefore not there yet, so
        # routing onto a freshly-started tone fails with "no unit ingests source 'cal:...'". Measured
        # on the rig; invisible to the unit tests, whose fake router never consults a view.
        self._refresh_view = refresh_view
        self._fifo_dir = fifo_dir
        self._state: ToneState | None = None
        self._writer: asyncio.Task | None = None
        self._watchdog: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    # -- introspection --------------------------------------------------------

    def status(self) -> dict:
        return self._state.to_dict() if self._state else {"playing": False}

    @property
    def active_player_id(self) -> str | None:
        return self._state.player_id if self._state else None

    # -- lifecycle ------------------------------------------------------------

    async def start(
        self,
        player_id: str,
        volume: int,
        *,
        url: str | None = None,
        tone_type: str = TONE_PINK,
        seconds: float = DEFAULT_TONE_SECONDS,
        freq: float = DEFAULT_SINE_HZ,
    ) -> dict:
        if not player_id:
            raise ToneError("player_id required")
        if tone_type not in TONE_TYPES:
            raise ToneError(f"tone must be one of {', '.join(TONE_TYPES)}")
        volume = max(0, min(100, int(volume)))
        seconds = max(1.0, min(MAX_TONE_SECONDS, float(seconds)))

        async with self._lock:
            # Carry the previous session's restore record forward rather than re-reading the view.
            # The aggregator polls on an interval, so a view read moments after the last tone would
            # report the TONE's volume and group as "what to go back to" — and the wizard restarts
            # the tone once per measurement, so that error compounds across a calibration.
            carried_source, carried_volume = None, None
            if self._state is not None:
                carried_source = self._state.restore_source_id
                carried_volume = self._state.restore_volume
                await self._stop_locked("superseded by a new tone")

            restore_source, restore_volume = self._current_placement(player_id)
            if carried_source is not None or carried_volume is not None:
                restore_source, restore_volume = carried_source, carried_volume

            source_id = CAL_SOURCE_PREFIX + player_id
            fifo_path = os.path.join(self._fifo_dir, f"cal-{_fifo_safe(player_id)}-fifo")

            loop = asyncio.get_running_loop()
            sample_rate, channels = 44100, 2
            pcm = await loop.run_in_executor(None, build_tone, tone_type, sample_rate, channels, freq)

            self._engine.start_source(source_id, fifo_path)
            # Make the new source visible to the router BEFORE trying to route onto it. Without
            # this the route fails outright: the view it resolves against is a 2 s cache that
            # predates the source. Cheap and bounded — the local snapshot is free and peer fetches
            # are capped and individually fault-tolerant.
            if self._refresh_view is not None:
                with contextlib.suppress(Exception):
                    await self._refresh_view()
            # The writer signals when the FIFO write end is actually open. Without waiting for it, a
            # source whose feeder never opened its read end still returns `playing: true` — the
            # wizard shows a running tone, the room is silent, and the only thing that would ever
            # notice is the watchdog minutes later.
            opened: asyncio.Event = asyncio.Event()
            self._writer = asyncio.ensure_future(
                self._write_loop(fifo_path, pcm, sample_rate, channels, source_id, opened)
            )

            adopted_url: str | None = None
            try:
                try:
                    await asyncio.wait_for(opened.wait(), timeout=WRITER_OPEN_TIMEOUT_S)
                except TimeoutError as exc:
                    raise ToneError("the calibration source never opened its FIFO") from exc
                try:
                    await self._router.route_player(player_id, source_id)
                except Exception as route_error:
                    # An idle third-party speaker is in no unit's `players` and no unit's
                    # `local_player`, so the router cannot resolve it at all — it is only visible
                    # over mDNS, as a URL. Adoption is the way in: it dials the speaker onto this
                    # source directly, and reports back the id its HANDSHAKE gave, which is what the
                    # calibration record must be keyed on (mDNS names by instance, the handshake by
                    # MAC, and the URL moves with DHCP).
                    if not url:
                        raise
                    logger.info(
                        "calibration tone: %s is not routable (%s); adopting %s instead",
                        player_id,
                        route_error,
                        url,
                    )
                    learned = await self._engine.adopt_client(source_id, url)
                    if not learned:
                        raise ToneError(f"could not reach the speaker at {url}") from route_error
                    player_id = learned
                    adopted_url = url
                await self._router.set_volume(player_id, volume, False)
            except Exception as exc:
                # Never leave a half-built session behind: the source would keep a feeder and a
                # writer alive forever, and the GUI would poll a tone that is not audible anywhere.
                # An adopt that landed before a later step failed must still be handed back.
                logger.warning("calibration tone for %s failed to start: %s", player_id, exc)
                if adopted_url is not None:
                    with contextlib.suppress(Exception):
                        await self._engine.release_client(source_id, player_id, url=adopted_url)
                await self._teardown(source_id, fifo_path)
                raise

            self._state = ToneState(
                player_id=player_id,
                source_id=source_id,
                volume=volume,
                tone_type=tone_type,
                started_at=time.monotonic(),
                seconds=seconds,
                restore_source_id=restore_source,
                restore_volume=restore_volume,
                adopted_url=adopted_url,
                fifo_path=fifo_path,
            )
            self._watchdog = asyncio.ensure_future(self._expire_after(seconds))
            logger.info(
                "calibration tone: %s on %s at %d%% for %.0fs (restore: source=%s volume=%s)",
                tone_type,
                player_id,
                volume,
                seconds,
                restore_source or "(none)",
                restore_volume if restore_volume is not None else "(unknown)",
            )
            return self._state.to_dict()

    async def set_volume(self, volume: int) -> dict:
        """Re-level the running tone without restarting it, so the noise does not gap between steps."""
        async with self._lock:
            if self._state is None:
                raise ToneError("no tone is playing")
            volume = max(0, min(100, int(volume)))
            await self._router.set_volume(self._state.player_id, volume, False)
            self._state.volume = volume
            return self._state.to_dict()

    async def stop(self) -> dict:
        async with self._lock:
            await self._stop_locked("stopped by request")
            return {"playing": False}

    async def shutdown(self) -> None:
        """Called on unit shutdown: never leave a speaker playing noise into an empty house."""
        with contextlib.suppress(Exception):
            await self.stop()

    # -- internals ------------------------------------------------------------

    def _current_placement(self, player_id: str) -> tuple[str | None, int | None]:
        """Where this player is and how loud, so the session can put it back.

        Returns (source_id, volume); either may be None when the view does not know — an idle
        speaker is on no source, and a player we have never seen has no level to restore.
        """
        try:
            view = self._view()
        except Exception:  # noqa: BLE001 - a view failure must not block a calibration
            return None, None

        source_id = None
        for unit in view.units:
            for source in unit.sources:
                if source.source_id.startswith(CAL_SOURCE_PREFIX):
                    continue  # never "restore" a player onto a previous calibration tone
                if player_id in source.player_ids:
                    source_id = source.source_id
                    break
            if source_id:
                break

        # Only OUR OWN player echoes `client/state` after a volume change — we made it do so, and
        # the library otherwise sends exactly one, at connect (docs/SPEC-CONFORMANCE.md). So a third
        # party's reported level is its connect-time value and nothing else; "restoring" it would
        # assert a level we never read, and for a speaker that connected at 100 that is a loud
        # surprise at the end of every calibration. Unknown is honest, and _stop_locked already
        # skips the restore for None.
        found = view.find_player(player_id)
        own = view.unit_by_own_player(player_id) is not None
        volume = found[1].volume if (found and own) else None
        return source_id, volume

    async def _write_loop(
        self,
        fifo_path: str,
        pcm: bytes,
        sample_rate: int,
        channels: int,
        source_id: str,
        opened: asyncio.Event,
    ) -> None:
        """Feed the looped tone into the source FIFO in real time until cancelled."""
        fd = None
        try:
            fd = await self._open_writer(fifo_path)
            opened.set()
            frame = _frame_bytes(channels, 16)
            chunk = (sample_rate * _WRITE_CHUNK_MS // 1000) * frame
            period = _WRITE_CHUNK_MS / 1000.0
            offset = 0
            next_due = time.monotonic()

            while True:
                # Wrap around the loop buffer, splicing across the seam so no chunk is short.
                end = offset + chunk
                if end <= len(pcm):
                    data = pcm[offset:end]
                    offset = end % len(pcm)
                else:
                    tail = pcm[offset:]
                    head = pcm[: end - len(pcm)]
                    data = tail + head
                    offset = len(head)

                view = memoryview(data)
                while view:
                    try:
                        written = os.write(fd, view)
                        view = view[written:]
                    except BlockingIOError:
                        await asyncio.sleep(_PIPE_FULL_SLEEP_S)

                # Resync rather than accumulate. Advancing `next_due` unconditionally means any
                # stall — a GC pause, a shairport burst, an encoder spike — leaves it permanently
                # behind wall clock, so the sleep is 0 forever after and the loop degenerates into
                # write-until-the-pipe-fills. It never hard-spins (both sleeps yield) but it never
                # recovers its cadence either, and runs the rest of the session ~360 ms deep.
                now = time.monotonic()
                next_due = max(next_due + period, now)
                await asyncio.sleep(max(0.0, next_due - now))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a writer failure must not take the audio loop down
            logger.exception("[%s] calibration tone writer failed", source_id)
        finally:
            # Closing the write end is what the feeder sees as EOF, which is how the source
            # announces idle immediately instead of sitting "active" for the 300 s timeout.
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)

    async def _open_writer(self, fifo_path: str, timeout: float = 5.0) -> int:
        """Open the FIFO write end, waiting for the feeder's read end to appear.

        O_WRONLY on a FIFO with no reader BLOCKS, which in the audio loop would be fatal; the
        non-blocking form raises ENXIO instead, so this polls until the feeder has opened its end.
        """
        deadline = time.monotonic() + timeout
        while True:
            try:
                return os.open(fifo_path, os.O_WRONLY | os.O_NONBLOCK)
            except FileNotFoundError:
                pass  # the feeder creates the FIFO; it may not have run yet
            except OSError as exc:
                if exc.errno != errno.ENXIO:
                    raise
            if time.monotonic() > deadline:
                raise ToneError("the calibration source never opened its FIFO")
            await asyncio.sleep(0.05)

    async def _expire_after(self, seconds: float) -> None:
        """Stop the tone on its own. A browser that navigates away cannot press Stop."""
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            raise
        async with self._lock:
            if self._state is not None:
                await self._stop_locked(f"expired after {seconds:.0f}s")

    async def _stop_locked(self, why: str) -> None:
        state, self._state = self._state, None
        watchdog, self._watchdog = self._watchdog, None
        # NEVER cancel the task we are running in. `_expire_after` calls this, so on the timeout
        # path `watchdog` IS the current task: `Task.cancel()` on a running task sets a pending
        # cancellation delivered at the next await that actually SUSPENDS — which here is the
        # release/route call below, real network I/O on hardware. CancelledError is a BaseException,
        # so `except Exception` does not catch it, and everything after that point — stopping the
        # source, killing the writer, unlinking the FIFO, restoring the volume — is skipped. The
        # speaker plays pink noise forever; `_state` is already None so a later Stop reports success
        # and does nothing; and the next start leaves TWO writers interleaving chunks into one FIFO.
        # Reproduced in isolation before fixing. The unit tests missed it only because the fake
        # router never suspends, so the pending cancellation was consumed harmlessly.
        if watchdog is not None and watchdog is not asyncio.current_task():
            watchdog.cancel()
        if state is None:
            return

        logger.info("calibration tone on %s: %s", state.player_id, why)

        # Move the player OFF the tone before the source dies, so it lands where it belongs rather
        # than in a group whose feeder has just been torn down.
        try:
            if state.adopted_url is not None:
                # WE dialled this one, so we have to hang up, not merely detach. `unroute_player`
                # drops it from the group and leaves the websocket up — a client holds exactly one,
                # so the speaker would stay captured by us and its real server (Music Assistant)
                # could never take it back. `release_client` is the three-step version: detach,
                # stop dialling, disconnect.
                await self._engine.release_client(state.source_id, state.player_id, url=state.adopted_url)
            elif state.restore_source_id:
                await self._router.route_player(state.player_id, state.restore_source_id)
            else:
                await self._router.unroute_player(state.player_id, state.source_id)
        except Exception as exc:  # noqa: BLE001 - teardown must complete regardless
            logger.warning("could not restore %s after calibration: %s", state.player_id, exc)

        await self._teardown(state.source_id, state.fifo_path)

        if state.restore_volume is not None:
            with contextlib.suppress(Exception):
                await self._router.set_volume(state.player_id, state.restore_volume, False)

    async def _teardown(self, source_id: str, fifo_path: str) -> None:
        if self._writer is not None:
            self._writer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._writer
            self._writer = None
        with contextlib.suppress(Exception):
            await self._engine.stop_source(source_id)
        with contextlib.suppress(OSError):
            os.unlink(fifo_path)


def _fifo_safe(player_id: str) -> str:
    """A player id is a public key; keep the FIFO name to characters a path can hold."""
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in player_id)[:48]
