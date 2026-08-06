"""Unit tests for per-source config resolution + rendering (spotify_config, airplay_config).

Pure-logic: endpoint filtering, port/UDP-block allocation, instance derivation, and the template
substitution that produces each daemon's config file. This is the layer that decides which
endpoints come up and on which ports — a silent off-by-one here means two daemons fighting for a
socket, which only shows up on hardware. Run: `pytest tests/Unit`.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "scripts"))

from sources import airplay_config as ac  # noqa: E402
from sources import spotify_config as sc  # noqa: E402


def _spotify_settings(endpoints, bitrate=320):
    return {"integrations": {"spotify": {"bitrate": bitrate, "endpoints": endpoints}}}


def _airplay_settings(endpoints):
    return {"integrations": {"airplay": {"endpoints": endpoints}}}


# -- enabled_endpoints filtering ---------------------------------------------------------------


def test_enabled_endpoints_drops_disabled_and_caps():
    eps = [{"id": str(i), "enabled": i % 2 == 0} for i in range(1, 30)]  # evens enabled
    enabled = sc.enabled_endpoints(_spotify_settings(eps))
    assert all(e["enabled"] for e in enabled)
    assert len(enabled) <= sc.MAX_ENDPOINTS  # capped even though many are enabled
    # The cap is applied to the FIRST MAX_ENDPOINTS raw endpoints, then filtered — mirror that.
    assert enabled == [e for e in eps[: sc.MAX_ENDPOINTS] if e.get("enabled")]


def test_enabled_endpoints_missing_section_is_empty():
    assert sc.enabled_endpoints({}) == []
    assert ac.enabled_endpoints({"integrations": {}}) == []


# -- port allocation ---------------------------------------------------------------------------


def test_spotify_api_port_is_stable_per_id():
    assert sc.api_port_for("1") == sc.API_PORT_BASE
    assert sc.api_port_for("3") == sc.API_PORT_BASE + 2
    assert sc.api_port_for("bogus") == sc.API_PORT_BASE  # non-numeric falls back, never crashes


def test_airplay_ports_do_not_overlap_between_adjacent_endpoints():
    # Endpoint N's UDP block must clear endpoint N-1's block + its range, or two shairports collide.
    assert ac.UDP_PORT_STRIDE >= 10  # the template's udp_port_range
    p1, p2 = ac.port_for("1"), ac.port_for("2")
    u1, u2 = ac.udp_port_base_for("1"), ac.udp_port_base_for("2")
    assert p2 == p1 + 1
    assert u2 - u1 == ac.UDP_PORT_STRIDE
    assert ac.port_for("bogus") == ac.PORT_BASE  # non-numeric fallback


# -- instance derivation -----------------------------------------------------------------------


def test_spotify_instance_fields():
    (inst,) = sc.instances_from_settings(
        _spotify_settings([{"id": "2", "enabled": True, "deviceName": "Den", "zeroconfPort": 5355}]),
        config_root="/tmp/glr",
    )
    assert inst.instance_id == "2"
    assert inst.source_id == "spotify-2"
    assert inst.device_name == "Den"
    assert inst.fifo_path == "/tmp/spotify-2-fifo"
    assert inst.api_port == sc.API_PORT_BASE + 1
    assert inst.api_base == f"http://127.0.0.1:{sc.API_PORT_BASE + 1}"
    assert inst.config_dir == "/tmp/glr/2"


def test_airplay_instance_fields_and_derived_paths():
    (inst,) = ac.instances_from_settings(
        _airplay_settings([{"id": "1", "enabled": True, "deviceName": "Kitchen"}]),
        config_root="/tmp/sp",
    )
    assert inst.source_id == "airplay-1"
    assert inst.fifo_path == "/tmp/airplay-1-fifo"
    assert inst.metadata_fifo == "/tmp/airplay-1-metadata-fifo"
    assert inst.config_path == "/tmp/sp/1/shairport-sync.conf"
    # The private MPRIS bus lives under the instance's config dir — the whole multi-endpoint trick.
    assert inst.bus_address == "unix:path=/tmp/sp/1/dbus.socket"


def test_airplay_instance_honours_stored_ports_but_falls_back_to_derived():
    settings = _airplay_settings([
        {"id": "1", "enabled": True, "deviceName": "A", "port": 5090, "udpPortBase": 6500},
        {"id": "2", "enabled": True, "deviceName": "B"},  # no ports stored → derived
    ])
    a, b = ac.instances_from_settings(settings)
    assert (a.port, a.udp_port_base) == (5090, 6500)  # stored wins
    assert (b.port, b.udp_port_base) == (ac.port_for("2"), ac.udp_port_base_for("2"))


# -- rendering (writes real files under tmp) ---------------------------------------------------


def test_spotify_render_substitutes_every_placeholder(tmp_path):
    settings = _spotify_settings(
        [{"id": "1", "enabled": True, "deviceName": "Living Room", "zeroconfPort": 5354}], bitrate=160
    )
    (inst,) = sc.render_configs(
        settings,
        template_path=str(REPO / "backend" / "config" / "go-librespot.yml.template"),
        config_root=str(tmp_path),
    )
    body = (tmp_path / "1" / "config.yml").read_text()
    assert "Living Room" in body
    assert "160" in body  # bitrate
    assert str(inst.api_port) in body
    # No placeholder tokens survive substitution.
    for token in ("SPOTIFY_NAME", "SPOTIFY_BITRATE", "SPOTIFY_API_PORT", "INSTANCE_ID"):
        assert token not in body, f"unsubstituted {token}"


def test_airplay_render_substitutes_every_placeholder(tmp_path):
    settings = _airplay_settings([{"id": "2", "enabled": True, "deviceName": "Patio"}])
    (inst,) = ac.render_configs(
        settings,
        template_path=str(REPO / "backend" / "config" / "shairport-sync.conf.template"),
        config_root=str(tmp_path),
    )
    body = (tmp_path / "2" / "shairport-sync.conf").read_text()
    assert 'name = "Patio"' in body
    assert f"port = {inst.port}" in body
    assert f"udp_port_base = {inst.udp_port_base}" in body
    for token in ("PLUM_AP_NAME", "PLUM_AP_PORT", "PLUM_AP_UDP_BASE", "PLUM_AP_ID"):
        assert token not in body, f"unsubstituted {token}"
