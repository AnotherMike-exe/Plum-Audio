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
        self.adopted: list[tuple[str, str]] = []
        self.released: list[tuple[str, str, str | None]] = []
        self._readers: dict[str, int] = {}

    adopt_result: str | None = "aa:bb:cc:dd:ee:ff"

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

    async def adopt_client(self, source_id: str, url: str, player_id: str | None = None):
        self.adopted.append((source_id, url))
        return self.adopt_result

    async def release_client(self, source_id: str, player_id: str, url: str | None = None) -> None:
        self.released.append((source_id, player_id, url))

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
    """Resolves a source through a VIEW, as Router.route_player does.

    That detail is the whole point of this fake. The real router looks the source up in the
    aggregated mesh view, which is a 2 s cache — so a source created moments ago is not in it yet
    and the route fails outright. A fake that simply accepted any source id hid that on the rig.
    """

    def __init__(self, view_provider=None):
        self._view = view_provider or (lambda: None)
        self.routes: list[tuple[str, str]] = []
        self.unroutes: list[tuple[str, str]] = []
        self.volumes: list[tuple[str, int, bool]] = []
        self.fail_route_to: str | None = None
        self.fail_volume = False

    async def route_player(self, player_id: str, source_id: str) -> bool:
        # Suspends, like the real thing: routing is network I/O. Without this a pending
        # cancellation is never delivered and a whole class of teardown bug is invisible.
        await asyncio.sleep(0)
        if self.fail_route_to is not None and source_id == self.fail_route_to:
            raise RuntimeError("nope")
        view = self._view()
        if view is not None and not any(
            src.source_id == source_id for unit in view.units for src in unit.sources
        ):
            raise RuntimeError(f"no unit ingests source {source_id!r}")
        self.routes.append((player_id, source_id))
        return True

    async def unroute_player(self, player_id: str, source_id: str) -> None:
        await asyncio.sleep(0)
        self.unroutes.append((player_id, source_id))

    async def set_volume(self, player_id: str, volume: int, muted: bool) -> None:
        await asyncio.sleep(0)
        if self.fail_volume:
            raise RuntimeError("volume failed")
        self.volumes.append((player_id, volume, muted))


def _view(
    *,
    player_id="spk",
    volume=42,
    on_source: str | None = "airplay-1",
    own=True,
    extra_sources: list[str] | None = None,
) -> MeshView:
    """`own=False` models a THIRD-PARTY speaker: attached, but not the unit's own player — so its
    reported volume is its frozen connect-time value, not something we may restore."""
    sources = [
        SourceState(source_id=sid, group_id=sid, group_name=sid, streaming=False, player_ids=[])
        for sid in (extra_sources or [])
    ]
    known = {src.source_id for src in sources}
    # The home source EXISTS whether or not the player is currently on it — sources outlive
    # membership. Modelling it any other way made a restore route fail against a source that, on a
    # real unit, would still be sitting there.
    if "airplay-1" not in known:
        sources.append(
            SourceState(
                source_id="airplay-1",
                group_id="g1",
                group_name="G",
                streaming=True,
                player_ids=[player_id] if on_source == "airplay-1" else [],
                active=True,
            )
        )
    if on_source and on_source != "airplay-1" and on_source not in known:
        sources.append(
            SourceState(
                source_id=on_source,
                group_id="g2",
                group_name=on_source,
                streaming=True,
                player_ids=[player_id],
                active=True,
            )
        )
    unit = UnitSnapshot(
        unit_id="unit-1",
        name="Unit One",
        host="192.168.1.10",
        local_player={"player_id": player_id} if own else None,
        sources=sources,
        players=[
            PlayerState(player_id=player_id, name="Kitchen", connected=True, group_id="g1", volume=volume)
        ],
    )
    return MeshView([unit])


