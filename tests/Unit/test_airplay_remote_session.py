"""Unit tests for AirplayRemote's session-presence tracking.

`_has_session` decides whether the GUI offers the source-volume slider as controllable. It used to be
a bare `PlaybackStatus != "Stopped"` written from THREE places on two schedules — the 5s volume
watch, the PlaybackStatus signal, and the Volume signal (which forced it True unconditionally).

A pausing AirPlay sender produces a flurry of status changes, so the flag flapped on roughly the
poll interval: the slider locked and unlocked at random, and an adjustment attempted during a
"locked" window was silently refused by the server. Reported from the rig 2026-08-05.

The Volume signal was the worse half. shairport emits `Volume=0.0` as a session tears down, and the
handler both latched that as the last-known level and flipped presence back to True on the way past.

Nothing tested this module before, which is why none of it was caught.

Run: `pytest tests/Unit/test_airplay_remote_session.py`.
"""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "scripts"))

pytest.importorskip("dbus_next", reason="airplay_remote imports dbus-next")

from sources.airplay_remote import PLAYER_IFACE, SESSION_LOSS_STRIKES, AirplayRemote  # noqa: E402


def remote():
    reported: list[int] = []
    r = AirplayRemote(on_source_volume=reported.append)
    return r, reported


def status(r, value: str) -> None:
    r._on_props_changed(PLAYER_IFACE, {"PlaybackStatus": value}, [])


def volume(r, level: float) -> None:
    r._on_props_changed(PLAYER_IFACE, {"Volume": level}, [])


# -- presence ----------------------------------------------------------------------------------


def test_a_playing_sender_is_present():
    r, _ = remote()
    status(r, "Playing")
    assert r.supports_source_volume is False, "not bound to a player yet"
    assert r._has_session is True


def test_a_paused_sender_is_still_present():
    """Pause is not departure — the phone is still there and its level is still meaningful."""
    r, _ = remote()
    status(r, "Playing")
    status(r, "Paused")
    assert r._has_session is True


def test_one_stopped_does_not_lose_the_session():
    """The regression: a single transient Stopped mid-pause used to lock the slider immediately."""
    r, _ = remote()
    status(r, "Playing")
    status(r, "Stopped")
    assert r._has_session is True, "loss must require hysteresis"


def test_sustained_stopped_loses_the_session():
    r, _ = remote()
    status(r, "Playing")
    for _ in range(SESSION_LOSS_STRIKES):
        status(r, "Stopped")
    assert r._has_session is False


def test_a_sender_returning_is_immediate():
    """Gaining is not hysteretic — a phone reconnecting must be usable at once."""
    r, _ = remote()
    status(r, "Playing")
    for _ in range(SESSION_LOSS_STRIKES):
        status(r, "Stopped")
    status(r, "Playing")
    assert r._has_session is True


def test_the_strike_counter_resets_on_presence():
    """Alternating Stopped/Playing must never accumulate its way to a false loss."""
    r, _ = remote()
    status(r, "Playing")
    for _ in range(10):
        status(r, "Stopped")
        status(r, "Playing")
    assert r._has_session is True


# -- the volume signal must not drive presence --------------------------------------------------


def test_a_volume_signal_does_not_resurrect_a_departed_sender():
    r, _ = remote()
    status(r, "Playing")
    for _ in range(SESSION_LOSS_STRIKES):
        status(r, "Stopped")
    assert r._has_session is False

    volume(r, 0.0)  # what shairport emits as the session tears down
    assert r._has_session is False, "presence is PlaybackStatus's job alone"


def test_the_teardown_volume_is_not_latched_as_the_last_known_level():
    """The 'jumped to a different value' half: a 0.0 at teardown must not become the cached level."""
    r, reported = remote()
    status(r, "Playing")
    volume(r, 0.64)
    for _ in range(SESSION_LOSS_STRIKES):
        status(r, "Stopped")
    volume(r, 0.0)

    assert reported == [64], "only the level from a live session may be reported"


def test_volume_still_flows_while_a_sender_is_present():
    r, reported = remote()
    status(r, "Playing")
    volume(r, 0.25)
    volume(r, 0.80)
    assert reported == [25, 80]


def test_a_pause_does_not_stop_volume_reporting():
    """A paused phone can still move its slider, and that must reach us."""
    r, reported = remote()
    status(r, "Playing")
    status(r, "Paused")
    volume(r, 0.42)
    assert reported == [42]


def test_signals_for_other_interfaces_are_ignored():
    r, reported = remote()
    status(r, "Playing")
    r._on_props_changed("org.mpris.MediaPlayer2", {"Volume": 0.9}, [])
    assert reported == []
