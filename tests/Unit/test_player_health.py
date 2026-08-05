"""Unit tests for the player's client/state health signal.

The spec asks a client that cannot maintain sync to report `state: 'error'`; that is how a server
learns to give it more lead time. The signal is only useful if it is quiet when nothing is wrong.

`test_an_idle_player_is_not_an_error` guards the bug this file was written for. AlsaRenderer keeps
two padded-silence counters and they are NOT interchangeable:

  pad_frames     every padded frame, unconditionally — deliberately, so a gap that happened while
                 the renderer thought it was idle (a cross-server roam) is still counted.
  starved_frames padded frames only while the renderer believes it is PLAYING.

Health was first measured on pad_frames. An attached-but-idle player pads every callback block, so
it accumulates a full second of padding per second and trips any threshold immediately — every idle
speaker in the mesh reported `error`. Caught on hardware 2026-08-05 (`state=error` with `starv=0`)
and not by any test, because nothing exercised the idle path.

Run: `pytest tests/Unit/test_player_health.py`.
"""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "scripts"))

pytest.importorskip("numpy", reason="sendspin_player imports numpy")
pytest.importorskip("aiosendspin", reason="aiosendspin is a real runtime dep")

from aiosendspin.models.types import ClientStateType  # noqa: E402

import sendspin_player  # noqa: E402
from sendspin_player import ERROR_STARVED_FRAMES  # noqa: E402


class FakeRenderer:
    """Only the two counters _health reads."""

    def __init__(self):
        self.pad_frames = 0
        self.starved_frames = 0

    def stats(self) -> str:
        return f"[pad={self.pad_frames} starv={self.starved_frames}]"


class FakePlayer:
    """_health unbound from the class, so no PortAudio/aiosendspin construction is needed."""

    def __init__(self):
        self.renderer = FakeRenderer()
        self._last_starved_frames = 0

    health = sendspin_player.SendspinPlayer._health


def player() -> FakePlayer:
    return FakePlayer()


def test_a_quiet_player_reports_synchronized():
    p = player()
    assert p.health() is ClientStateType.SYNCHRONIZED


def test_an_idle_player_is_not_an_error():
    """The regression. Idle padding accrues on pad_frames only — it must not reach the signal."""
    p = player()
    p.renderer.pad_frames += ERROR_STARVED_FRAMES * 100  # ~5s of idle silence
    assert p.health() is ClientStateType.SYNCHRONIZED, "idle padding must not read as a fault"


def test_sustained_starvation_reports_error():
    p = player()
    p.renderer.starved_frames += ERROR_STARVED_FRAMES
    assert p.health() is ClientStateType.ERROR


def test_a_single_hiccup_is_tolerated():
    """Below threshold is one scheduling blip, not a client that cannot keep up."""
    p = player()
    p.renderer.starved_frames += ERROR_STARVED_FRAMES - 1
    assert p.health() is ClientStateType.SYNCHRONIZED


def test_recovery_returns_to_synchronized():
    """The delta must reset, or one dropout pins the player at error for the rest of its life."""
    p = player()
    p.renderer.starved_frames += ERROR_STARVED_FRAMES * 2
    assert p.health() is ClientStateType.ERROR
    assert p.health() is ClientStateType.SYNCHRONIZED, "a lifetime total would never recover"


def test_starvation_is_measured_per_window_not_cumulatively():
    p = player()
    for _ in range(5):
        p.renderer.starved_frames += ERROR_STARVED_FRAMES // 4  # slow drip, under threshold
        assert p.health() is ClientStateType.SYNCHRONIZED