@pytest.fixture
def rig(tmp_path):
    engine = FakeEngine()
    view_holder: dict = {"view": _view(), "kwargs": {}, "refreshes": 0}
    router = FakeRouter(lambda: view_holder["view"])

    def rebuild():
        """Rebuild the view from what the engine actually holds, as DataAggregator.refresh does."""
        view_holder["view"] = _view(extra_sources=list(engine.sources), **view_holder["kwargs"])

    async def refresh():
        view_holder["refreshes"] += 1
        rebuild()

    def set_view(**kwargs):
        """Re-point the base view; the engine's live sources are folded back in on the next refresh."""
        view_holder["kwargs"] = kwargs
        view_holder["view"] = _view(extra_sources=list(engine.sources), **kwargs)

    view_holder["set"] = set_view
    controller = CalibrationToneController(
        engine, router, lambda: view_holder["view"], refresh_view=refresh, fifo_dir=str(tmp_path)
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
    holder["set"](on_source=None)
    await controller.start("spk", 40, seconds=30)
    await controller.stop()
    assert router.unroutes == [("spk", CAL_SOURCE_PREFIX + "spk")]
    assert not any(src == "airplay-1" for _, src in router.routes)


@asyncio_test
async def test_a_previous_calibration_source_is_never_restored_onto(rig):
    """Restoring a speaker onto a dead tone source would leave it silently attached to nothing."""
    controller, _, router, holder = rig
    holder["set"](on_source=CAL_SOURCE_PREFIX + "spk")
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
    holder["set"](volume=30, on_source=CAL_SOURCE_PREFIX + "spk")
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


# -- third-party speakers -----------------------------------------------------
#
# An idle third-party speaker is in no unit's `players` and no unit's `local_player`, so the router
# cannot resolve it at all — it exists only as an mDNS URL. Adoption is the way in, and it is also
# the only moment the URL and the handshake id are both in hand: the record must be keyed on the id
# (mDNS names by instance, the handshake by MAC, and the URL is IP-derived so it moves with DHCP).


@asyncio_test
async def test_an_unroutable_speaker_is_adopted_by_url(rig):
    controller, engine, router, _ = rig
    router.fail_route_to = CAL_SOURCE_PREFIX + "ws://192.168.1.87:8927/sendspin"
    state = await controller.start(
        "ws://192.168.1.87:8927/sendspin", 40, url="ws://192.168.1.87:8927/sendspin", seconds=30
    )
    try:
        assert engine.adopted == [(state["sourceId"], "ws://192.168.1.87:8927/sendspin")]
        # The reply carries the id the HANDSHAKE gave, which is what the calibration record keys on.
        assert state["playerId"] == "aa:bb:cc:dd:ee:ff"
        assert router.volumes[-1] == ("aa:bb:cc:dd:ee:ff", 40, False)
    finally:
        await controller.stop()


@asyncio_test
async def test_an_adopted_speaker_is_handed_back_not_merely_detached(rig):
    """Detaching drops it from the group but leaves the websocket up, and a client holds exactly
    one — so the speaker would stay captured and Music Assistant could never take it back."""
    controller, engine, router, _ = rig
    url = "ws://192.168.1.87:8927/sendspin"
    router.fail_route_to = CAL_SOURCE_PREFIX + url
    await controller.start(url, 40, url=url, seconds=30)
    await controller.stop()

    assert engine.released == [(CAL_SOURCE_PREFIX + url, "aa:bb:cc:dd:ee:ff", url)]
    assert router.unroutes == []


@asyncio_test
async def test_a_failed_adopt_is_reported_not_silently_empty(rig):
    controller, engine, router, _ = rig
    url = "ws://192.168.1.87:8927/sendspin"
    router.fail_route_to = CAL_SOURCE_PREFIX + url
    engine.adopt_result = None
    with pytest.raises(ToneError):
        await controller.start(url, 40, url=url, seconds=30)
    assert engine.sources == {}
    assert controller.status() == {"playing": False}


@asyncio_test
async def test_an_adopt_that_lands_before_a_later_failure_is_still_handed_back(rig):
    controller, engine, router, _ = rig
    url = "ws://192.168.1.87:8927/sendspin"
    router.fail_route_to = CAL_SOURCE_PREFIX + url
    router.fail_volume = True
    with pytest.raises(RuntimeError):
        await controller.start(url, 40, url=url, seconds=30)
    assert engine.released == [(CAL_SOURCE_PREFIX + url, "aa:bb:cc:dd:ee:ff", url)]
    assert engine.sources == {}


@asyncio_test
async def test_an_unroutable_speaker_with_no_url_still_fails(rig):
    """Adoption is only attempted when the caller supplied somewhere to dial."""
    controller, engine, _, _ = rig
    rig_router = rig[2]
    rig_router.fail_route_to = CAL_SOURCE_PREFIX + "spk"
    with pytest.raises(RuntimeError):
        await controller.start("spk", 40, seconds=30)
    assert engine.adopted == []


@asyncio_test
async def test_a_third_party_volume_is_never_restored(rig):
    """Only our own player echoes client/state; anyone else's reported level is its connect-time
    value, so restoring it would assert a level we never read — and for a speaker that connected at
    100 that is a loud surprise at the end of every calibration."""
    controller, _, router, holder = rig
    holder["set"](volume=100, own=False)
    await controller.start("spk", 40, seconds=30)
    await controller.stop()
    assert router.volumes[-1] == ("spk", 40, False), "no restore should have been issued"


@asyncio_test
async def test_our_own_players_volume_is_still_restored(rig):
    controller, _, router, holder = rig
    holder["set"](volume=42, own=True)
    await controller.start("spk", 40, seconds=30)
    await controller.stop()
    assert router.volumes[-1] == ("spk", 42, False)


# -- the expiry path must complete its teardown -------------------------------
#
# `_expire_after` calls `_stop_locked`, so on the timeout path the watchdog is cancelling the task
# it is itself running in. A pending self-cancellation is delivered at the next await that SUSPENDS
# — the release/route call — and CancelledError is a BaseException, so `except Exception` lets it
# past and every later step is skipped. That leaves the writer alive (the speaker plays noise
# forever), the source up, the FIFO on disk, and `_state` already None so a later Stop reports
# success and does nothing.


@asyncio_test
async def test_an_expired_tone_completes_its_whole_teardown(rig, tmp_path):
    controller, engine, router, _ = rig
    await controller.start("spk", 40, seconds=1.0)
    await asyncio.sleep(1.4)

    assert controller.status() == {"playing": False}
    assert engine.stopped == [CAL_SOURCE_PREFIX + "spk"], "the source must be stopped"
    assert engine.sources == {}
    assert list(tmp_path.glob("cal-*-fifo")) == [], "the FIFO must be unlinked"
    assert router.routes[-1] == ("spk", "airplay-1"), "the endpoint must be restored"
    assert router.volumes[-1] == ("spk", 42, False), "the level must be restored"


@asyncio_test
async def test_an_expired_adopted_speaker_is_still_handed_back(rig):
    """The worst version: a cancelled teardown leaves a third-party speaker captured by us, and
    Music Assistant can never take it back."""
    controller, engine, router, _ = rig
    url = "ws://192.168.1.87:8927/sendspin"
    router.fail_route_to = CAL_SOURCE_PREFIX + url
    await controller.start(url, 40, url=url, seconds=1.0)
    await asyncio.sleep(1.4)

    assert engine.released == [(CAL_SOURCE_PREFIX + url, "aa:bb:cc:dd:ee:ff", url)]
    assert engine.stopped == [CAL_SOURCE_PREFIX + url]


@asyncio_test
async def test_the_writer_is_dead_after_an_expiry(rig):
    """The audible symptom: if the writer survives, the speaker keeps making noise."""
    controller, engine, _, _ = rig
    await controller.start("spk", 40, seconds=1.0)
    await asyncio.sleep(1.4)
    assert controller._writer is None or controller._writer.done()


@asyncio_test
async def test_an_adopted_third_party_fifo_is_unlinked(rig, tmp_path):
    """The teardown path must use the FIFO it actually created, not one re-derived from an id that
    adoption replaced."""
    controller, _, router, _ = rig
    url = "ws://192.168.1.87:8927/sendspin"
    router.fail_route_to = CAL_SOURCE_PREFIX + url
    await controller.start(url, 40, url=url, seconds=30)
    assert list(tmp_path.glob("cal-*-fifo")), "a FIFO should exist while the tone runs"
    await controller.stop()
    assert list(tmp_path.glob("cal-*-fifo")) == []


@asyncio_test
async def test_a_source_that_never_opens_its_fifo_fails_the_start(rig, monkeypatch):
    """A tone nobody can hear must not report `playing: true`. The writer's open is the only signal
    that the audio path actually exists on the other end of the FIFO."""
    controller, engine, _, _ = rig
    monkeypatch.setattr(tone_mod, "WRITER_OPEN_TIMEOUT_S", 0.4)
    # A source that creates no reader: O_WRONLY|O_NONBLOCK keeps returning ENXIO.
    engine.start_source = lambda source_id, fifo_path: (
        engine.sources.__setitem__(source_id, fifo_path),
        engine.started.append(source_id),
        os.mkfifo(fifo_path, mode=0o660) if not os.path.exists(fifo_path) else None,
    )
    with pytest.raises(ToneError, match="never opened its FIFO"):
        await controller.start("spk", 40, seconds=30)
    assert controller.status() == {"playing": False}
    assert engine.stopped == [CAL_SOURCE_PREFIX + "spk"]


def test_the_tone_buffer_is_synthesized_once(monkeypatch):
    """Regenerating a deterministic buffer on every Play costs a Pi about a second of GIL each
    time, competing with the feeder's 20 ms commit cadence."""
    tone_mod._TONE_CACHE.clear()
    calls = {"n": 0}
    real = tone_mod.generate_pink

    def counted(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(tone_mod, "generate_pink", counted)
    first = tone_mod.build_tone("pink", 8000, 2)
    second = tone_mod.build_tone("pink", 8000, 2)
    assert calls["n"] == 1
    assert first is second
    tone_mod._TONE_CACHE.clear()


def test_the_cache_keys_on_the_tone_shape(monkeypatch):
    tone_mod._TONE_CACHE.clear()
    sine_a = tone_mod.build_tone("sine", 8000, 2, 1000.0)
    sine_b = tone_mod.build_tone("sine", 8000, 2, 440.0)
    assert sine_a != sine_b, "a different frequency must not reuse the cached buffer"
    tone_mod._TONE_CACHE.clear()


@asyncio_test
async def test_the_new_source_is_published_before_anything_routes_onto_it(rig):
    """Caught on the rig, not here. `Router.route_player` resolves the source through the
    aggregated mesh view, which is a 2 s CACHE — so a source created moments earlier is not in it
    and the route fails with "no unit ingests source 'cal:...'". The tone must publish it first."""
    controller, _, _, holder = rig
    await controller.start("spk", 40, seconds=30)
    try:
        assert holder["refreshes"] >= 1, "the view must be refreshed before routing"
    finally:
        await controller.stop()


@asyncio_test
async def test_without_the_refresh_the_route_would_fail(rig):
    """Pins the mechanism: with the refresh suppressed, the fake router reproduces the rig error."""
    controller, _, _, _ = rig
    controller._refresh_view = None
    with pytest.raises(RuntimeError, match="no unit ingests source"):
        await controller.start("spk", 40, seconds=30)
    assert controller.status() == {"playing": False}
