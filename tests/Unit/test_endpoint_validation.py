"""Unit tests for endpoint field validation: the id, the ports and the Bluetooth adapter.

An endpoint `id` names a directory (`os.path.join(config_root, id)` then `os.makedirs`), names a
FIFO, and lands unescaped in the shairport and go-librespot templates. `POST /api/settings` takes
arbitrary JSON, so the id is caller input. The Bluetooth `adapter` reaches the bluealsa argv.

Two layers, and each test names the one it guards:

* the API refuses a bad value with 400 and writes nothing. It does not rewrite the value;
* the renderers skip a bad endpoint that is already on disk, and keep the good ones.

`test_real_ids_are_accepted` is the guard on the allow side. The ids that exist today are the
decimal strings `EndpointsManager._next_id` makes. A pattern that refuses them stops every source.

Run: `pytest tests/Unit/test_endpoint_validation.py`.
"""

import json
import sys
from pathlib import Path

import flask
import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "scripts"))
sys.path.insert(0, str(REPO / "backend" / "scripts" / "apis"))

import settings_api  # noqa: E402
from settings_api import SettingsManager, create_settings_blueprint  # noqa: E402
from sources import airplay_config as ac  # noqa: E402
from sources import bluetooth_config as bc  # noqa: E402
from sources import endpoint_config as ec  # noqa: E402
from sources import spotify_config as sc  # noqa: E402

BAD_IDS = ["../../etc", "1/../2", "a b", "", "1\n2", '1"; x = "', ".", "..", "-rf", "x" * 33, None, True, 1.5, ["1"]]


@pytest.fixture()
def manager(tmp_path):
    return SettingsManager(str(tmp_path / "settings.json"))


@pytest.fixture()
def client(manager):
    app = flask.Flask(__name__)
    app.register_blueprint(create_settings_blueprint(manager))
    return app.test_client()


def _on_disk(manager):
    return json.loads(Path(manager.settings_file).read_text())


# -- the primitives ----------------------------------------------------------------------------


def test_real_ids_are_accepted():
    for good in ("1", "2", "10", "7", "kitchen", "amp_2", "a-b"):
        assert ec.checked_id(good) == good
    # A hand-edited file can hold a JSON number. The renderers always read it through str().
    assert ec.checked_id(3) == "3"
    # Every default endpoint id must pass.
    for section in settings_api.DEFAULT_SETTINGS["integrations"].values():
        if isinstance(section, dict):
            for endpoint in section.get("endpoints", []):
                assert ec.checked_id(endpoint["id"]) == endpoint["id"]


@pytest.mark.parametrize("bad", BAD_IDS)
def test_a_bad_id_is_refused(bad):
    with pytest.raises(ec.InvalidEndpoint):
        ec.checked_id(bad)


def test_ports_are_integers_in_range():
    assert ec.checked_port(5050, "port") == 5050
    assert ec.checked_port(65535, "port") == 65535
    for bad in (0, 80, 1023, 65536, -1, "5050; x", "abc", True, 5050.5, None, [5050]):
        with pytest.raises(ec.InvalidEndpoint):
            ec.checked_port(bad, "port")


def test_the_adapter_is_an_hci_name():
    assert ec.checked_adapter("hci0") == "hci0"
    assert ec.checked_adapter("hci12") == "hci12"
    for bad in ("hci", "hci0\n", "hci0 --help", "-i", "eth0", "hci0;reboot", "", None, 0, "hci1234"):
        with pytest.raises(ec.InvalidEndpoint):
            ec.checked_adapter(bad)


# -- the API refuses, and does not rewrite ------------------------------------------------------


@pytest.mark.parametrize("source", ["airplay", "spotify", "bluetooth", "dlna"])
@pytest.mark.parametrize("bad", ["../../etc/cron.d", "1/x", "a b", '1"'])
def test_post_settings_refuses_a_bad_endpoint_id(client, manager, source, bad):
    before = _on_disk(manager)
    patch = {"integrations": {source: {"endpoints": [{"id": bad, "enabled": True, "deviceName": "X"}]}}}
    response = client.post("/api/settings", json=patch)
    assert response.status_code == 400
    assert "id" in response.get_json()["error"]
    assert _on_disk(manager) == before  # nothing written, version not bumped


@pytest.mark.parametrize(
    ("source", "field", "bad"),
    [
        ("airplay", "port", 80),
        ("airplay", "port", "5050\nx"),
        ("airplay", "udpPortBase", 70000),
        ("spotify", "zeroconfPort", "5354 #"),
        ("spotify", "zeroconfPort", True),
        ("bluetooth", "adapter", "hci0 --io-thread-rt-priority=99"),
        ("bluetooth", "adapter", "-B"),
    ],
)
def test_post_settings_refuses_a_bad_endpoint_field(client, manager, source, field, bad):
    before = _on_disk(manager)
    endpoint = {"id": "1", "enabled": True, "deviceName": "X", field: bad}
    response = client.post("/api/settings", json={"integrations": {source: {"endpoints": [endpoint]}}})
    assert response.status_code == 400
    assert field in response.get_json()["error"]
    assert _on_disk(manager) == before


