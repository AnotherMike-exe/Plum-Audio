"""Unit tests for the loudness-calibration REST surface.

Two properties here are worth more than the endpoint coverage.

`test_flat_measurements_are_refused` is the predecessor's exact failure, caught at the boundary
where it is still explicable. Plum-Snapcast's tone bypassed the volume stage entirely, so every
measurement read the same SPL; it stored that happily and produced NaN volumes downstream. A flat
or inverted response means the tone was not coming from the endpoint being measured, and the only
place that is recoverable is the moment the user presses Save.

`test_two_concurrent_saves_both_survive` guards the reason `SettingsManager.mutate` exists at all.
The map is keyed by player id, so a get-then-post would let two browsers calibrating two speakers
each read the same map and the second silently drop the first — with a bumped version, so no
poller would ever reconcile it.

Run: `pytest tests/Unit/test_calibration_api.py`.
"""

import json
import math
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "scripts"))
sys.path.insert(0, str(REPO / "backend" / "scripts" / "apis"))

flask = pytest.importorskip("flask", reason="Flask is a real runtime dep; skipped on a bare checkout")

import settings_api  # noqa: E402
from calibration_api import create_calibration_blueprint  # noqa: E402
from settings_api import SettingsManager  # noqa: E402

BASE = "/api/audio/calibration"


def _samples(offset_db=30.0, volumes=(35, 60, 85)):
    """Measurements from a perfect amplitude scaler sitting `offset_db` from reference."""
    return [{"volume": v, "db": 20.0 * math.log10(v) + offset_db} for v in volumes]


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"version": 1, "deviceName": "Test Unit"}))
    monkeypatch.setenv("PLUM_SETTINGS_FILE", str(path))
    monkeypatch.setattr(settings_api, "SETTINGS_FILE", str(path))
    return path


@pytest.fixture
def manager(settings_file):
    return SettingsManager(str(settings_file))


@pytest.fixture
def client(manager):
    app = flask.Flask(__name__)
    app.register_blueprint(create_calibration_blueprint(manager))
    return app.test_client()


# -- reading ------------------------------------------------------------------


def test_a_fresh_unit_has_no_calibrations_and_the_default_policy(client):
    body = client.get(BASE).get_json()
    assert body["calibrations"] == {}
    assert body["policy"]["mode"] == "follow"
    assert body["policy"]["sets"] == []
    assert body["minSamples"] == 2
    assert body["maxSamples"] == 5


# -- saving a record ----------------------------------------------------------


def test_saving_a_record_returns_the_derived_curve(client):
    resp = client.put(f"{BASE}/player-a", json={"name": "Kitchen", "samples": _samples()})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["calibrated"] is True
    assert body["curve"]["a"] == pytest.approx(20.0, abs=1e-6)
    assert body["curve"]["n"] == 3
    assert body["curve"]["suspect"] is False
    assert body["lastCalibrated"]


def test_a_saved_record_persists_under_the_player_id(client, settings_file):
    client.put(f"{BASE}/player-a", json={"name": "Kitchen", "samples": _samples()})
    stored = json.loads(settings_file.read_text())["audio"]["calibration"]
    assert list(stored) == ["player-a"]
    assert stored["player-a"]["name"] == "Kitchen"


def test_saving_preserves_sibling_audio_settings(client, manager):
    """The settings merge is one level deep, so a careless write here would clobber the output."""
    manager.update_settings({"audio": {"output": {"device": "HiFiBerry:0", "device_type": "HAT"}}})
    client.put(f"{BASE}/player-a", json={"samples": _samples()})
    assert manager.get_settings()["audio"]["output"]["device"] == "HiFiBerry:0"


def test_saving_a_second_endpoint_keeps_the_first(client):
    client.put(f"{BASE}/player-a", json={"name": "Kitchen", "samples": _samples()})
    client.put(f"{BASE}/player-b", json={"name": "Living", "samples": _samples(36.0)})
    assert set(client.get(BASE).get_json()["calibrations"]) == {"player-a", "player-b"}


def test_re_saving_an_endpoint_replaces_its_record(client):
    client.put(f"{BASE}/player-a", json={"name": "Kitchen", "samples": _samples()})
    client.put(f"{BASE}/player-a", json={"name": "Kitchen Renamed", "samples": _samples(36.0)})
    cals = client.get(BASE).get_json()["calibrations"]
    assert len(cals) == 1
    assert cals["player-a"]["name"] == "Kitchen Renamed"


def test_a_record_with_no_samples_is_allowed(client):
    """Saving a trim or a ceiling before measuring anything must not be an error."""
    resp = client.put(f"{BASE}/player-a", json={"name": "Kitchen", "trimDb": -3.0})
    assert resp.status_code == 200
    assert resp.get_json()["calibrated"] is False
    assert resp.get_json()["fitRejected"] is False


