"""Unit tests for the calibration tone: synthesis, and the session's restore contract.

No aiosendspin, no audio hardware. The engine and router are fakes; the FIFO is real, because the
writer's non-blocking open-and-wait against a reader that does not exist yet is exactly the part
that would deadlock the audio loop if it were wrong.

The restore contract is what these mostly guard. Playing the tone ROUTES the endpoint, which pulls
it off whatever it was playing — so a session that forgets where the speaker was, or restores it to
the tone's own volume, silently rearranges the user's house every time they calibrate. The
`carries_the_restore_record_forward` test is the subtle one: the wizard restarts the tone once per
measurement, so re-reading a lagging mesh view for "what to go back to" compounds the error.

Run: `pytest tests/Unit/test_calibration_tone.py`.
"""

import asyncio
import functools
import os
import struct
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "scripts"))

import calibration_tone as tone_mod  # noqa: E402
from calibration_tone import (  # noqa: E402
    CAL_SOURCE_PREFIX,
    CalibrationToneController,
    ToneError,
    build_tone,
    generate_pink,
    generate_sine,
)
from mesh.model import MeshView, PlayerState, SourceState, UnitSnapshot  # noqa: E402


def asyncio_test(fn):
    """Run an async test body. This project has no pytest-asyncio; the convention is asyncio.run."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


# -- synthesis ----------------------------------------------------------------


def test_pink_noise_is_identical_every_time():
    """Every sample in a calibration must hear the same signal, or the differences measured are
    partly differences in the noise."""
    assert generate_pink(8000, 2, 0.2, 0.6) == generate_pink(8000, 2, 0.2, 0.6)


def test_pink_noise_hits_its_target_peak_without_clipping():
    raw = generate_pink(8000, 1, 0.5, 0.6)
    peaks = [abs(v) for (v,) in struct.iter_unpack("<h", raw)]
    assert max(peaks) == pytest.approx(0.6 * 32767, rel=0.02)
    assert max(peaks) < 32767


def test_pink_noise_is_not_silence():
    raw = generate_pink(8000, 1, 0.5, 0.6)
    values = [v for (v,) in struct.iter_unpack("<h", raw)]
    assert sum(abs(v) for v in values) / len(values) > 500


def test_sine_is_stereo_and_whole_cycles():
    raw = generate_sine(8000, 2, 0.5, 1000.0, 0.35)
    frames = len(raw) // 4
    # Whole cycles at 1 kHz in 8 kHz means a frame count divisible by 8.
    assert frames % 8 == 0
    first, second = struct.unpack("<hh", raw[:4])
    assert first == second  # both channels carry the same signal


def test_sine_respects_its_peak():
    raw = generate_sine(8000, 1, 0.5, 1000.0, 0.35)
    peaks = [abs(v) for (v,) in struct.iter_unpack("<h", raw)]
    assert max(peaks) == pytest.approx(0.35 * 32767, rel=0.02)


def test_build_tone_dispatches_on_type():
    assert build_tone("sine", 8000, 1, 1000.0) == generate_sine(
        8000, 1, tone_mod.LOOP_SECONDS, 1000.0, tone_mod.SINE_PEAK
    )
    assert build_tone("pink", 8000, 1) == generate_pink(8000, 1, tone_mod.LOOP_SECONDS, tone_mod.PINK_PEAK)


# -- fakes --------------------------------------------------------------------


class FakeEngine:
    """Stands in for the audio engine, and drains the FIFO the way a real SourceFeeder does."""

    def __init__(self):
        self.sources: dict[str, str] = {}
        self.started: list[str] = []
        self.stopped: list[str] = []
        self._readers: dict[str, int] = {}

    def start_source(self, source_id: str, fifo_path: str) -> None:
        self.sources[source_id] = fifo_path
        self.started.append(source_id)
        if not os.path.exists(fifo_path):
            os.mkfifo(fifo_path, mode=0o660)
        # O_RDONLY|O_NONBLOCK returns immediately with no writer — the real feeder's trick.
        self._readers[source_id] = os.open(fifo_path, os.O_RDONLY | os.O_NONBLOCK)

    async def stop_source(self, source_id: str) -> None:
        self.sources.pop(source_id, None)
        self.stopped.append(source_id)
        fd = self._readers.pop(source_id, None)
        if fd is not None:
            os.close(fd)

    def drain(self, source_id: str) -> int:
        """Read whatever is buffered, so the writer never blocks on a full pipe."""
        fd = self._readers.get(source_id)
        if fd is None:
            return 0
        total = 0
        while True:
            try:
                data = os.read(fd, 65536)
            except BlockingIOError:
                return total
            if not data:
                return total
            total += len(data)


class FakeRouter:
    def __init__(self):
        self.routes: list[tuple[str, str]] = []
        self.unroutes: list[tuple[str, str]] = []
        self.volumes: list[tuple[str, int, bool]] = []
        self.fail_route_to: str | None = None

    async def route_player(self, player_id: str, source_id: str) -> bool:
        if self.fail_route_to is not None and source_id == self.fail_route_to:
            raise RuntimeError("nope")
        self.routes.append((player_id, source_id))
        return True

    async def unroute_player(self, player_id: str, source_id: str) -> None:
        self.unroutes.append((player_id, source_id))

    async def set_volume(self, player_id: str, volume: int, muted: bool) -> None:
        self.volumes.append((player_id, volume, muted))


def _view(*, player_id="spk", volume=42, on_source: str | None = "airplay-1") -> MeshView:
    sources = []
    if on_source:
        sources.append(
            SourceState(
                source_id=on_source,
                group_id="g1",
                group_name="G",
                streaming=True,
                player_ids=[player_id],
                active=True,
            )
        )
    unit = UnitSnapshot(
        unit_id="unit-1",
        name="Unit One",
        host="192.168.1.10",
        sources=sources,
        players=[
            PlayerState(player_id=player_id, name="Kitchen", connected=True, group_id="g1", volume=volume)
        ],
    )
    return MeshView([unit])


@pytest.fixture
def rig(tmp_path):
    engine, router = FakeEngine(), FakeRouter()
    view_holder = {"view": _view()}
    controller = CalibrationToneController(
        engine, router, lambda: view_holder["view"], fifo_dir=str(tmp_path)
    )
    return controller, engine, router, view_holder


# -- starting -----------------------------------------------------------------


@asyncio_test
async def test_starting_creates_a_source_routes_the_player_and_sets_the_volume(rig):
    controller, engine, router, _ = rig
    state = await controller.start("spk", 40, seconds=30)
    try:
        assert state["playing"] is True
        assert state["volume"] == 40
        assert engine.started == [CAL_SOURCE_PREFIX + "spk"]
        assert router.routes == [("spk", CAL_SOURCE_PREFIX + "spk")]
        assert router.volumes[-1] == ("spk", 40, False)
    finally:
        await controller.stop()


@asyncio_test
async def test_the_tone_actually_reaches_the_fifo(rig):
    """The whole point: audio must flow into the source, not to a local ALSA device."""
    controller, engine, _, _ = rig
    await controller.start("spk", 40, seconds=30)
    try:
        await asyncio.sleep(0.25)
        assert engine.drain(CAL_SOURCE_PREFIX + "spk") > 0
    finally:
        await controller.stop()


@asyncio_test
async def test_status_is_quiet_before_and_after(rig):
    controller, *_ = rig
    assert controller.status() == {"playing": False}
    await controller.start("spk", 40, seconds=30)
    assert controller.status()["playing"] is True
    await controller.stop()
    assert controller.status() == {"playing": False}


@pytest.mark.parametrize("bad", [{"player_id": ""}, {"tone_type": "sweep"}])
@asyncio_test
async def test_bad_requests_are_refused(rig, bad):
    controller, *_ = rig
    with pytest.raises(ToneError):
        await controller.start(bad.get("player_id", "spk"), 40, tone_type=bad.get("tone_type", "pink"))


@asyncio_test
async def test_volume_and_duration_are_clamped(rig):
    controller, _, router, _ = rig
    state = await controller.start("spk", 500, seconds=99999)
    try:
        assert state["volume"] == 100
        assert router.volumes[-1][1] == 100
        assert state["remaining"] <= tone_mod.MAX_TONE_SECONDS
    finally:
        await controller.stop()


# -- restore ------------------------------------------------------------------


@asyncio_test
async def test_stopping_puts_the_endpoint_back_where_it_was(rig):
    controller, _, router, _ = rig
    await controller.start("spk", 40, seconds=30)
    await controller.stop()
    assert router.routes[-1] == ("spk", "airplay-1")
    assert router.volumes[-1] == ("spk", 42, False)


@asyncio_test
async def test_an_idle_endpoint_is_returned_to_idle_not_to_a_source(rig):
    controller, _, router, holder = rig
    holder["view"] = _view(on_source=None)
    await controller.start("spk", 40, seconds=30)
    await controller.stop()
    assert router.unroutes == [("spk", CAL_SOURCE_PREFIX + "spk")]
    assert not any(src == "airplay-1" for _, src in router.routes)


@asyncio_test
async def test_a_previous_calibration_source_is_never_restored_onto(rig):
    """Restoring a speaker onto a dead tone source would leave it silently attached to nothing."""
    controller, _, router, holder = rig
    holder["view"] = _view(on_source=CAL_SOURCE_PREFIX + "spk")
    await controller.start("spk", 40, seconds=30)
    await controller.stop()
    assert router.unroutes  # treated as "was idle", not "was on the tone"


@asyncio_test
async def test_carries_the_restore_record_forward_across_a_restart(rig):
    """The wizard restarts the tone once per measurement. The mesh view lags, so re-reading it
    would capture the TONE's volume as 'what to go back to' and compound across a calibration."""
    controller, _, router, holder = rig
    await controller.start("spk", 30, seconds=30)
    # The aggregator now reports the tone's own placement and level, as it really would.
    holder["view"] = _view(volume=30, on_source=CAL_SOURCE_PREFIX + "spk")
    await controller.start("spk", 60, seconds=30)
    await controller.start("spk", 85, seconds=30)
    await controller.stop()
    assert router.routes[-1] == ("spk", "airplay-1")
    assert router.volumes[-1] == ("spk", 42, False)