def test_post_settings_refuses_endpoints_that_are_not_a_list_of_objects(client, manager):
    for bad in ({"endpoints": "1"}, {"endpoints": ["1"]}, {"endpoints": {"id": "1"}}):
        response = client.post("/api/settings", json={"integrations": {"airplay": bad}})
        assert response.status_code == 400
    assert client.post("/api/settings", json={"integrations": None}).status_code == 400


def test_post_settings_refuses_a_bad_spotify_bitrate(client):
    patch = {"integrations": {"spotify": {"bitrate": "320\naudio_output_pipe: /etc/x", "endpoints": []}}}
    assert client.post("/api/settings", json=patch).status_code == 400


def test_post_settings_still_accepts_what_the_gui_writes(client, manager):
    """The GUI posts whole sections back. Every default must make the round trip."""
    settings = client.get("/api/settings").get_json()
    response = client.post("/api/settings", json={"integrations": settings["integrations"]})
    assert response.status_code == 200
    patch = {
        "integrations": {
            "airplay": {"endpoints": [{"id": "2", "enabled": True, "deviceName": "Patio"}]},
            "spotify": {"bitrate": 160, "endpoints": [{"id": "1", "enabled": True, "zeroconfPort": 5355}]},
            "bluetooth": {"endpoints": [{"id": "1", "enabled": True, "adapter": "hci1"}]},
        }
    }
    assert client.post("/api/settings", json=patch).status_code == 200
    assert _on_disk(manager)["integrations"]["airplay"]["endpoints"][0]["id"] == "2"


def test_a_bad_endpoint_already_on_disk_does_not_block_other_writes(client, manager):
    """Only a CHANGED section is validated, so one bad record cannot lock the whole unit."""
    data = _on_disk(manager)
    data["integrations"]["airplay"]["endpoints"] = [{"id": "../x", "enabled": True}]
    Path(manager.settings_file).write_text(json.dumps(data))

    assert client.post("/api/settings", json={"federation": {"enabled": True}}).status_code == 200
    manager.mutate(lambda s: s["audio"]["calibration"].update({"p": {}}) or True)
    # A write that keeps the bad record in a section it rewrites is refused.
    resend = {"integrations": {"airplay": {"endpoints": [{"id": "../x", "enabled": False}]}}}
    assert client.post("/api/settings", json=resend).status_code == 400


# -- the top-level device name -------------------------------------------------------------------


def test_a_device_name_that_sanitises_to_nothing_is_refused(client, manager):
    before = _on_disk(manager)
    for bad in ('"";\n', "日本語", "   ", 42, ["x"]):
        response = client.post("/api/settings", json={"deviceName": bad})
        assert response.status_code == 400, bad
    assert client.post("/api/settings/device", json={"deviceName": '";{}'}).status_code == 400
    assert _on_disk(manager) == before


def test_an_unusable_device_name_on_disk_is_replaced_on_the_next_write(manager):
    data = _on_disk(manager)
    data["deviceName"] = '";\n'
    Path(manager.settings_file).write_text(json.dumps(data))
    updated = manager.update_settings({"federation": {"enabled": True}})
    assert updated["deviceName"] == settings_api.DEFAULT_DEVICE_NAME


def test_a_usable_device_name_is_still_scrubbed_not_refused(manager):
    assert manager.update_settings({"deviceName": 'Kitchen"; x'})["deviceName"] == "Kitchen x"


# -- the renderers skip what is already on disk --------------------------------------------------


def _mixed(extra=None):
    good = {"id": "1", "enabled": True, "deviceName": "Good", **(extra or {})}
    bad = {"id": "../../escape", "enabled": True, "deviceName": "Bad"}
    return [bad, good]


def test_airplay_skips_a_bad_endpoint_and_keeps_the_good_one(tmp_path, caplog):
    root = tmp_path / "shairport"
    settings = {"integrations": {"airplay": {"endpoints": _mixed()}}}
    with caplog.at_level("WARNING"):
        instances = ac.render_configs(
            settings,
            template_path=str(REPO / "backend" / "config" / "shairport-sync.conf.template"),
            config_root=str(root),
        )
    assert [i.instance_id for i in instances] == ["1"]
    assert [i.instance_id for i in ac.instances_from_settings(settings, config_root=str(root))] == ["1"]
    assert sorted(p.name for p in root.iterdir()) == ["1"]
    assert not (tmp_path / "escape").exists()
    assert "escape" in caplog.text


def test_airplay_skips_an_endpoint_with_a_bad_port(tmp_path):
    endpoints = [
        {"id": "1", "enabled": True, "port": "5050\nrun_this = 1"},
        {"id": "2", "enabled": True, "udpPortBase": 99999},
        {"id": "3", "enabled": True, "port": 5052, "udpPortBase": 6021},
    ]
    instances = ac.instances_from_settings({"integrations": {"airplay": {"endpoints": endpoints}}})
    assert [(i.instance_id, i.port, i.udp_port_base) for i in instances] == [("3", 5052, 6021)]


