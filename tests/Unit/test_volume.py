"""Unit tests for the two volumes: the endpoint's persisted render level, and source volume.

Pure-logic: no aiosendspin, no D-Bus, no hardware. Covers the player's persisted render state
(`player_state`), the snapshot wire form for both new field groups, and the router's source-volume
passthrough. Run: `pytest tests/Unit`.

The reason the persistence exists is worth restating where it is tested: a Sendspin server never
tells a player what its level is, so the value a player reports at connect is what the whole mesh
believes. Lose it on restart and the room silently returns to 100%.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend" / "scripts"))

from mesh.model import MeshView, PlayerState, SourceState  # noqa: E402
from mesh.router import Router  # noqa: E402
from player_state import load_render_state, save_render_state, state_file_path  # noqa: E402


def _router(engine) -> Router:
    return Router("unitA", engine, view_provider=lambda: MeshView([]), peer_provider=lambda _u: None)


# -- persisted render state ---------------------------------------------------


def test_render_state_round_trips(tmp_path):
    path = str(tmp_path / "player_state.json")
    save_render_state(path, 42, True)
    assert load_render_state(path, default_volume=100) == (42, True)


def test_render_state_creates_missing_directory(tmp_path):
    path = str(tmp_path / "nested" / "dir" / "player_state.json")
    save_render_state(path, 55, False)
    assert json.loads(Path(path).read_text()) == {"volume": 55, "muted": False}


def test_missing_file_falls_back_to_default(tmp_path):
    assert load_render_state(str(tmp_path / "absent.json"), default_volume=70) == (70, False)


def test_malformed_file_falls_back_rather_than_raising(tmp_path):
    path = tmp_path / "player_state.json"
    path.write_text("{ this is not json")
    assert load_render_state(str(path), default_volume=65, default_muted=True) == (65, True)


def test_out_of_range_volume_is_clamped(tmp_path):
    path = tmp_path / "player_state.json"
    path.write_text(json.dumps({"volume": 320, "muted": False}))
    assert load_render_state(str(path), default_volume=100)[0] == 100


def test_state_file_path_honours_env(monkeypatch):
    monkeypatch.setenv("PLUM_PLAYER_STATE_FILE", "/tmp/elsewhere.json")
    assert state_file_path() == "/tmp/elsewhere.json"


# -- snapshot wire form -------------------------------------------------------


def test_player_state_carries_volume_over_the_wire():
    state = PlayerState("p1", "Kitchen", True, "g1", url="ws://x/sendspin", volume=37, muted=True)
    assert PlayerState.from_dict(state.to_dict()) == state


def test_player_state_defaults_when_a_peer_predates_the_field():
    # A peer on an older build sends no volume key; it must read as full, not as absent/0.
    restored = PlayerState.from_dict({"player_id": "p1", "name": "Kitchen", "connected": True, "group_id": None})
    assert (restored.volume, restored.muted) == (100, False)


def test_source_state_carries_source_volume_over_the_wire():
    state = SourceState(
        "airplay-1", "g1", "G", True, name="Lounge AP",
        source_volume=64, source_muted=False, supports_source_volume=True,
    )
    assert SourceState.from_dict(state.to_dict()) == state


def test_source_without_a_sender_reports_no_source_volume():
    state = SourceState("airplay-1", "g1", "G", False)
    assert state.to_dict()["source_volume"] is None
    assert state.to_dict()["supports_source_volume"] is False


# -- router passthrough -------------------------------------------------------


class FakeEngine:
    def __init__(self):
        self.calls = []

    async def set_player_volume(self, player_id, volume, muted):
        self.calls.append(("player", player_id, volume, muted))

    async def set_source_volume(self, source_id, volume=None, muted=None):
        self.calls.append(("source", source_id, volume, muted))


def test_router_keeps_the_two_volumes_apart():
    engine = FakeEngine()
    router = _router(engine)
    asyncio.run(router.set_volume("player-210", 60, False))
    asyncio.run(router.set_source_volume("airplay-1", 30, None))
    assert engine.calls == [("player", "player-210", 60, False), ("source", "airplay-1", 30, None)]


def test_router_surfaces_a_source_that_cannot_do_it():
    class NoSourceVolume(FakeEngine):
        async def set_source_volume(self, source_id, volume=None, muted=None):
            raise RuntimeError(f"source {source_id!r} has no source-volume control")

    router = _router(NoSourceVolume())
    with pytest.raises(RuntimeError):
        asyncio.run(router.set_source_volume("bluetooth-1", 50))