# -- validation ---------------------------------------------------------------


def test_flat_measurements_are_refused_with_an_explanation(client):
    """The predecessor's signature failure: a tone that bypassed the volume stage."""
    resp = client.put(
        f"{BASE}/player-a", json={"samples": [{"volume": 38, "db": 62}, {"volume": 80, "db": 62}]}
    )
    assert resp.status_code == 400
    assert resp.get_json()["fitRejected"] is True
    assert "rise with volume" in resp.get_json()["error"]


def test_inverted_measurements_are_refused(client):
    resp = client.put(
        f"{BASE}/player-a", json={"samples": [{"volume": 35, "db": 70}, {"volume": 85, "db": 55}]}
    )
    assert resp.status_code == 400


def test_too_many_samples_are_refused(client):
    rows = [{"volume": v, "db": 20.0 * math.log10(v) + 30} for v in (20, 30, 40, 50, 60, 70)]
    resp = client.put(f"{BASE}/player-a", json={"samples": rows})
    assert resp.status_code == 400
    assert "at most 5" in resp.get_json()["error"]


@pytest.mark.parametrize(
    "body",
    [
        {"samples": "not-a-list"},
        {"samples": [{"volume": 35}]},
        {"samples": [{"volume": 500, "db": 60}]},
        {"samples": [{"volume": 35, "db": 9999}]},
        {"maxLimit": {"mode": "nonsense", "value": 50}},
        {"maxLimit": {"mode": "percentage", "value": 150}},
        {"trimDb": 999},
        {"trimDb": "loud"},
    ],
)
def test_malformed_bodies_are_a_400_not_a_500(client, body):
    assert client.put(f"{BASE}/player-a", json=body).status_code == 400


def test_a_non_object_body_is_refused(client):
    assert client.put(f"{BASE}/player-a", json=[1, 2, 3]).status_code == 400


def test_a_name_is_bounded_and_stripped_of_control_characters(client):
    resp = client.put(f"{BASE}/player-a", json={"name": "Kit\x00chen\n" + "x" * 500})
    assert resp.status_code == 200
    name = resp.get_json()["name"]
    assert "\x00" not in name and "\n" not in name
    assert len(name) <= 120


# -- deleting -----------------------------------------------------------------


def test_deleting_removes_the_record(client):
    client.put(f"{BASE}/player-a", json={"samples": _samples()})
    assert client.delete(f"{BASE}/player-a").status_code == 200
    assert client.get(BASE).get_json()["calibrations"] == {}


def test_deleting_an_unknown_endpoint_is_a_404(client):
    assert client.delete(f"{BASE}/nobody").status_code == 404


def test_a_no_op_delete_does_not_bump_the_settings_version(client, manager):
    """A version bump with no change churns every GUI's settings poll for nothing."""
    before = manager.get_settings()["version"]
    client.delete(f"{BASE}/nobody")
    assert manager.get_settings()["version"] == before


# -- the match policy ---------------------------------------------------------


def test_policy_mode_round_trips(client):
    assert client.put(f"{BASE}/policy", json={"mode": "stream"}).status_code == 200
    assert client.get(BASE).get_json()["policy"]["mode"] == "stream"


def test_policy_sets_round_trip(client):
    body = {
        "mode": "sets",
        "sets": [
            {"id": "s1", "name": "Open plan", "members": ["kitchen", "living"]},
            {"id": "s2", "name": "Upstairs", "members": ["office"]},
        ],
    }
    assert client.put(f"{BASE}/policy", json=body).status_code == 200
    policy = client.get(BASE).get_json()["policy"]
    assert policy["mode"] == "sets"
    assert [s["id"] for s in policy["sets"]] == ["s1", "s2"]
    assert policy["sets"][0]["members"] == ["kitchen", "living"]


@pytest.mark.parametrize(
    "body",
    [
        {"mode": "nonsense"},
        {"mode": "sets", "sets": "not-a-list"},
        {"mode": "sets", "sets": [{"name": "no id"}]},
        {"mode": "sets", "sets": [{"id": "a"}, {"id": "a"}]},
        {"mode": "sets", "sets": [{"id": "a", "members": "not-a-list"}]},
    ],
)
def test_malformed_policies_are_a_400(client, body):
    assert client.put(f"{BASE}/policy", json=body).status_code == 400


def test_the_policy_does_not_disturb_stored_calibrations(client):
    client.put(f"{BASE}/player-a", json={"samples": _samples()})
    client.put(f"{BASE}/policy", json={"mode": "stream"})
    assert "player-a" in client.get(BASE).get_json()["calibrations"]


