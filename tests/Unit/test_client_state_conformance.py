"""Unit tests for the client/state wire format.

**Rewritten for aiosendspin 9.x.** This file used to guard `build_client_state_message`, a local
builder that existed because 6.0.5 dropped the spec's REQUIRED top-level `state` field (UPSTREAM §0).
Its last test was a canary asserting that upstream bug was still present.

The canary fired in the only way it could not have anticipated: **9.x deleted the field.**
`ClientStateType` no longer exists anywhere in the package and `ClientStatePayload.state` became
`available: bool`. The library now populates the top level itself, so the workaround is gone and
`send_player_state()` is called directly again.

What is left worth testing is not the library's serialisation but two decisions of ours:

  1. A struggling renderer must report `available=True`. `available` is not the old `state` renamed —
     `send_available`'s own docstring says "an active source stream ends before the client reports
     unavailable", so mapping a transient xrun onto `available=False` would tear the stream down and
     convert a brief dropout into a real one. The health signal moved off the wire entirely; see
     `sendspin_player.PlayerHealth`.
  2. The volume echo survives. The server's view of a player's level moves ONLY on client/state, so
     if the library ever stops carrying volume here, every level in the mesh silently reads 100%
     while the audio is demonstrably quieter — a documented hard-won failure that looks like a GUI
     bug and is not one.

Run: `pytest tests/Unit/test_client_state_conformance.py`.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "scripts"))

pytest.importorskip("aiosendspin", reason="aiosendspin is a real runtime dep; skipped on a bare checkout")
pytest.importorskip("numpy", reason="sendspin_player imports numpy")

from aiosendspin.models.core import ClientStatePayload  # noqa: E402
from aiosendspin.models.player import PlayerStatePayload  # noqa: E402

import sendspin_player  # noqa: E402
from sendspin_player import PlayerHealth  # noqa: E402


class FakeClient:
    """Records what send_player_state was called with."""

    def __init__(self):
        self.calls: list[dict] = []

    async def send_player_state(self, *, available, volume, muted):
        self.calls.append({"available": available, "volume": volume, "muted": muted})


class FakeRenderer:
    def stats(self) -> str:
        return "[pad=0 starv=0]"


class FakePlayer:
    """_send_client_state unbound, so nothing real has to be constructed."""

    def __init__(self, tmp_path):
        self.client = FakeClient()
        self.renderer = FakeRenderer()
        self._volume = 42
        self._muted = False
        self._last_reported_state = PlayerHealth.SYNCHRONIZED
        self._state_file = str(tmp_path / "player_state.json")

    send = sendspin_player.SendspinPlayer._send_client_state


def test_a_struggling_player_still_reports_available_true(tmp_path):
    """THE regression guard for the 9.x migration.

    available=False ends the active stream. Reporting an underrun that way would make a dropout
    permanent — the opposite of what the old `state: error` asked a server to do.
    """
    p = FakePlayer(tmp_path)
    asyncio.run(p.send(PlayerHealth.ERROR))
    assert p.client.calls == [{"available": True, "volume": 42, "muted": False}]


def test_a_health_change_is_persisted_where_the_api_can_see_it(tmp_path):
    """The signal moved off the wire, so it has to land somewhere queryable or it is just gone."""
    p = FakePlayer(tmp_path)
    asyncio.run(p.send(PlayerHealth.ERROR))
    assert json.loads(Path(p._state_file).read_text())["player_health"] == "error"


def test_an_unchanged_health_is_not_rewritten(tmp_path):
    """It reports every HEALTH_POLL_S; only transitions are worth a warning and a write."""
    p = FakePlayer(tmp_path)
    asyncio.run(p.send(PlayerHealth.SYNCHRONIZED))
    assert not Path(p._state_file).exists()
    assert p.client.calls, "the state message itself must still be sent every time"


def test_available_is_present_at_the_top_level():
    """The spec field the old builder existed to supply — now the library's job, so this is a canary
    on the library rather than on us. If it regresses, a spec-strict peer sees a required field
    missing on every message we send."""
    payload = json.loads(ClientStatePayload(available=True, player=PlayerStatePayload(volume=1)).to_json())
    assert payload["available"] is True


def test_the_player_payload_still_carries_volume_and_mute():
    payload = json.loads(ClientStatePayload(player=PlayerStatePayload(volume=17, muted=True)).to_json())["player"]
    assert payload["volume"] == 17
    assert payload["muted"] is True


def test_the_deleted_state_enum_has_not_come_back():
    """The inverse canary of the one this file used to carry.

    If ClientStateType reappears upstream, the spec has changed again and the PlayerHealth decision
    (detection local, reporting off-wire) should be revisited rather than left as a silent divergence.
    """
    import aiosendspin.models.types as types

    assert not hasattr(types, "ClientStateType"), (
        "aiosendspin reintroduced ClientStateType — re-read sendspin_player.PlayerHealth"
    )
