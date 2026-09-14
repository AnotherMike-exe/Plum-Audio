"""Unit tests for the discovery beacon's handling of hostile or malformed input.

The beacon is unauthenticated UDP broadcast on a LAN, so `_on_datagram` is the one place in the
mesh that parses bytes from an untrusted sender. Three things had to hold and did not:

- the peer table only ever GREW (peers() filtered by TTL but never deleted), and every entry costs
  a concurrent HTTP fetch per aggregator tick — inside the audio event loop;
- `int(msg.get("server_port"))` raised on a non-numeric value, and the exception escaped into
  datagram_received, i.e. one traceback per packet;
- nothing bounded how many distinct unit_ids a sender could mint.

Run: `pytest tests/Unit/test_mesh_discovery.py`.
"""

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "scripts"))

from mesh.discovery import MAX_PEERS, MeshDiscovery  # noqa: E402


def make_discovery() -> MeshDiscovery:
    return MeshDiscovery(unit_id="us", unit_name="Us", server_port=8927, player_port=8928)


def beacon(**over) -> bytes:
    msg = {"v": 1, "unit_id": "them", "name": "Them", "server_port": 8927, "player_port": 8928}
    msg.update(over)
    return json.dumps(msg).encode()


def test_a_normal_beacon_is_accepted():
    d = make_discovery()
    d._on_datagram(beacon(), ("192.168.1.5", 8929))
    peers = d.peers()
    assert [p.unit_id for p in peers] == ["them"]
    assert peers[0].host == "192.168.1.5"  # source IP wins over anything in the payload


def test_expired_peers_are_deleted_not_merely_filtered():
    d = make_discovery()
    d._on_datagram(beacon(), ("192.168.1.5", 8929))
    d._peers["them"].last_seen -= 999  # age it past the TTL

    assert d.peers() == []
    assert "them" not in d._peers, "the table itself must shrink, not just its filtered view"


def test_a_flood_of_unit_ids_cannot_grow_the_table_without_bound():
    d = make_discovery()
    for i in range(MAX_PEERS * 3):
        d._on_datagram(beacon(unit_id=f"flood-{i}"), ("192.168.1.9", 8929))
    assert len(d._peers) <= MAX_PEERS


def test_a_non_numeric_port_is_dropped_rather_than_raising():
    d = make_discovery()
    d._on_datagram(beacon(server_port="not-a-port"), ("192.168.1.5", 8929))
    assert d.peers() == []


def test_an_out_of_range_port_is_dropped():
    d = make_discovery()
    d._on_datagram(beacon(player_port=99999), ("192.168.1.5", 8929))
    assert d.peers() == []


def test_a_missing_port_falls_back_to_our_own():
    """Absent is not malformed — older beacons omitted these."""
    d = make_discovery()
    raw = json.dumps({"v": 1, "unit_id": "them", "name": "Them"}).encode()
    d._on_datagram(raw, ("192.168.1.5", 8929))
    assert d.peers()[0].server_port == 8927


def test_garbage_is_ignored_quietly():
    d = make_discovery()
    for raw in [b"", b"\xff\xfe", b"{}", b"not json", json.dumps({"v": 99}).encode()]:
        d._on_datagram(raw, ("192.168.1.5", 8929))
    assert d.peers() == []


def test_our_own_beacon_is_ignored():
    d = make_discovery()
    d._on_datagram(beacon(unit_id="us"), ("192.168.1.1", 8929))
    assert d.peers() == []


def test_a_non_string_unit_id_is_ignored():
    d = make_discovery()
    d._on_datagram(beacon(unit_id={"nested": "object"}), ("192.168.1.5", 8929))
    assert d.peers() == []


def test_a_known_peer_refreshes_rather_than_competing_for_a_slot():
    """A full table must not stop existing units from staying alive."""
    d = make_discovery()
    for i in range(MAX_PEERS):
        d._on_datagram(beacon(unit_id=f"u{i}"), ("192.168.1.9", 8929))
    first = d._peers["u0"].last_seen
    d._peers["u0"].last_seen -= 1

    d._on_datagram(beacon(unit_id="u0"), ("192.168.1.9", 8929))
    assert d._peers["u0"].last_seen >= first - 1


# -- recognising our OWN player in the mDNS view ----------------------------------------------------


class _Svc:
    """The fields Neighbourhood.is_own_player reads off a DiscoveredService."""

    def __init__(self, name, ws_url):
        self.name = name
        self.ws_url = ws_url


def _nbhd(**kw):
    from mesh.neighbourhood import Neighbourhood

    return Neighbourhood("unit-7122", "Plum RackPi", server_port=8927, **kw)


def test_our_own_player_is_recognised_by_url_not_id():
    """The 9.x regression.

    An mDNS record carries the LISTENER id; the id a server knows a speaker by is now its X25519
    public key. `own_client_ids` holds the latter, so matching on name alone stopped recognising our
    own speaker — it showed as foreign in the neighbourhood and the GUI offered to route it to
    itself. The listener URL is the identifier both views share.
    """
    n = _nbhd(own_client_ids={"PEERKEY-x25519"}, own_player_url="ws://192.168.7.122:8928/sendspin")
    mine = _Svc("player-7122", "ws://192.168.7.122:8928/sendspin")
    assert n.is_own_player(mine) is True
    assert n.foreign_players() == []


def test_a_peers_player_on_the_same_port_is_still_foreign():
    """Host matters, not just port — every unit's player listens on 8928."""
    n = _nbhd(own_client_ids=set(), own_player_url="ws://192.168.7.122:8928/sendspin")
    assert n.is_own_player(_Svc("player-7204", "ws://192.168.7.204:8928/sendspin")) is False


def test_the_id_match_still_works_without_a_url():
    """Fallback for a unit whose advertised URL could not be derived."""
    n = _nbhd(own_client_ids={"player-7122"}, own_player_url=None)
    assert n.is_own_player(_Svc("player-7122", "ws://192.168.7.122:8928/sendspin")) is True


def test_a_trailing_path_difference_does_not_make_it_foreign():
    """Compared on (host, port): what a device advertises and what we derived need not match byte
    for byte."""
    n = _nbhd(own_client_ids=set(), own_player_url="ws://192.168.7.122:8928")
    assert n.is_own_player(_Svc("player-7122", "ws://192.168.7.122:8928/sendspin")) is True