# -- derived reporting --------------------------------------------------------


def test_the_reported_range_never_starts_at_silence(client):
    """Volume 0 is silence; the predecessor printed a fabricated finite dB for it."""
    body = client.put(f"{BASE}/player-a", json={"samples": _samples()}).get_json()
    assert body["dbRange"]["lowVolume"] == 10.0
    assert body["dbRange"]["highDb"] > body["dbRange"]["lowDb"]


def test_a_decibel_ceiling_is_reported_as_a_resolved_percentage(client):
    body = client.put(
        f"{BASE}/player-a",
        json={"samples": _samples(), "maxLimit": {"mode": "decibel", "value": 60.0}},
    ).get_json()
    # 20*log10(v) + 30 == 60  ->  v == 31.6
    assert body["effectiveMaxVolume"] == pytest.approx(31.62, abs=0.05)
    assert body["dbRange"]["highDb"] == pytest.approx(60.0, abs=0.01)


def test_scattered_measurements_are_flagged_suspect_not_hidden(client):
    body = client.put(
        f"{BASE}/player-a",
        json={"samples": [{"volume": 30, "db": 50}, {"volume": 60, "db": 62}, {"volume": 90, "db": 55}]},
    ).get_json()
    assert body["calibrated"] is True
    assert body["curve"]["suspect"] is True


# -- concurrency --------------------------------------------------------------


def test_two_concurrent_saves_both_survive(manager, settings_file):
    """Why SettingsManager.mutate exists. Flask is threaded=True, so this is a real race."""
    app = flask.Flask(__name__)
    app.register_blueprint(create_calibration_blueprint(manager))

    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def save(player_id: str, offset: float):
        try:
            barrier.wait(timeout=5)
            with app.test_client() as c:
                assert c.put(f"{BASE}/{player_id}", json={"samples": _samples(offset)}).status_code == 200
        except BaseException as exc:  # noqa: BLE001 - re-raised on the main thread below
            errors.append(exc)

    threads = [
        threading.Thread(target=save, args=("player-a", 30.0)),
        threading.Thread(target=save, args=("player-b", 36.0)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    stored = json.loads(settings_file.read_text())["audio"]["calibration"]
    assert set(stored) == {"player-a", "player-b"}


# -- causal revision ----------------------------------------------------------


def test_each_save_allocates_the_next_rev(client):
    first = client.put(f"{BASE}/player-a", json={"samples": _samples()}).get_json()
    second = client.put(f"{BASE}/player-a", json={"samples": _samples(36.0)}).get_json()
    assert first["rev"] == 1
    assert second["rev"] == 2


def test_a_clients_known_rev_lifts_the_counter_past_a_peers(client):
    """The client holds the merged cross-unit view; this process cannot reach the mesh. Claiming a
    peer's higher rev is how a save made here outranks a record written on another unit."""
    saved = client.put(f"{BASE}/player-a", json={"samples": _samples(), "knownRev": 7}).get_json()
    assert saved["rev"] == 8


def test_a_stale_known_rev_still_beats_the_local_record(client):
    client.put(f"{BASE}/player-a", json={"samples": _samples()})  # rev 1
    client.put(f"{BASE}/player-a", json={"samples": _samples()})  # rev 2
    saved = client.put(f"{BASE}/player-a", json={"samples": _samples(), "knownRev": 0}).get_json()
    assert saved["rev"] == 3, "must never regress below what is already stored locally"


def test_endpoints_have_independent_revisions(client):
    client.put(f"{BASE}/player-a", json={"samples": _samples(), "knownRev": 20})
    other = client.put(f"{BASE}/player-b", json={"samples": _samples()}).get_json()
    assert other["rev"] == 1


@pytest.mark.parametrize("bad", [{"knownRev": -1}, {"knownRev": "many"}, {"knownRev": 10**9}])
def test_a_malformed_known_rev_is_a_400(client, bad):
    assert client.put(f"{BASE}/player-a", json={"samples": _samples(), **bad}).status_code == 400


def test_concurrent_saves_of_one_endpoint_do_not_share_a_rev(manager, settings_file):
    """Allocation happens inside the settings lock, so two racing saves cannot both claim the same
    number and fall back to a timestamp comparison."""
    app = flask.Flask(__name__)
    app.register_blueprint(create_calibration_blueprint(manager))
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def save():
        try:
            barrier.wait(timeout=5)
            with app.test_client() as c:
                c.put(f"{BASE}/player-a", json={"samples": _samples()})
        except BaseException as exc:  # noqa: BLE001 - re-raised on the main thread
            errors.append(exc)

    threads = [threading.Thread(target=save) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    assert json.loads(settings_file.read_text())["audio"]["calibration"]["player-a"]["rev"] == 2
