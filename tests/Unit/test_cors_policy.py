"""Unit tests for the CORS origin policy.

This is a security change whose failure mode is not "insecure" but "the mesh view goes blank on
every unit at once", so most of these guard the ALLOW side rather than the deny side.

The three that matter most:

`test_the_page_may_call_its_own_unit` — the GUI POSTs cross-origin to :5001 for EVERY unit including
the one serving the page. A policy that allowed peers but not self would break route/volume/adopt on
the page in front of you, which is the single easiest way to get this wrong.

`test_a_request_with_no_origin_is_not_refused` — peer snapshot polls, delegated routes and the
loopback player-state POST send no Origin at all. Refusing those breaks mesh aggregation from the
inside, with nothing visible in any browser.

`test_a_peer_recognises_a_page_served_by_its_hostname` — the peer table only ever learns IPs, but
people open `http://plum-amp100.local`. That is why `hostname` is on the snapshot.

Run: `pytest tests/Unit/test_cors_policy.py`.
"""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "scripts"))

import cors_policy  # noqa: E402
from cors_policy import is_allowed, known_hosts, name_forms, origin_header  # noqa: E402
from mesh.model import MeshView, UnitSnapshot  # noqa: E402


def _units():
    return [
        UnitSnapshot("unit-7204", "Plum Amp100", host="198.51.100.21", hostname="plum-amp100"),
        UnitSnapshot("unit-7122", "Plum VLAN7", host="198.51.100.20", hostname="plum-vlan7"),
    ]


HOSTS = known_hosts(_units())


# -- what must be allowed --------------------------------------------------------------------------


def test_the_page_may_call_its_own_unit():
    """The GUI POSTs to :5001 by IP even for the unit serving the page — GETs go via nginx, writes do not."""
    assert is_allowed("http://198.51.100.21", HOSTS, extras=[]) is True


def test_a_page_on_one_unit_may_drive_a_peer():
    assert is_allowed("http://198.51.100.21", HOSTS, extras=[]) is True
    assert is_allowed("http://198.51.100.20", HOSTS, extras=[]) is True


def test_a_peer_recognises_a_page_served_by_its_hostname():
    """The peer table knows IPs; a person types plum-amp100.local. Hence hostname on the snapshot."""
    for origin in (
        "http://plum-amp100",
        "http://plum-amp100.local",
        "http://plum-amp100.lan",
        "http://PLUM-AMP100.local",
    ):
        assert is_allowed(origin, HOSTS, extras=[]) is True, origin


def test_the_port_does_not_matter():
    """The GUI is on :80, dev is on :5173, and a reachable unit is reachable on all of its own ports."""
    assert is_allowed("http://198.51.100.21:5173", HOSTS, extras=[]) is True
    assert is_allowed("http://198.51.100.21:5001", HOSTS, extras=[]) is True


def test_loopback_is_always_allowed():
    assert is_allowed("http://localhost:5173", set(cors_policy.LOOPBACK_HOSTS), extras=[]) is True
    assert is_allowed("http://127.0.0.1", set(cors_policy.LOOPBACK_HOSTS), extras=[]) is True


def test_https_is_accepted():
    assert is_allowed("https://198.51.100.21", HOSTS, extras=[]) is True


def test_a_configured_extra_is_allowed():
    assert is_allowed("http://dev-box:5173", HOSTS, extras=["http://dev-box:5173"]) is True


def test_the_wildcard_escape_hatch_restores_the_old_behaviour():
    """PLUM_ALLOWED_ORIGINS=* — a unit whose GUI is dead is worse than one reachable too broadly."""
    assert is_allowed("http://anything-at-all", HOSTS, extras=["*"]) is True


# -- what must be refused ---------------------------------------------------------------------------


def test_an_unknown_lan_page_is_refused():
    """The whole point: any page on the LAN could previously rename a unit or re-route its audio."""
    assert is_allowed("http://198.51.100.40", HOSTS, extras=[]) is False


def test_a_lookalike_hostname_is_refused():
    assert is_allowed("http://plum-amp100.evil.com", HOSTS, extras=[]) is False
    assert is_allowed("http://notplum-amp100", HOSTS, extras=[]) is False


def test_a_non_http_scheme_is_refused():
    assert is_allowed("file://", HOSTS, extras=[]) is False
    assert is_allowed("null", HOSTS, extras=[]) is False  # sandboxed iframe / file:// page


