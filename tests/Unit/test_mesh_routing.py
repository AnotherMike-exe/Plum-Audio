"""Unit tests for the mesh state model + router path selection.

Pure-logic: no aiosendspin, no sockets. Covers the normalized model wire form and the router's
three routing paths (intra / cross-server reclaim / delegate) plus its error cases — the same
logic hardware-validated on the two-Pi mesh. Run: `pytest tests/Unit` (adds backend/scripts to
the path so `mesh`/`sync_engine` import as they do at runtime).
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend" / "scripts"))

from mesh.discovery import Peer  # noqa: E402
from mesh.model import MeshView, PlayerState, SourceState, UnitSnapshot  # noqa: E402
from mesh.router import RouteError, Router  # noqa: E402


class FakeEngine:
    """Records engine calls so tests can assert what the router asked for."""

    def __init__(self):
        self.calls = []

    async def attach_local_player(self, source_id, player_id):
        self.calls.append(("attach_local", source_id, player_id))

    async def detach_player(self, source_id, player_id):
        self.calls.append(("detach", source_id, player_id))

    async def reclaim_remote_player(self, source_id, player_id, url):
        self.calls.append(("reclaim", source_id, player_id, url))
        return True

    async def set_player_volume(self, player_id, volume, muted):
        self.calls.append(("vol", player_id, volume, muted))


def _view():
    local = UnitSnapshot(
        "unitA",
        "A",
        host="10.0.0.1",
        sources=[SourceState("airplay", "g1", "AirPlay", True, ["playerA"])],
        players=[PlayerState("playerA", "A", True, "g1", url="ws://10.0.0.1:8928/sendspin")],
    )
    peer = UnitSnapshot(
        "unitB",
        "B",
        host="10.0.0.2",
        sources=[SourceState("spotify", "g2", "Spotify", True, [])],
        players=[PlayerState("playerB", "B", True, None, url="ws://10.0.0.2:8928/sendspin")],
    )
    return MeshView([local, peer])


PEERS = {"unitB": Peer("unitB", "B", "10.0.0.2", 8927, 8928, 0.0)}


def _router(engine, delegate=None):
    return Router(
        "unitA",
        engine,
        view_provider=_view,
        peer_provider=PEERS.get,
        delegate=delegate,
    )


def test_intra_server_route_uses_attach_local():
    e = FakeEngine()
    ok = asyncio.run(_router(e).route_player("playerA", "airplay"))
    assert ok and e.calls == [("attach_local", "airplay", "playerA")]


def test_cross_server_route_reclaims_against_players_own_url():
    e = FakeEngine()
    ok = asyncio.run(_router(e).route_player("playerB", "airplay"))
    # reclaim uses the player's OWN listener URL, not the unit it's attached to.
    assert ok and e.calls == [("reclaim", "airplay", "playerB", "ws://10.0.0.2:8928/sendspin")]


def test_remote_source_delegates_to_owning_unit():
    delegated = []

    async def delegate(peer, source_id, player_id):
        delegated.append((peer.unit_id, source_id, player_id))
        return True

    e = FakeEngine()
    ok = asyncio.run(_router(e, delegate=delegate).route_player("playerA", "spotify"))
    assert ok and delegated == [("unitB", "spotify", "playerA")] and e.calls == []


def test_remote_source_without_delegate_raises():
    with pytest.raises(RouteError, match="no remote delegate"):
        asyncio.run(_router(FakeEngine()).route_player("playerA", "spotify"))


@pytest.mark.parametrize(
    "player_id,source_id",
    [("playerA", "nope"), ("ghost", "airplay")],
)
def test_unknown_source_or_player_raises(player_id, source_id):
    with pytest.raises(RouteError):
        asyncio.run(_router(FakeEngine()).route_player(player_id, source_id))


def test_find_player_prefers_connected_over_stale_stub():
    # A roamed player can momentarily appear on both units; connected wins.
    stale = UnitSnapshot("u1", "1", host=None, players=[PlayerState("p", "p", False, None)])
    live = UnitSnapshot("u2", "2", host=None, players=[PlayerState("p", "p", True, "g")])
    assert MeshView([stale, live]).find_player("p")[0].unit_id == "u2"


def test_unit_snapshot_wire_roundtrip():
    snap = _view().units[0]
    assert UnitSnapshot.from_dict(snap.to_dict()).to_dict() == snap.to_dict()


def _view_with_idle_player():
    """unitB's speaker is attached to nothing: absent from every `players` list, present only in
    its own unit's `local_player` self-report. This is the state a player lands in after being sent
    to none, or after its unit reboots."""
    local = UnitSnapshot(
        "unitA",
        "A",
        host="10.0.0.1",
        sources=[SourceState("airplay", "g1", "AirPlay", True, [])],
    )
    peer = UnitSnapshot(
        "unitB",
        "B",
        host="10.0.0.2",
        local_player={"player_id": "playerB", "url": "ws://10.0.0.2:8928/sendspin", "attached": False},
    )
    return MeshView([local, peer])


def test_idle_player_is_routable_via_its_units_self_report():
    # Regression: routing an idle player raised "unknown player (not on any unit)", and since
    # routing is the only thing that attaches a player, nothing could get it out of that state —
    # auto-follow retried forever while the speaker stayed silent.
    engine = FakeEngine()
    router = Router("unitA", engine, view_provider=_view_with_idle_player, peer_provider=PEERS.get)
    assert asyncio.run(router.route_player("playerB", "airplay")) is True
    assert engine.calls == [("reclaim", "airplay", "playerB", "ws://10.0.0.2:8928/sendspin")]


def test_idle_player_loopback_url_is_rewritten_to_the_units_host():
    # A unit that registered itself as ws://127.0.0.1 must not be dialled on OUR loopback.
    view = _view_with_idle_player()
    view.units[1].local_player["url"] = "ws://127.0.0.1:8928/sendspin"
    engine = FakeEngine()
    router = Router("unitA", engine, view_provider=lambda: view, peer_provider=PEERS.get)
    asyncio.run(router.route_player("playerB", "airplay"))
    assert engine.calls == [("reclaim", "airplay", "playerB", "ws://10.0.0.2:8928/sendspin")]


def test_genuinely_unknown_player_still_raises():
    router = Router("unitA", FakeEngine(), view_provider=_view_with_idle_player, peer_provider=PEERS.get)
    with pytest.raises(RouteError):
        asyncio.run(router.route_player("ghost", "airplay"))
