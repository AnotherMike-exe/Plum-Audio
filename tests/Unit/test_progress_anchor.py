"""Every progress emit must carry a FRESH server timestamp.

Sendspin progress is an anchor, not a ticker: a source publishes `(track_progress, timestamp_us,
playback_speed)` and every consumer computes `progress + (now - timestamp) * speed`. So a stale
timestamp paired with a current position makes the client add all the elapsed time since that stamp
on top of a position that was already current.

aiosendspin makes this easy to get wrong. `role.update()` does `replace(current, **kwargs)`, which
COPIES the existing `timestamp_us`, and `set_metadata` only stamps anew when it is None — so the
only way to emit a clean "(position @ now)" pair is to pass `timestamp_us=None` explicitly. See
docs/UPSTREAM-AIOSENDSPIN.md §2.

AirPlay and Bluetooth both do this and document it at length. Spotify did not, and the symptom
matched the Bluetooth comment's prediction word for word: correct while playing, correct while
paused (speed 0 freezes the client at track_progress), then a jump forward ON RESUME by the length
of the pause — several seconds, or straight to 100% after a long one. Reported from the rig
2026-08-05.

This tests the rule across ALL sources rather than one, because it is a rule about the library and
the next handler will meet it too.

Run: `pytest tests/Unit/test_progress_anchor.py`.
"""

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "scripts"))

pytest.importorskip("aiosendspin", reason="the source handlers import aiosendspin models")

from sources.spotify_golibrespot import SpotifyGoLibrespot  # noqa: E402


@dataclass
class FakeMetadata:
    """Stands in for the role's current metadata — a REAL dataclass, because the code under test
    calls dataclasses.replace() on it and that is precisely the call that copies timestamp_us."""

    track_progress: int = 0
    track_duration: int = 0
    playback_speed: int = 0
    timestamp_us: int | None = 111_111  # an OLD stamp, as a live role would carry


class FakeRole:
    def __init__(self):
        self.metadata = FakeMetadata()
        self.set_calls: list = []
        self.update_calls: list = []

    def set_metadata(self, md):
        self.set_calls.append(md)
        self.metadata = md

    def update(self, **kw):
        self.update_calls.append(kw)


class FakeGroup:
    def __init__(self, role):
        self._role = role

    def group_role(self, name):
        return self._role if name == "metadata" else None


def spotify():
    role = FakeRole()
    src = SpotifyGoLibrespot(FakeGroup(role), "1", "http://127.0.0.1:3678")
    return src, role


def test_spotify_clears_the_timestamp_so_the_server_stamps_now():
    """The regression: an inherited stamp is what made resume jump forward."""
    src, role = spotify()
    src._playing = True
    src._anchor_progress(30_000, 200_000)

    assert role.set_calls, "must go through set_metadata, not update()"
    assert role.set_calls[-1].timestamp_us is None, "timestamp must be cleared so the server re-stamps"


def test_spotify_does_not_use_role_update_for_progress():
    """update() copies the previous timestamp — the whole bug in one call."""
    src, role = spotify()
    src._playing = True
    src._anchor_progress(30_000, 200_000)
    assert role.update_calls == []


def test_the_position_and_duration_still_get_through():
    src, role = spotify()
    src._playing = True
    src._anchor_progress(30_000, 200_000)
    md = role.set_calls[-1]
    assert (md.track_progress, md.track_duration, md.playback_speed) == (30_000, 200_000, 1000)


def test_a_paused_anchor_freezes_at_the_reported_position():
    src, role = spotify()
    src._playing = False
    src._anchor_progress(30_000, 200_000)
    md = role.set_calls[-1]
    assert md.playback_speed == 0
    assert md.track_progress == 30_000
    assert md.timestamp_us is None


def test_resume_after_a_pause_re_stamps_rather_than_accumulating():
    """Pause then resume: BOTH emits must clear the stamp, or the second one races ahead."""
    src, role = spotify()
    src._playing = True
    src._anchor_progress(30_000, 200_000)

    src._playing = False
    src._anchor_progress(45_000, 200_000)   # pause
    src._playing = True
    src._anchor_progress(45_000, 200_000)   # resume at the same spot

    assert [md.timestamp_us for md in role.set_calls] == [None, None, None]
    assert role.set_calls[-1].track_progress == 45_000, "resume must anchor where the pause left off"


def test_negative_or_absent_values_are_handled():
    src, role = spotify()
    src._playing = True
    src._anchor_progress(-5, 200_000)
    assert role.set_calls[-1].track_progress == 0

    before = len(role.set_calls)
    src._anchor_progress(None, 200_000)
    src._anchor_progress(30_000, None)
    assert len(role.set_calls) == before, "an incomplete trio must emit nothing"


@pytest.mark.parametrize("module,symbol", [
    ("sources.airplay_metadata", "AirplayMetadataReader"),
    ("sources.bluetooth_avrcp", "BluetoothAvrcp"),
])
def test_the_other_handlers_still_clear_the_timestamp(module, symbol):
    """A source-level guard: every progress emitter must mention timestamp_us=None.

    Crude on purpose — it is a grep, not a behavioural assertion — but it fails loudly if someone
    'simplifies' one of these back to role.update(), which is exactly how Spotify ended up wrong.
    """
    pytest.importorskip(module.split(".")[-1] and "dbus_next" if "bluetooth" in module else "aiosendspin")
    path = REPO / "backend" / "scripts" / (module.replace(".", "/") + ".py")
    src = path.read_text()
    assert "timestamp_us=None" in src, f"{symbol} must force a fresh progress timestamp"
