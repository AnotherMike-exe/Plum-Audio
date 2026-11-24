"""Unit tests for the mesh DataAggregator: the poll loop's fault reporting.

`_poll_loop` wrapped `refresh()` in `contextlib.suppress(Exception)`. One bug in a snapshot — ours
or a peer's — then froze the mesh view on every unit that polled it, with nothing in any log.

Pure logic: the engine, discovery and the peer fetch are fakes. Run: `pytest tests/Unit`.
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

import pytest

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


# --- the player self-report goes stale ---------------------------------------
#
# The report was stored with no timestamp, so a dead player read `attached: true` for ever.

REPORT = {
    "player_id": "peer-key",
    "name": "Kitchen",
    "url": "ws://192.168.7.10:8928/sendspin",
    "attached": True,
    "playing": True,
    "group_id": "g1",
    "server_id": "srv",
}


def test_a_fresh_self_report_is_published_as_sent_with_its_age():
    clock = FakeClock()
    agg = make_aggregator(clock=clock)
    agg.set_local_player_state(dict(REPORT))
    clock.now += 4.0
    snap = agg.local_snapshot()
    assert snap.local_player == REPORT
    assert snap.local_player_age_s == 4.0
    assert snap.local_player_offline is False


def test_a_stale_self_report_stops_claiming_the_player_is_attached():
    clock = FakeClock()
    agg = make_aggregator(clock=clock)
    agg.set_local_player_state(dict(REPORT))
    clock.now += agg_mod.LOCAL_PLAYER_STALE_S + 1
    snap = agg.local_snapshot()
    assert snap.local_player_offline is True
    assert snap.local_player["attached"] is False
    assert snap.local_player["playing"] is False
    assert snap.local_player["offline"] is True
    # Identity survives: the GUI names the speaker by it and the router dials it back by the URL.
    assert snap.local_player["player_id"] == "peer-key"
    assert snap.local_player["url"] == REPORT["url"]


def test_a_player_that_reports_again_is_online_again():
    clock = FakeClock()
    agg = make_aggregator(clock=clock)
    agg.set_local_player_state(dict(REPORT))
    clock.now += 60
    assert agg.local_snapshot().local_player_offline is True
    agg.set_local_player_state(dict(REPORT))
    snap = agg.local_snapshot()
    assert snap.local_player_offline is False
    assert snap.local_player == REPORT


def test_a_player_that_never_reported_is_unknown_not_offline():
    """Boot, and every unit with no player: None must not read as a dead speaker."""
    snap = make_aggregator().local_snapshot()
    assert snap.local_player is None
    assert snap.local_player_age_s is None
    assert snap.local_player_offline is False


def test_a_snapshot_from_an_older_image_reads_as_unknown_not_offline():
    """The `has_player` precedent: a missing field must take the harmless default."""
    old = {"unit_id": "unit-b", "name": "B", "local_player": dict(REPORT)}
    snap = UnitSnapshot.from_dict(old)
    assert snap.local_player_offline is False
    assert snap.local_player_age_s is None
    assert snap.health is None
    assert snap.health_age_s is None
    assert snap.local_player["attached"] is True


def test_the_new_fields_survive_the_wire():
    snap = UnitSnapshot(
        "u", "U", None, local_player_age_s=2.5, local_player_offline=True, health={"ok": True}, health_age_s=9.0
    )
    back = UnitSnapshot.from_dict(snap.to_dict())
    assert (back.local_player_age_s, back.local_player_offline, back.health, back.health_age_s) == (
        2.5,
        True,
        {"ok": True},
        9.0,
    )


def test_a_peer_cannot_break_the_view_with_a_bad_health_or_age_value():
    snap = UnitSnapshot.from_dict(
        {"unit_id": "u", "health": "fine", "health_age_s": "soon", "local_player_age_s": True}
    )
    assert snap.health is None
    assert snap.health_age_s is None
    assert snap.local_player_age_s is None


# --- /data/health.json -------------------------------------------------------


def test_no_health_file_means_unknown(tmp_path):
    agg = make_aggregator(health_file=str(tmp_path / "health.json"))
    snap = agg.local_snapshot()
    assert snap.health is None
    assert snap.health_age_s is None


def test_a_health_file_is_published_verbatim_with_unknown_keys(tmp_path):
    path = tmp_path / "health.json"
    path.write_text('{"status": "ok", "a_key_nobody_defined_yet": [1, 2]}')
    agg = make_aggregator(health_file=str(path))
    snap = agg.local_snapshot()
    assert snap.health == {"status": "ok", "a_key_nobody_defined_yet": [1, 2]}
    assert snap.health_age_s is not None and snap.health_age_s < 60


def test_a_damaged_health_file_means_unknown(tmp_path):
    for content in ("{not json", "[1, 2, 3]", "", "x" * (agg_mod.HEALTH_MAX_BYTES + 1)):
        path = tmp_path / "health.json"
        path.write_text(content)
        agg = make_aggregator(health_file=str(path))
        assert agg.local_snapshot().health is None, content[:20]


def test_the_health_file_is_not_read_on_every_snapshot(tmp_path):
    path = tmp_path / "health.json"
    path.write_text('{"status": "ok"}')
    clock = FakeClock()
    agg = make_aggregator(clock=clock, health_file=str(path))
    assert agg.local_snapshot().health == {"status": "ok"}
    path.write_text('{"status": "bad"}')
    assert agg.local_snapshot().health == {"status": "ok"}  # cached
    clock.now += agg_mod.HEALTH_REREAD_S + 0.1
    assert agg.local_snapshot().health == {"status": "bad"}


# --- health_age_s comes from the verdict, not from the file -----------------------------------------


def test_the_health_age_is_read_from_checked_at(tmp_path):
    """The mtime moves with a copy or a restore. `checked_at` is when the check last ran."""
    import time

    path = tmp_path / "health.json"
    path.write_text(json.dumps({"healthy": True, "checked_at": time.time() - 250}))  # written just now
    age = make_aggregator(health_file=str(path)).local_snapshot().health_age_s
    assert age is not None and 249 <= age <= 260


@pytest.mark.parametrize("checked_at", ["soon", None, True, -5, 0, float("nan"), float("inf"), 4102444800, [1]])
def test_a_checked_at_that_is_not_a_sane_time_falls_back_to_the_mtime(tmp_path, checked_at):
    import os
    import time

    path = tmp_path / "health.json"
    path.write_text(json.dumps({"healthy": True, "checked_at": checked_at}))
    then = time.time() - 100
    os.utime(path, (then, then))
    age = make_aggregator(health_file=str(path)).local_snapshot().health_age_s
    assert age is not None and 99 <= age <= 110
