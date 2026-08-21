"""Unit tests for LoudnessReconciler — matched loudness across grouped endpoints.

No aiosendspin, no hardware: the aggregator view and the router are fakes and `tick()` is driven
directly, the same shape as test_follow_reconciler.py.

What these mostly guard is restraint. A reconciler that moves speakers is only acceptable if it
moves them for a reason, so the interesting cases are the ones where it must do NOTHING: an
uncalibrated partner, a lone endpoint, a group it has never seen (adopt the state, don't impose
one), a muted room, and — the sharp one — an endpoint currently playing a calibration tone, whose
level is deliberately unrelated to the group's and would otherwise be read as a human moving a
slider and re-level the whole house mid-measurement.

Run: `pytest tests/Unit/test_loudness_reconciler.py`.
"""

import asyncio
import functools
import json
import math
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "scripts"))

from mesh.loudness import LoudnessReconciler  # noqa: E402
from mesh.model import MeshView, PlayerState, SourceState, UnitSnapshot  # noqa: E402


def asyncio_test(fn):
    """This project has no pytest-asyncio; the convention is asyncio.run."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


def samples(offset_db, volumes=(35, 60, 85)):
    """Measurements from a perfect amplitude scaler sitting `offset_db` from reference."""
    return [{"volume": v, "db": 20.0 * math.log10(v) + offset_db} for v in volumes]


class FakeRouter:
    def __init__(self):
        self.calls: list[tuple[str, int]] = []
        self.fail: set[str] = set()

    async def set_volume(self, player_id: str, volume: int, muted: bool) -> None:
        if player_id in self.fail:
            raise RuntimeError("unreachable")
        self.calls.append((player_id, volume))

    def last_for(self, player_id):
        hits = [v for p, v in self.calls if p == player_id]
        return hits[-1] if hits else None


class FakeAggregator:
    def __init__(self, view: MeshView):
        self._view = view

    def view(self) -> MeshView:
        return self._view


def build_view(levels: dict[str, int], *, source_id="airplay-1", follows: dict[str, str] | None = None):
    """One leader unit ingesting `source_id`, with every player in `levels` attached to it.

    Each player lives on its own unit so `follows_unit_id` can be varied per endpoint — which is
    what the default 'follow' scope keys off.
    """
    follows = follows or {}
    leader = UnitSnapshot(
        unit_id="unit-leader",
        name="Living Room",
        host="192.168.1.10",
        local_player={"player_id": "living"},
        sources=[
            SourceState(
                source_id=source_id,
                group_id="g1",
                group_name="G",
                streaming=True,
                player_ids=list(levels),
                active=True,
            )
        ],
        players=[
            PlayerState(player_id=pid, name=pid, connected=True, group_id="g1", volume=vol)
            for pid, vol in levels.items()
        ],
    )
    units = [leader]
    for pid in levels:
        if pid == "living":
            continue
        units.append(
            UnitSnapshot(
                unit_id=f"unit-{pid}",
                name=pid,
                host="192.168.1.11",
                local_player={"player_id": pid},
                follows_unit_id=follows.get(pid, "unit-leader"),
            )
        )
    return MeshView(units)


@pytest.fixture
def rig(tmp_path):
    settings_path = tmp_path / "settings.json"

    def write(calibration: dict, mode="stream", sets=None):
        settings_path.write_text(
            json.dumps(
                {
                    "audio": {
                        "calibration": calibration,
                        "loudnessMatch": {"mode": mode, "sets": sets or []},
                    }
                }
            )
        )

    def make(view: MeshView, tone_player=None):
        router = FakeRouter()
        rec = LoudnessReconciler(
            FakeAggregator(view),
            router,
            local_unit_id="unit-leader",
            settings_file=str(settings_path),
            tone_player_provider=lambda: tone_player,
        )
        return rec, router

    return write, make


# Two rooms: the kitchen is 6 dB less efficient, so it needs double the volume to match.
CALS = {
    "living": {"name": "Living", "samples": samples(40.0)},
    "kitchen": {"name": "Kitchen", "samples": samples(34.0)},
}


# -- the core behaviour -------------------------------------------------------


@asyncio_test
async def test_moving_the_reference_re_levels_the_follower(rig):
    write, make = rig
    write(CALS)
    rec, router = make(build_view({"living": 40, "kitchen": 40}))

    await rec.tick()  # first tick adopts the current state as the baseline
    router.calls.clear()

    rec._aggregator = FakeAggregator(build_view({"living": 40, "kitchen": 40}))
    rec._commanded["living"] = 20  # the view now disagrees with us: a human moved it to 40
    await rec.tick()
    # 6 dB less efficient at 20 dB/decade is exactly a doubling.
    assert router.last_for("kitchen") == pytest.approx(80, abs=1)


@asyncio_test
async def test_raising_the_reference_raises_the_follower(rig):
    write, make = rig
    write(CALS)
    rec, router = make(build_view({"living": 30, "kitchen": 60}))
    await rec.tick()
    router.calls.clear()

    rec._aggregator = FakeAggregator(build_view({"living": 60, "kitchen": 60}))
    await rec.tick()
    assert router.last_for("kitchen") > 60


@asyncio_test
async def test_identical_endpoints_land_on_the_same_volume(rig):
    write, make = rig
    write({"a": {"samples": samples(40.0)}, "b": {"samples": samples(40.0)}})
    rec, router = make(build_view({"a": 50, "b": 20}))
    await rec.tick()
    router.calls.clear()

    rec._aggregator = FakeAggregator(build_view({"a": 50, "b": 20}))
    rec._commanded["a"] = 10  # a human moved "a" to 50
    await rec.tick()
    assert router.last_for("b") == 50


@asyncio_test
async def test_an_endpoint_that_cannot_reach_is_clamped_and_reported(rig):
    write, make = rig
    write({"living": {"samples": samples(50.0)}, "closet": {"samples": samples(20.0)}})
    rec, router = make(build_view({"living": 20, "closet": 20}))
    await rec.tick()
    router.calls.clear()

    rec._aggregator = FakeAggregator(build_view({"living": 95, "closet": 20}))
    await rec.tick()
    assert router.last_for("closet") == 100
    assert "closet" in rec.status()["atLimit"]


@asyncio_test
async def test_a_trim_is_preserved_across_a_re_level(rig):
    write, make = rig
    write(
        {
            "living": {"samples": samples(40.0)},
            "kitchen": {"samples": samples(40.0), "trimDb": -6.0},
        }
    )
    rec, router = make(build_view({"living": 40, "kitchen": 70}))
    await rec.tick()
    router.calls.clear()

    rec._aggregator = FakeAggregator(build_view({"living": 80, "kitchen": 70}))
    await rec.tick()
    # Same curve, 6 dB trim: half the reference's volume.
    assert router.last_for("kitchen") == pytest.approx(40, abs=1)


# -- restraint ----------------------------------------------------------------


@asyncio_test
async def test_the_first_sight_of_a_group_moves_nothing(rig):
    """Adopt the state the user left; never impose one they did not ask for."""
    write, make = rig
    write(CALS)
    rec, router = make(build_view({"living": 40, "kitchen": 55}))
    await rec.tick()
    assert router.calls == []


@asyncio_test
async def test_an_uncalibrated_partner_is_left_alone(rig):
    write, make = rig
    write({"living": {"samples": samples(40.0)}, "kitchen": {"name": "Kitchen"}})
    rec, router = make(build_view({"living": 40, "kitchen": 40}))
    await rec.tick()
    rec._aggregator = FakeAggregator(build_view({"living": 80, "kitchen": 40}))
    await rec.tick()
    assert router.calls == []


@asyncio_test
async def test_an_endpoint_opted_out_is_left_alone(rig):
    write, make = rig
    write(
        {
            "living": {"samples": samples(40.0)},
            "kitchen": {"samples": samples(34.0), "enabled": False},
        }
    )
    rec, router = make(build_view({"living": 40, "kitchen": 40}))
    await rec.tick()
    rec._aggregator = FakeAggregator(build_view({"living": 80, "kitchen": 40}))
    await rec.tick()
    assert router.calls == []


@asyncio_test
async def test_a_lone_endpoint_is_never_touched(rig):
    write, make = rig
    write(CALS)
    rec, router = make(build_view({"living": 40}))
    await rec.tick()
    await rec.tick()
    assert router.calls == []


@asyncio_test
async def test_mode_off_does_nothing(rig):
    write, make = rig
    write(CALS, mode="off")
    rec, router = make(build_view({"living": 40, "kitchen": 40}))
    await rec.tick()
    rec._aggregator = FakeAggregator(build_view({"living": 90, "kitchen": 40}))
    await rec.tick()
    assert router.calls == []


@asyncio_test
async def test_muting_one_room_does_not_mute_the_house(rig):
    write, make = rig
    write(CALS)
    rec, router = make(build_view({"living": 40, "kitchen": 40}))
    await rec.tick()
    router.calls.clear()

    rec._aggregator = FakeAggregator(build_view({"living": 0, "kitchen": 40}))
    await rec.tick()
    assert router.calls == []


@asyncio_test
async def test_a_calibration_tone_never_re_levels_the_group(rig):
    """The sharp one. A tone drives one endpoint to a level unrelated to the group's; reading that
    as a human moving a slider would re-level the whole house mid-measurement."""
    write, make = rig
    write(CALS)
    rec, router = make(build_view({"living": 40, "kitchen": 40}), tone_player="kitchen")
    await rec.tick()
    router.calls.clear()

    rec._aggregator = FakeAggregator(build_view({"living": 40, "kitchen": 85}))
    await rec.tick()
    assert router.calls == []


@asyncio_test
async def test_a_calibration_source_is_never_matched_against(rig):
    write, make = rig
    write(CALS)
    rec, router = make(build_view({"living": 40, "kitchen": 40}, source_id="cal:kitchen"))
    await rec.tick()
    rec._aggregator = FakeAggregator(build_view({"living": 90, "kitchen": 40}, source_id="cal:kitchen"))
    await rec.tick()
    assert router.calls == []


@asyncio_test
async def test_missing_settings_are_survived(rig, tmp_path):
    _, make = rig
    rec, router = make(build_view({"living": 40, "kitchen": 40}))
    rec.settings_file = str(tmp_path / "absent.json")
    await rec.tick()
    assert router.calls == []


@asyncio_test
async def test_an_unreachable_endpoint_does_not_stop_the_others(rig):
    write, make = rig
    write(
        {
            "living": {"samples": samples(40.0)},
            "kitchen": {"samples": samples(34.0)},
            "office": {"samples": samples(34.0)},
        }
    )
    rec, router = make(build_view({"living": 40, "kitchen": 40, "office": 40}))
    router.fail.add("kitchen")
    await rec.tick()
    router.calls.clear()

    rec._aggregator = FakeAggregator(build_view({"living": 80, "kitchen": 40, "office": 40}))
    await rec.tick()
    assert router.last_for("office") is not None


# -- scope --------------------------------------------------------------------


@asyncio_test
async def test_follow_mode_only_touches_rooms_slaved_to_this_unit(rig):
    write, make = rig
    write(
        {
            "living": {"samples": samples(40.0)},
            "kitchen": {"samples": samples(34.0)},
            "office": {"samples": samples(34.0)},
        },
        mode="follow",
    )
    # The kitchen follows the leader; the office followed nobody and merely joined the stream.
    view = build_view({"living": 40, "kitchen": 40, "office": 40}, follows={"office": None})
    rec, router = make(view)
    await rec.tick()
    router.calls.clear()

    rec._aggregator = FakeAggregator(
        build_view({"living": 80, "kitchen": 40, "office": 40}, follows={"office": None})
    )
    await rec.tick()
    assert router.last_for("kitchen") is not None
    assert router.last_for("office") is None


@asyncio_test
async def test_sets_mode_keeps_independent_rooms_independent(rig):
    write, make = rig
    write(
        {
            "living": {"samples": samples(40.0)},
            "kitchen": {"samples": samples(34.0)},
            "office": {"samples": samples(34.0)},
        },
        mode="sets",
        sets=[{"id": "s1", "name": "Open plan", "members": ["living", "kitchen"]}],
    )
    rec, router = make(build_view({"living": 40, "kitchen": 40, "office": 40}))
    await rec.tick()
    router.calls.clear()

    rec._aggregator = FakeAggregator(build_view({"living": 80, "kitchen": 40, "office": 40}))
    await rec.tick()
    assert router.last_for("kitchen") is not None
    assert router.last_for("office") is None


@asyncio_test
async def test_a_stale_group_target_is_forgotten(rig):
    write, make = rig
    write(CALS)
    rec, router = make(build_view({"living": 40, "kitchen": 40}))
    await rec.tick()
    assert rec.status()["targets"]

    rec._aggregator = FakeAggregator(build_view({"living": 40}))
    await rec.tick()
    assert rec.status()["targets"] == {}


@asyncio_test
async def test_an_endpoint_already_at_the_right_level_is_not_re_commanded(rig):
    """Re-sending a level a player already holds churns the mesh for nothing, and every command is
    a websocket round trip to a speaker that may be mid-track."""
    write, make = rig
    write({"a": {"samples": samples(40.0)}, "b": {"samples": samples(40.0)}})
    rec, router = make(build_view({"a": 50, "b": 50}))
    await rec.tick()
    router.calls.clear()

    rec._aggregator = FakeAggregator(build_view({"a": 50, "b": 50}))
    rec._commanded["a"] = 10  # a human moved "a" to 50; "b" is already correct
    await rec.tick()
    assert router.calls == []


@asyncio_test
async def test_a_third_party_speaker_is_matched_under_stream_scope(rig):
    """An adopted ESP32 has no unit and so no follow relationship, but it is a real endpoint and
    must still be matchable when the user has widened the scope."""
    write, make = rig
    write({"living": {"samples": samples(40.0)}, "esp32": {"samples": samples(34.0)}}, mode="stream")
    rec, router = make(build_view({"living": 40, "esp32": 40}))
    await rec.tick()
    router.calls.clear()

    rec._aggregator = FakeAggregator(build_view({"living": 80, "esp32": 40}))
    await rec.tick()
    assert router.last_for("esp32") is not None


# -- endpoints that never echo their level ------------------------------------
#
# A server cannot READ a speaker's volume, only command it (docs/SPEC-CONFORMANCE.md). Our own
# player echoes `client/state` after every change because we made it; a third-party speaker reports
# its connect-time level and never moves. Divergence is this reconciler's signal for "a human moved
# this", so a non-echoing endpoint is a permanent false positive — and being frozen high it wins
# `max(moved)`, which pegs its whole group to its 100% loudness on every tick.


def build_frozen_view(reference_level: int, frozen_at: int = 100):
    """The living room reports honestly; the ESP32 is stuck at its connect-time level."""
    return build_view({"living": reference_level, "esp32": frozen_at})


@asyncio_test
async def test_a_non_echoing_endpoint_never_becomes_the_reference(rig):
    write, make = rig
    write({"living": {"samples": samples(40.0)}, "esp32": {"samples": samples(34.0)}}, mode="stream")
    rec, router = make(build_frozen_view(40))

    await rec.tick()  # seed
    rec._aggregator = FakeAggregator(build_frozen_view(40))
    await rec.tick()  # a human moves the living room; the esp32 is commanded and does not echo
    commanded_once = router.last_for("esp32")
    assert commanded_once is not None

    # Tick repeatedly with the esp32 still reporting 100. It must never drag the group up.
    for _ in range(4):
        rec._aggregator = FakeAggregator(build_frozen_view(40))
        await rec.tick()

    assert router.last_for("living") is None, "the reference must never be driven by a stale echo"
    assert router.last_for("esp32") == commanded_once, "the target must not have drifted"


@asyncio_test
async def test_a_non_echoing_endpoint_does_not_peg_the_group_loud(rig):
    """The concrete damage: frozen at 100, it wins max(moved) and re-derives the target from its
    own full-scale loudness, driving every honest member to its ceiling."""
    write, make = rig
    write({"living": {"samples": samples(40.0)}, "esp32": {"samples": samples(40.0)}}, mode="stream")
    rec, router = make(build_frozen_view(30))
    await rec.tick()

    for _ in range(3):
        rec._aggregator = FakeAggregator(build_frozen_view(30))
        await rec.tick()

    assert "living" not in rec.status()["atLimit"]
    assert router.last_for("living") is None


@asyncio_test
async def test_an_endpoint_that_does_echo_is_trusted_again(rig):
    """The guard must not permanently deafen a well-behaved player: once its echo matches what we
    asked for, moving its slider is user intent again."""
    write, make = rig
    write({"living": {"samples": samples(40.0)}, "kitchen": {"samples": samples(40.0)}}, mode="stream")
    rec, router = make(build_view({"living": 40, "kitchen": 70}))
    await rec.tick()

    rec._aggregator = FakeAggregator(build_view({"living": 80, "kitchen": 70}))
    await rec.tick()
    echoed = router.last_for("kitchen")
    assert echoed is not None

    # The kitchen echoes the level we commanded — it is trusted from here on.
    rec._aggregator = FakeAggregator(build_view({"living": 80, "kitchen": echoed}))
    await rec.tick()
    assert "kitchen" not in rec._unconfirmed

    # Now a human moves the kitchen; it must be honoured as the new reference.
    router.calls.clear()
    rec._aggregator = FakeAggregator(build_view({"living": 80, "kitchen": 20}))
    await rec.tick()
    assert router.last_for("living") is not None


@asyncio_test
async def test_lag_between_command_and_echo_is_not_read_as_a_human(rig):
    """Same rule covers the benign case: a command that has not landed yet leaves a STALE reported
    level, which is not a deliberate act and must not re-derive the group."""
    write, make = rig
    write({"living": {"samples": samples(40.0)}, "kitchen": {"samples": samples(34.0)}}, mode="stream")
    rec, router = make(build_view({"living": 40, "kitchen": 40}))
    await rec.tick()

    rec._aggregator = FakeAggregator(build_view({"living": 80, "kitchen": 40}))
    await rec.tick()
    router.calls.clear()

    # The kitchen was commanded but the view still shows the old level — mid-flight, not a human.
    rec._aggregator = FakeAggregator(build_view({"living": 80, "kitchen": 40}))
    await rec.tick()
    assert router.last_for("living") is None


# -- stale per-player state ---------------------------------------------------


@asyncio_test
async def test_an_endpoint_that_leaves_and_rejoins_does_not_hijack_the_group(rig):
    """`_targets` was pruned when a group dissolved but `_commanded` was not. So an endpoint that
    left, was turned up by hand while solo, and rejoined looked like "a human just moved this" the
    instant it came back — and dragged the whole group up to itself."""
    write, make = rig
    write({"living": {"samples": samples(40.0)}, "kitchen": {"samples": samples(40.0)}}, mode="stream")
    rec, router = make(build_view({"living": 40, "kitchen": 40}))
    await rec.tick()  # seed: both remembered at 40

    # The kitchen leaves the group entirely.
    rec._aggregator = FakeAggregator(build_view({"living": 40}))
    await rec.tick()
    assert "kitchen" not in rec._commanded, "per-player memory must be dropped with the group"

    # It is turned up by hand while solo, then rejoins.
    router.calls.clear()
    rec._aggregator = FakeAggregator(build_view({"living": 40, "kitchen": 90}))
    await rec.tick()
    assert router.last_for("living") is None, "the joiner must not re-derive the group's target"


@asyncio_test
async def test_a_groups_target_survives_a_member_leaving(rig):
    """The group key must not churn with membership: it is keyed on the partition label, not on a
    member id, so toning one endpoint does not make the rest forget their level."""
    write, make = rig
    write(
        {
            "living": {"samples": samples(40.0)},
            "kitchen": {"samples": samples(40.0)},
            "office": {"samples": samples(40.0)},
        },
        mode="stream",
    )
    rec, _ = make(build_view({"living": 40, "kitchen": 40, "office": 40}))
    await rec.tick()
    before = dict(rec.status()["targets"])
    assert before

    # The office is pulled out (as a calibration tone would do); the remaining group keeps its key.
    rec._aggregator = FakeAggregator(build_view({"living": 40, "kitchen": 40}))
    await rec.tick()
    assert list(rec.status()["targets"]) == list(before)


# -- no unbounded re-issue ----------------------------------------------------


@asyncio_test
async def test_a_level_already_asked_for_is_not_asked_for_again(rig):
    """An endpoint whose echo never arrives would otherwise be commanded every 2 s for as long as
    the group existed — unbounded traffic to a speaker that is already where we want it."""
    write, make = rig
    write({"living": {"samples": samples(40.0)}, "esp32": {"samples": samples(34.0)}}, mode="stream")
    rec, router = make(build_frozen_view(40))
    await rec.tick()
    rec._aggregator = FakeAggregator(build_frozen_view(40))
    await rec.tick()
    issued = len([c for c in router.calls if c[0] == "esp32"])
    assert issued >= 1

    for _ in range(4):
        rec._aggregator = FakeAggregator(build_frozen_view(40))
        await rec.tick()
    assert len([c for c in router.calls if c[0] == "esp32"]) == issued, "must not re-issue"


@asyncio_test
async def test_a_target_change_still_re_sends(rig):
    """The suppression must not stick: a new target means a new wanted level."""
    write, make = rig
    write({"living": {"samples": samples(40.0)}, "esp32": {"samples": samples(34.0)}}, mode="stream")
    rec, router = make(build_frozen_view(30))
    await rec.tick()
    rec._aggregator = FakeAggregator(build_frozen_view(30))
    await rec.tick()
    first = router.last_for("esp32")

    # 45 rather than something that resolves to the frozen 100 — that would let an "already there"
    # check pass against a report we do not trust, which is a separate bug this must not mask.
    rec._aggregator = FakeAggregator(build_frozen_view(45))
    await rec.tick()
    assert router.last_for("esp32") != first


# -- settings are not re-parsed every tick ------------------------------------


@asyncio_test
async def test_settings_are_only_re_read_when_they_change(rig, tmp_path):
    """This runs on the audio event loop, so an avoidable parse competes with the feeder's 20 ms
    commit cadence on a busy SD card."""
    write, make = rig
    write(CALS, mode="stream")
    rec, _ = make(build_view({"living": 40, "kitchen": 40}))

    reads = {"n": 0}
    real_open = open

    def counting_open(path, *a, **k):
        if str(path) == rec.settings_file:
            reads["n"] += 1
        return real_open(path, *a, **k)

    import builtins

    builtins.open = counting_open
    try:
        await rec.tick()
        await rec.tick()
        await rec.tick()
    finally:
        builtins.open = real_open
    assert reads["n"] == 1


@asyncio_test
async def test_a_frozen_report_never_counts_as_already_there(rig):
    """An endpoint whose echo never arrives reports its connect-time level forever. If the target
    happens to resolve to that same number, comparing against the report would skip a command the
    speaker actually needs — it is sitting wherever we last put it, not where it claims."""
    write, make = rig
    write({"living": {"samples": samples(40.0)}, "esp32": {"samples": samples(40.0)}}, mode="stream")
    rec, router = make(build_frozen_view(30, frozen_at=100))
    await rec.tick()
    rec._aggregator = FakeAggregator(build_frozen_view(30, frozen_at=100))
    await rec.tick()
    assert router.last_for("esp32") == 30  # driven down to match the living room

    # Now the target resolves to exactly the value the esp32 has been claiming all along.
    rec._aggregator = FakeAggregator(build_frozen_view(100, frozen_at=100))
    await rec.tick()
    assert router.last_for("esp32") == 100, "must command it, not trust the frozen report"