def test_spotify_skips_a_bad_endpoint_and_keeps_the_good_one(tmp_path):
    root = tmp_path / "librespot"
    endpoints = [*_mixed({"zeroconfPort": 5354}), {"id": "2", "enabled": True, "zeroconfPort": "x"}]
    settings = {"integrations": {"spotify": {"bitrate": "320\nx: y", "endpoints": endpoints}}}
    instances = sc.render_configs(
        settings,
        template_path=str(REPO / "backend" / "config" / "go-librespot.yml.template"),
        config_root=str(root),
    )
    assert [i.instance_id for i in instances] == ["1"]
    assert sorted(p.name for p in root.iterdir()) == ["1"]
    body = (root / "1" / "config.yml").read_text()
    assert "x: y" not in body
    assert str(sc.DEFAULT_BITRATE) in body


def test_bluetooth_skips_a_bad_id_and_a_bad_adapter(tmp_path):
    root = tmp_path / "bluetooth"
    endpoints = [
        *_mixed({"adapter": "hci0"}),
        {"id": "2", "enabled": True, "adapter": "hci0 --help"},
        {"id": "3", "enabled": True},  # no adapter: derived from the id
    ]
    settings = {"integrations": {"bluetooth": {"endpoints": endpoints}}}
    instances = bc.render_configs(settings, config_root=str(root))
    assert [(i.instance_id, i.adapter) for i in instances] == [("1", "hci0"), ("3", "hci2")]
    assert sorted(p.name for p in root.iterdir()) == ["1", "3"]


def test_a_non_string_device_name_does_not_stop_the_render():
    settings = {"integrations": {"airplay": {"endpoints": [{"id": "1", "enabled": True, "deviceName": 7}]}}}
    (inst,) = ac.instances_from_settings(settings)
    assert isinstance(inst.device_name, str)


# -- the managers reconcile what is valid ---------------------------------------------------------
# `desired()` runs on every poll. An exception there stops ALL reconciliation for that source, so
# one bad record on disk must cost that endpoint alone.


def test_the_airplay_manager_still_starts_the_valid_endpoint(tmp_path):
    from sources.airplay_manager import AirplayManager

    manager = AirplayManager(None, settings_file=str(tmp_path / "s.json"), config_root=str(tmp_path))
    desired = manager.desired({"integrations": {"airplay": {"endpoints": _mixed()}}})
    assert list(desired) == ["1"]
    signature, instance = desired["1"]
    assert signature == ("Good", instance.port, instance.udp_port_base)


def test_the_spotify_manager_still_starts_the_valid_endpoint(tmp_path):
    from sources.spotify_manager import SpotifyManager

    manager = SpotifyManager(None, settings_file=str(tmp_path / "s.json"), config_root=str(tmp_path))
    endpoints = _mixed({"zeroconfPort": 5360})
    desired = manager.desired({"integrations": {"spotify": {"bitrate": 160, "endpoints": endpoints}}})
    assert list(desired) == ["1"]
    signature, instance = desired["1"]
    assert signature == ("Good", 5360, 160) and instance.zeroconf_port == 5360


@pytest.mark.parametrize("bad", ["fast", None, True, [320], "320\nx: y", 128])
def test_the_spotify_manager_survives_a_bad_bitrate(tmp_path, caplog, bad):
    from sources.spotify_manager import SpotifyManager

    manager = SpotifyManager(None, settings_file=str(tmp_path / "s.json"), config_root=str(tmp_path))
    settings = {"integrations": {"spotify": {"bitrate": bad, "endpoints": [{"id": "1", "enabled": True}]}}}
    sc._bitrate_warned.clear()
    with caplog.at_level("WARNING"):
        first = manager.desired(settings)
        again = manager.desired(settings)
    assert first["1"][0][2] == sc.DEFAULT_BITRATE and again["1"][0] == first["1"][0]
    assert caplog.text.count("bitrate") == 1, "one warning for a bad value, not one per poll"


def test_the_warned_set_is_capped(caplog):
    """Each distinct bad record adds a key. Without a cap the set grows for the life of the process."""
    ec._warned.clear()

    def refuse(_endpoint):
        raise ec.InvalidEndpoint("bad")

    with caplog.at_level("WARNING"):
        for n in range(500):
            assert ec.resolve("airplay", [{"id": f"../{n}"}], refuse) == []
    assert len(ec._warned) <= ec._WARNED_MAX
    assert len(caplog.records) == 500, "a new bad record is still reported"
    caplog.clear()
    with caplog.at_level("WARNING"):
        ec.resolve("airplay", [{"id": "../499"}], refuse)
    assert not caplog.records, "the same record is not reported twice in a row"