@pytest.mark.parametrize("junk", ["", "   ", "http://", "not a url", "://x"])
def test_malformed_origins_are_refused_without_raising(junk):
    assert is_allowed(junk, HOSTS, extras=[]) is False


# -- the header decision ---------------------------------------------------------------------------


def test_a_request_with_no_origin_is_not_refused():
    """Peer polls, delegated routes and the loopback player-state POST carry no Origin.

    They are not browser requests, CORS never applied to them, and no headers is the correct answer —
    NOT a rejection. Rejecting here breaks mesh aggregation server-side with nothing to see anywhere.
    """
    assert origin_header(None, HOSTS, extras=[]) is None
    assert origin_header("", HOSTS, extras=[]) is None


def test_an_allowed_origin_is_echoed_not_starred():
    """`*` is what this change exists to remove, and an echo is required if credentials ever appear."""
    assert origin_header("http://198.51.100.21", HOSTS, extras=[]) == "http://198.51.100.21"


def test_a_refused_origin_gets_no_headers():
    assert origin_header("http://198.51.100.40", HOSTS, extras=[]) is None


# -- host-set derivation ---------------------------------------------------------------------------


def test_known_hosts_covers_ip_and_every_name_form():
    assert "198.51.100.21" in HOSTS
    assert {"plum-amp100", "plum-amp100.local", "plum-amp100.lan"} <= HOSTS


def test_known_hosts_tolerates_a_peer_with_no_hostname():
    """A peer on an older image sends no `hostname`; its IP must still be allowed."""
    hosts = known_hosts([UnitSnapshot("unit-old", "Old", host="198.51.100.11")])
    assert "198.51.100.11" in hosts


def test_known_hosts_tolerates_a_unit_with_no_host():
    """A unit that has not detected its own address yet must not break the whole set."""
    hosts = known_hosts([UnitSnapshot("unit-x", "X", host=None, hostname=None)])
    assert hosts == set(cors_policy.LOOPBACK_HOSTS)


def test_known_hosts_reads_a_live_mesh_view():
    """The wiring the API actually uses — the aggregator's view, which includes this unit itself."""
    hosts = known_hosts(MeshView(_units()).units)
    assert "198.51.100.20" in hosts


def test_name_forms_normalises_case_and_trailing_dot():
    assert name_forms("Plum-Amp100.local.") == {
        "plum-amp100.local", "plum-amp100", "plum-amp100.lan",
    }


def test_extras_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("PLUM_ALLOWED_ORIGINS", "http://a:5173, http://b ")
    assert cors_policy.configured_extra_origins() == ["http://a:5173", "http://b"]
    assert is_allowed("http://a:5173", HOSTS) is True


def test_no_environment_means_no_extras(monkeypatch):
    monkeypatch.delenv("PLUM_ALLOWED_ORIGINS", raising=False)
    assert cors_policy.configured_extra_origins() == []


# -- own_hosts(): the API with no mesh view to derive a host set from --------------------------------


def test_own_hosts_includes_the_real_lan_ip(monkeypatch):
    """The gap found on .201.133: :5002 refused the unit's OWN IP.

    It resolved its identity via gethostbyname(gethostname()), which inside the container answers
    127.0.1.1 from /etc/hosts — so the LAN address a person actually types never entered the set.
    """
    monkeypatch.setattr(cors_policy, "local_ip", lambda: "192.0.2.10")
    hosts = cors_policy.own_hosts()
    assert "192.0.2.10" in hosts
    assert is_allowed("http://192.0.2.10", hosts, extras=[]) is True


def test_own_hosts_still_covers_the_hostname_forms(monkeypatch):
    monkeypatch.setattr(cors_policy, "local_ip", lambda: "10.0.0.5")
    monkeypatch.setattr(cors_policy.socket, "gethostname", lambda: "Plum-Test-Pi4-02")
    hosts = cors_policy.own_hosts()
    assert {"plum-test-pi4-02", "plum-test-pi4-02.local"} <= hosts


def test_own_hosts_survives_a_box_with_no_route(monkeypatch):
    """No default route yet (early boot) must not empty the set and lock out loopback."""
    monkeypatch.setattr(cors_policy, "local_ip", lambda: None)
    assert set(cors_policy.LOOPBACK_HOSTS) <= cors_policy.own_hosts()
