"""Unit tests for the Bluetooth AVRCP progress/anchor state machine.

Every test here guards a bug that actually shipped to hardware on 2026-07-28 and was INVISIBLE in
logs — the source looked healthy while the GUI timeline was wrong. They are cheap; the hardware
round-trip that found them was not.

The headline one is `test_progress_anchor_requests_a_fresh_timestamp`. `role.update()` does
replace(current_metadata, **kwargs), which copies `timestamp_us`, and `set_metadata` only stamps
anew when it is None — so publishing progress via update() pairs a CURRENT position with an
ANCIENT timestamp and the client double-counts the elapsed time. Symptom: correct for a second,
then a jump forward, compounding after every pause. See airplay_metadata._emit_progress, which
documents the same library quirk.

Needs aiosendspin + dbus-next (real runtime deps); skipped on a bare checkout.

Run: `pytest tests/Unit`.
"""

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "scripts"))

avrcp_mod = pytest.importorskip(
    "sources.bluetooth_avrcp", reason="needs aiosendspin + dbus-next"
)
BluetoothAvrcp = avrcp_mod.BluetoothAvrcp


@dataclass
class FakeMetadata:
    """Stands in for aiosendspin's Metadata (only the fields this code touches).

    Must be a real dataclass: the production code calls dataclasses.replace() on it, which is
    precisely the mechanism that copies `timestamp_us` and caused the bug these tests guard.
    """

    track_progress: int | None = None
    track_duration: int | None = None
    playback_speed: int | None = None
    timestamp_us: int | None = 111_222_333  # a STALE stamp, as a live role would carry


class FakeRole:
    """Records how progress was published: via set_metadata (correct) or update (the bug)."""

    def __init__(self):
        self.metadata = FakeMetadata()
        self.set_metadata_calls = []
        self.update_calls = []

    def set_metadata(self, md):
        self.set_metadata_calls.append(md)
        self.metadata = md

    def update(self, **kwargs):
        self.update_calls.append(kwargs)

    def freeze_progress(self):
        pass

    def set_repeat(self, mode):
        pass

    def set_shuffle(self, shuffle):
        pass


class FakeGroup:
    def __init__(self):
        self.roles = {"metadata": FakeRole(), "controller": FakeRole()}

    def group_role(self, name):
        return self.roles.get(name)


class FakeAdapter:
    bus = None


def _avrcp():
    group = FakeGroup()
    return BluetoothAvrcp(group, "1", FakeAdapter()), group.roles["metadata"]


# -- the timestamp bug ---------------------------------------------------------------------------


def test_progress_anchor_requests_a_fresh_timestamp():
    """timestamp_us MUST be None so the server stamps 'now'; inheriting it double-counts elapsed."""
    avrcp, role = _avrcp()
    avrcp._playing = True
    avrcp._anchor_progress(45_000, 200_000)

    assert role.set_metadata_calls, "progress must be published via set_metadata"
    published = role.set_metadata_calls[-1]
    assert published.timestamp_us is None, (
        "published progress carried a stale timestamp_us; the client will add the already-elapsed "
        "time on top and the timeline will jump forward"
    )
    assert published.track_progress == 45_000
    assert published.track_duration == 200_000


def test_progress_is_never_published_through_role_update():
    """role.update() copies timestamp_us — using it for progress is the bug itself."""
    avrcp, role = _avrcp()
    avrcp._playing = True
    avrcp._anchor_progress(1_000, 200_000)

    progress_keys = {"track_progress", "track_duration", "playback_speed"}
    for call in role.update_calls:
        assert not (progress_keys & set(call)), f"progress went through role.update(): {call}"


# -- speed / pause -------------------------------------------------------------------------------


def test_pause_publishes_speed_zero():
    """Clients extrapolate locally; only an explicit speed 0 stops a paused timeline advancing."""
    avrcp, role = _avrcp()
    avrcp._playing = False
    avrcp._anchor_progress(45_000, 200_000)
    assert role.set_metadata_calls[-1].playback_speed == 0


def test_play_publishes_full_rate():
    avrcp, role = _avrcp()
    avrcp._playing = True
    avrcp._anchor_progress(45_000, 200_000)
    assert role.set_metadata_calls[-1].playback_speed == 1000


def test_anchor_is_skipped_without_a_duration():
    """The role emits progress only as a complete trio; a duration-less anchor would be dropped."""
    avrcp, role = _avrcp()
    avrcp._playing = True
    avrcp._anchor_progress(45_000, 0)
    assert not role.set_metadata_calls


# -- polled-position sanity check ----------------------------------------------------------------


def test_expected_position_advances_only_while_playing():
    avrcp, _role = _avrcp()
    avrcp._playing = True
    avrcp._anchor_progress(10_000, 200_000)
    assert avrcp._expected_position_ms() == pytest.approx(10_000, abs=200)

    # Paused: the anchor is the answer, no elapsed time added.
    avrcp._playing = False
    avrcp._anchor_progress(10_000, 200_000)
    assert avrcp._expected_position_ms() == 10_000


def test_expected_position_is_none_before_any_anchor():
    """No anchor yet means the ticker has nothing to validate against and must not reject blindly."""
    avrcp, _role = _avrcp()
    assert avrcp._expected_position_ms() is None
