"""Playing-vs-paused must not depend on knowing the track duration.

One guard covered two unrelated facts. `_emit_progress` returned early whenever `_duration_ms <= 0`,
so for a sender that never emits `prgr` (no duration, no position) every pbeg/prsm/paus was dropped
on the floor: the transport read *paused* while audio was audibly playing. Pressing the button
appeared to fix it only because that flipped the GUI's own optimistic state — and `apply_command`
was gated on duration too, so even that was inconsistent.

Reported from the rig against Music Assistant's AirPlay sender, 2026-08-14: play/pause stuck at
paused with no progress, while skip/previous worked normally (those are commands, not state).

Position still needs a duration to mean anything. Speed does not.

Run: `pytest tests/Unit/test_airplay_playback_state.py`
"""

import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "scripts"))

from sources.airplay_metadata import AirplayMetadataReader  # noqa: E402


@dataclass
class FakeMetadata:
    """A real dataclass, because production calls dataclasses.replace() on it."""

    track_progress: int = 0
    track_duration: int = 0
    playback_speed: int = 0
    timestamp_us: int | None = 12345


class FakeRole:
    def __init__(self):
        self.metadata = FakeMetadata()

    def set_metadata(self, md):
        self.metadata = md


class FakeGroup:
    def __init__(self):
        self.role = FakeRole()

    def group_role(self, family):
        return self.role if family == "metadata" else None


def _reader():
    r = AirplayMetadataReader(FakeGroup(), "/tmp/does-not-exist-fifo")
    r._duration_ms = 0          # the sender never told us how long the track is
    return r


def test_a_playing_source_reports_playing_without_a_duration():
    r = _reader()
    r._set_playing()
    assert r.group.role.metadata.playback_speed == 1000


def test_a_paused_source_reports_paused_without_a_duration():
    r = _reader()
    r._set_playing()
    r._set_paused()
    assert r.group.role.metadata.playback_speed == 0


def test_an_unknown_duration_still_suppresses_the_position():
    """The other half: we must not invent a position we do not have. Only the speed gets through."""
    r = _reader()
    r._anchor_pos_ms = 42_000
    r._set_playing()
    md = r.group.role.metadata
    assert md.playback_speed == 1000
    assert md.track_duration == 0, "duration is unknown and must stay unknown"
