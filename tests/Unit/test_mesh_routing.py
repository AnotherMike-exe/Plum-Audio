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
        self.staged = []  # (player_id, stage_pairing) per reclaim

    async def attach_local_player(self, source_id, player_id):
        self.calls.append(("attach_local", source_id, player_id))

    async def detach_player(self, source_id, player_id):
        self.calls.append(("detach", source_id, player_id))

    async def reclaim_remote_player(self, source_id, player_id, url, *, stage_pairing=False):
        self.calls.append(("reclaim", source_id, player_id, url))
        # Recorded separately so the existing call assertions keep their shape: staging a pairing
        # PSK against a cleartext speaker takes it offline, so WHETHER we stage is its own claim.
        self.staged.append((player_id, stage_pairing))
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


# -- has_player on the wire ------------------------------------------------------------------------


def test_has_player_defaults_true_for_a_peer_on_an_older_image():
    """Wire compat, and it is not cosmetic.

    A peer that predates this field sends no `has_player`. Reading that as "playerless" would make
    every existing unit look like an ingest-only node — and FollowReconciler would start deriving
    their leader stream from sources instead of their self-reported player.
    """
    snap = UnitSnapshot.from_dict(
        {"unit_id": "unit-211", "name": "Pi4-01", "host": "10.0.0.1", "sources": [], "players": []}
    )
    assert snap.has_player is True


def test_has_player_survives_a_round_trip():
    original = UnitSnapshot("unit-hl", "Headless", host="10.0.0.9", has_player=False)
    assert UnitSnapshot.from_dict(original.to_dict()).has_player is False


def test_routing_onto_a_playerless_unit_is_refused():
    """There is no speaker to route onto — the correct answer is an error, not a silent no-op."""
    headless = UnitSnapshot("unit-hl", "Headless", host="10.0.0.9", has_player=False,
                            sources=[SourceState("airplay", "g9", "AirPlay", True, [])])
    view = MeshView([headless])
    router = Router("unit-hl", FakeEngine(), view_provider=lambda: view, peer_provider=lambda _u: None)

    with pytest.raises(RouteError):
        asyncio.run(router.route_player("unit-hl-player", "airplay"))


# -- staging a pairing PSK on the reclaim path --------------------------------
#
# Staging turns the next handshake into a PAIRING handshake, which the library aborts against a
# cleartext client — taking that speaker offline until the next adopt (CLAUDE.md,
# docs/SENDSPIN-PAIRING.md). The reclaim path used to stage unconditionally, justified by "player_id
# came from a peer snapshot, so this only ever names a Plum player". `snapshot()` applies no
# ownership filter, so an adopted third-party speaker sits in a peer's `players` exactly like a Plum
# player, and that premise was false. OPEN-ITEMS #21.


def _view_with(peer_players, peer_local_player=None):
    """Our unit plus a peer holding `peer_players`, optionally declaring its own speaker."""
    local = UnitSnapshot(
        "unitA",
        "A",
        host="10.0.0.1",
        sources=[SourceState("airplay", "g1", "AirPlay", True, [])],
        players=[],
    )
    peer = UnitSnapshot(
        "unitB",
        "B",
        host="10.0.0.2",
        sources=[],
        players=peer_players,
        local_player=peer_local_player,
    )
    return MeshView([local, peer])


def _router_for(engine, view):
    return Router("unitA", engine, view_provider=lambda: view, peer_provider=PEERS.get)


def test_a_peers_own_player_is_staged():
    """The conclusive case: it is some unit's own speaker, and ours are never cleartext."""
    player = PlayerState("playerB", "B", True, None, url="ws://10.0.0.2:8928/sendspin", security="sentinel")
    view = _view_with([player], peer_local_player={"player_id": "playerB", "url": player.url})
    engine = FakeEngine()
    asyncio.run(_router_for(engine, view).route_player("playerB", "airplay"))
    assert engine.staged == [("playerB", True)]


def test_an_encrypted_client_is_staged_on_its_reported_security():
    """Not any unit's own speaker (it has roamed), but the holder reports an encrypted connection."""
    player = PlayerState("playerC", "C", True, "g2", url="ws://10.0.0.3:8928/sendspin", security="long_term")
    engine = FakeEngine()
    asyncio.run(_router_for(engine, _view_with([player])).route_player("playerC", "airplay"))
    assert engine.staged == [("playerC", True)]


def test_an_adopted_cleartext_speaker_is_NOT_staged():
    """The bug. A third-party speaker adopted onto a peer's source is in that peer's `players`;
    staging it would abort its next handshake and take it offline."""
    esp32 = PlayerState(
        "98:A3:16:D0:9E:E8", "Voice PE", True, "g2", url="ws://10.0.0.9:8927/sendspin", security=None
    )
    engine = FakeEngine()
    asyncio.run(_router_for(engine, _view_with([esp32])).route_player("98:A3:16:D0:9E:E8", "airplay"))
    assert engine.staged == [("98:A3:16:D0:9E:E8", False)]
    # It is still reclaimed — the route must work, it just must not pair.
    assert any(call[0] == "reclaim" for call in engine.calls)


def test_an_unknown_security_field_fails_safe():
    """`security` defaults to None, so a peer on an older image is indistinguishable from a
    cleartext client. Requiring POSITIVE evidence makes the ambiguous case skip staging (logged)
    rather than knock a speaker offline."""
    player = PlayerState("playerD", "D", True, "g2", url="ws://10.0.0.4:8928/sendspin")
    engine = FakeEngine()
    asyncio.run(_router_for(engine, _view_with([player])).route_player("playerD", "airplay"))
    assert engine.staged == [("playerD", False)]


def test_an_idle_player_from_a_self_report_is_staged():
    """The idle fallback resolves through `local_player`, which only a unit's own speaker appears
    in — so that path is conclusive and stages unconditionally."""
    view = _view_with([], peer_local_player={"player_id": "playerB", "url": "ws://10.0.0.2:8928/sendspin"})
    engine = FakeEngine()
    asyncio.run(_router_for(engine, view).route_player("playerB", "airplay"))
    assert engine.staged == [("playerB", True)]


def test_unit_by_own_player_ignores_attached_clients():
    """The join must read `local_player`, never `players`: after an adopt the latter contains
    third-party speakers, which is exactly what made the old premise false."""
    esp32 = PlayerState("aa:bb", "ESP", True, "g2", url="ws://10.0.0.9:8927/sendspin")
    view = _view_with([esp32], peer_local_player={"player_id": "playerB"})
    assert view.unit_by_own_player("aa:bb") is None
    assert view.unit_by_own_player("playerB") is not None
    assert view.unit_by_own_player(None) is None