@asyncio_test
async def test_stopping_when_nothing_plays_is_harmless(rig):
    controller, _, router, _ = rig
    assert await controller.stop() == {"playing": False}
    assert router.routes == []


# -- failure and expiry -------------------------------------------------------


@asyncio_test
async def test_a_failed_route_leaves_no_source_behind(rig):
    """A half-built session would keep a feeder and a writer alive forever."""
    controller, engine, router, _ = rig
    router.fail_route_to = CAL_SOURCE_PREFIX + "spk"
    with pytest.raises(RuntimeError):
        await controller.start("spk", 40, seconds=30)
    assert engine.sources == {}
    assert engine.stopped == [CAL_SOURCE_PREFIX + "spk"]
    assert controller.status() == {"playing": False}


@asyncio_test
async def test_the_tone_expires_on_its_own(rig):
    """A browser that navigates away cannot press Stop, and an unattended speaker playing noise
    is how you lose a rig for an afternoon."""
    controller, _, router, _ = rig
    await controller.start("spk", 40, seconds=1.0)
    await asyncio.sleep(1.4)
    assert controller.status() == {"playing": False}
    assert router.routes[-1] == ("spk", "airplay-1")


@asyncio_test
async def test_the_fifo_is_cleaned_up(rig, tmp_path):
    controller, *_ = rig
    await controller.start("spk", 40, seconds=30)
    await controller.stop()
    assert list(tmp_path.glob("cal-*-fifo")) == []


# -- re-levelling -------------------------------------------------------------


@asyncio_test
async def test_set_volume_relevels_without_restarting_the_source(rig):
    """Restarting between steps would gap the noise while the meter is integrating."""
    controller, engine, router, _ = rig
    await controller.start("spk", 40, seconds=30)
    try:
        state = await controller.set_volume(75)
        assert state["volume"] == 75
        assert router.volumes[-1] == ("spk", 75, False)
        assert engine.started == [CAL_SOURCE_PREFIX + "spk"]  # not restarted
    finally:
        await controller.stop()


@asyncio_test
async def test_set_volume_with_no_tone_is_an_error(rig):
    controller, *_ = rig
    with pytest.raises(ToneError):
        await controller.set_volume(50)


@asyncio_test
async def test_shutdown_never_leaves_a_speaker_playing(rig):
    controller, _, router, _ = rig
    await controller.start("spk", 40, seconds=30)
    await controller.shutdown()
    assert controller.status() == {"playing": False}
    assert router.routes[-1] == ("spk", "airplay-1")
