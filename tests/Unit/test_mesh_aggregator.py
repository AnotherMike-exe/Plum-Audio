"""Unit tests for the mesh DataAggregator: the poll loop's fault reporting.

`_poll_loop` wrapped `refresh()` in `contextlib.suppress(Exception)`. One bug in a snapshot — ours
or a peer's — then froze the mesh view on every unit that polled it, with nothing in any log.

Pure logic: the engine, discovery and the peer fetch are fakes. Run: `pytest tests/Unit`.
"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend" / "scripts"))

from mesh import aggregator as agg_mod  # noqa: E402
from mesh.aggregator import DataAggregator  # noqa: E402
from mesh.model import UnitSnapshot  # noqa: E402


class FakeEngine:
    def snapshot(self) -> UnitSnapshot:
        return UnitSnapshot(unit_id="unit-a", name="A", host=None)


class FakeDiscovery:
    def peers(self):
        return []


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


async def no_fetch(_peer):
    return None


def make_aggregator(**kw) -> DataAggregator:
    return DataAggregator("unit-a", FakeEngine(), FakeDiscovery(), no_fetch, **kw)


def run_poll(agg: DataAggregator, ticks: int, on_tick=None) -> None:
    """Run the poll loop for exactly `ticks` refresh calls, then stop it."""
    calls = {"n": 0}
    real_refresh = agg.refresh

    async def counted():
        calls["n"] += 1
        if on_tick is not None:
            on_tick(calls["n"])
        try:
            return await real_refresh()
        finally:
            if calls["n"] >= ticks:
                agg._stop.set()

    agg.refresh = counted  # type: ignore[method-assign]
    asyncio.run(agg._poll_loop())


def test_a_failing_refresh_is_logged_with_its_traceback(caplog):
    agg = make_aggregator(interval_s=0)

    async def boom():
        agg._stop.set()
        raise RuntimeError("bad snapshot")

    agg.refresh = boom  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR, logger="plum.mesh.aggregator"):
        asyncio.run(agg._poll_loop())
    assert len(caplog.records) == 1
    assert caplog.records[0].exc_info is not None
    assert "bad snapshot" in str(caplog.records[0].exc_info[1])


def test_a_persistent_fault_does_not_flood_the_log(caplog):
    """2 s poll interval: an unbounded log is 43 000 tracebacks a day on an SD card."""
    clock = FakeClock()
    agg = make_aggregator(interval_s=0, clock=clock)
    ticks = {"n": 0}

    async def boom():
        ticks["n"] += 1
        clock.now += 2.0  # one poll interval
        if ticks["n"] >= 100:
            agg._stop.set()
        raise RuntimeError("still broken")

    agg.refresh = boom  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR, logger="plum.mesh.aggregator"):
        asyncio.run(agg._poll_loop())
    # 100 failures over 200 s, one line for each REFRESH_ERROR_LOG_EVERY_S window.
    expected = 1 + int(198 // agg_mod.REFRESH_ERROR_LOG_EVERY_S)
    assert len(caplog.records) == expected
    assert "more failure" in caplog.records[-1].getMessage()  # the repeat count is not lost


def test_recovery_is_logged_and_the_next_fault_is_reported_at_once(caplog):
    clock = FakeClock()
    agg = make_aggregator(interval_s=0, clock=clock)
    script = iter([True, False, True])  # fail, recover, fail again within the rate-limit window

    async def scripted():
        try:
            fail = next(script)
        except StopIteration:
            agg._stop.set()
            return agg.view()
        if fail:
            raise RuntimeError("boom")
        return agg.view()

    agg.refresh = scripted  # type: ignore[method-assign]
    with caplog.at_level(logging.INFO, logger="plum.mesh.aggregator"):
        asyncio.run(agg._poll_loop())
    messages = [(r.levelname, r.getMessage()) for r in caplog.records]
    # The fourth line is the recovery from the second fault, when the script ends.
    assert [level for level, _ in messages] == ["ERROR", "INFO", "ERROR", "INFO"]
    assert "recovered" in messages[1][1]
