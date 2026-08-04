"""Unit tests for the audio output REST surface and its settings migration.

Two things here are worth more than the endpoint coverage.

`test_legacy_placeholder_is_cleared_*` guards a change that touches EVERY existing unit at once.
Each one on the rig has `audio.output.device = "hw:Headphones"` sitting in settings.json — the
shipped default of a tab that was never wired up, so it was never a user's choice. The moment the
player starts preferring settings.json over PLUM_DAC_DEVICE (Phase 3) that placeholder outranks the
env everywhere simultaneously and resolves to nothing. It has to be cleared on read, not just
defaulted differently for new units.

`test_setting_an_unopenable_device_is_refused` guards the other direction: persisting a device that
cannot be opened leaves a unit that plays fine until its next restart and is then silent, with
settings.json looking entirely correct. Refusing at the API is the only place that failure is
visible to the person causing it.

Run: `pytest tests/Unit/test_audio_api.py`.
"""

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "scripts"))
sys.path.insert(0, str(REPO / "backend" / "scripts" / "apis"))

flask = pytest.importorskip("flask", reason="Flask is a real runtime dep; skipped on a bare checkout")

import audio_devices  # noqa: E402
import settings_api  # noqa: E402
from audio_api import create_audio_blueprint  # noqa: E402
from audio_devices import AudioDevice, DeviceType  # noqa: E402
from settings_api import SettingsManager  # noqa: E402


def _device(card=2, card_name="sndrpihifiberry", available=True, active=False):
    return AudioDevice(
        card=card,
        device=0,
        card_name=card_name,
        card_description="snd_rpi_hifiberry_dacplus",
        device_description="HiFiBerry DAC+ Pro HiFi pcm512x-hifi-0",
        type=DeviceType.HAT,
        friendly_name="HiFiBerry DAC+ Pro (HAT)",
        is_available=available,
        unavailable_reason=None if available else "PortAudio cannot open this device.",
        is_active=active,
    )


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"version": 1, "deviceName": "Test Unit"}))
    monkeypatch.setenv("PLUM_SETTINGS_FILE", str(path))
    monkeypatch.setattr(settings_api, "SETTINGS_FILE", str(path))
    return path


@pytest.fixture
def client(settings_file, monkeypatch):
    monkeypatch.delenv("PLUM_DAC_DEVICE", raising=False)
    app = flask.Flask(__name__)
    app.register_blueprint(create_audio_blueprint(SettingsManager(str(settings_file))))
    return app.test_client()


def _present(monkeypatch, devices):
    monkeypatch.setattr(audio_devices, "list_output_devices", lambda **k: devices)


# -- the migration ---------------------------------------------------------------------------------


def test_legacy_placeholder_is_cleared_on_read(settings_file):
    settings_file.write_text(
        json.dumps(
            {
                "version": 3,
                "audio": {
                    "output": {
                        "device": "hw:Headphones",
                        "device_type": "BUILTIN_HEADPHONES",
                        "fallback_device": "hw:Headphones",
                    }
                },
            }
        )
    )
    output = SettingsManager(str(settings_file)).get_settings()["audio"]["output"]

    assert output["device"] is None  # ...so PLUM_DAC_DEVICE wins again
    assert output["device_type"] is None
    assert "fallback_device" not in output


def test_legacy_placeholder_clearing_is_persisted(settings_file):
    settings_file.write_text(
        json.dumps({"version": 3, "audio": {"output": {"device": "hw:Headphones"}}})
    )
    SettingsManager(str(settings_file)).get_settings()

    # Written back, not just fixed in memory: the player reads this file directly.
    assert json.loads(settings_file.read_text())["audio"]["output"]["device"] is None


def test_a_real_choice_is_never_migrated_away(settings_file):
    settings_file.write_text(
        json.dumps({"version": 3, "audio": {"output": {"device": "sndrpihifiberry:0"}}})
    )
    output = SettingsManager(str(settings_file)).get_settings()["audio"]["output"]
    assert output["device"] == "sndrpihifiberry:0"


def test_default_settings_ship_without_an_output_device():
    """A plausible-looking default would silently outrank PLUM_DAC_DEVICE on every unit."""
    assert settings_api.DEFAULT_SETTINGS["audio"]["output"]["device"] is None


# -- precedence ------------------------------------------------------------------------------------


def test_settings_outrank_the_environment(settings_file, monkeypatch):
    monkeypatch.setenv("PLUM_DAC_DEVICE", "bcm2835")
    settings_file.write_text(json.dumps({"audio": {"output": {"device": "sndrpihifiberry:0"}}}))
    assert audio_devices.configured_output_spec() == "sndrpihifiberry:0"


def test_environment_is_the_pre_configuration_default(settings_file, monkeypatch):
    monkeypatch.setenv("PLUM_DAC_DEVICE", "bcm2835")
    settings_file.write_text(json.dumps({"audio": {"output": {"device": None}}}))
    assert audio_devices.configured_output_spec() == "bcm2835"


def test_an_unreadable_settings_file_falls_back_rather_than_raising(monkeypatch, tmp_path):
    """The audio process must come up and play even with settings.json missing or mid-write."""
    monkeypatch.setenv("PLUM_SETTINGS_FILE", str(tmp_path / "nope.json"))
    monkeypatch.setenv("PLUM_DAC_DEVICE", "bcm2835")
    assert audio_devices.configured_output_spec() == "bcm2835"


# -- endpoints ---------------------------------------------------------------------------------------


def test_lists_devices(client, monkeypatch):
    _present(monkeypatch, [_device(active=True)])
    body = client.get("/api/audio/devices/output").get_json()
    assert len(body) == 1
    assert body[0]["id"] == "sndrpihifiberry:0"
    assert body[0]["is_active"] is True


def test_setting_a_device_persists_the_stable_id_not_the_hw_address(client, monkeypatch, settings_file):
    _present(monkeypatch, [_device()])
    response = client.post("/api/audio/output/device", json={"id": "sndrpihifiberry:0"})

    assert response.status_code == 200
    stored = json.loads(settings_file.read_text())["audio"]["output"]
    assert stored["device"] == "sndrpihifiberry:0"
    assert not stored["device"].startswith("hw:")  # card numbers move; the id must not


def test_setting_a_device_reports_itself_as_pending(client, monkeypatch):
    """A 200 means SAVED. The player applies it on its next poll, a second or so later."""
    _present(monkeypatch, [_device()])
    body = client.post("/api/audio/output/device", json={"id": "sndrpihifiberry:0"}).get_json()
    assert body["success"] is True
    assert body["pending"] is True


def test_setting_an_unopenable_device_is_refused(client, monkeypatch, settings_file):
    _present(monkeypatch, [_device(available=False)])
    response = client.post("/api/audio/output/device", json={"id": "sndrpihifiberry:0"})

    assert response.status_code == 409
    # And nothing was written — a saved-but-unopenable device is silence after the next restart.
    stored = json.loads(settings_file.read_text())
    assert (stored.get("audio", {}).get("output", {}).get("device")) is None


def test_setting_an_absent_device_is_a_404(client, monkeypatch):
    _present(monkeypatch, [_device()])
    assert client.post("/api/audio/output/device", json={"id": "no-such:0"}).status_code == 404


def test_setting_without_an_id_is_a_400(client):
    assert client.post("/api/audio/output/device", json={}).status_code == 400


def test_current_output_resolves_against_present_hardware(client, monkeypatch, settings_file):
    settings_file.write_text(json.dumps({"audio": {"output": {"device": "sndrpihifiberry:0"}}}))
    _present(monkeypatch, [_device(active=True)])

    body = client.get("/api/audio/output/current").get_json()
    assert body["resolved"] is True
    assert body["friendly_name"] == "HiFiBerry DAC+ Pro (HAT)"


def test_current_output_says_so_when_the_configured_device_is_gone(client, monkeypatch, settings_file):
    """A HAT pulled, or a spec copied from another unit. Do not silently substitute a working one."""
    settings_file.write_text(json.dumps({"audio": {"output": {"device": "sndrpihifiberry:0"}}}))
    _present(monkeypatch, [_device(card_name="Headphones")])

    body = client.get("/api/audio/output/current").get_json()
    assert body["resolved"] is False
    assert body["is_available"] is False
    assert "not attached to this unit" in body["unavailable_reason"]


def test_test_tone_refuses_the_active_device_with_an_explanation(client, monkeypatch):
    monkeypatch.setattr(audio_devices, "test_device", lambda *a, **k: (False, "X is in use — it is already this unit's output."))
    response = client.post("/api/audio/output/test", json={"id": "sndrpihifiberry:0"})
    assert response.status_code == 409
    assert "already this unit's output" in response.get_json()["message"]


def test_discovery_failure_does_not_take_down_the_tab(client, monkeypatch):
    def boom(**_):
        raise OSError("aplay exploded")

    monkeypatch.setattr(audio_devices, "list_output_devices", boom)
    assert client.get("/api/audio/devices/output").status_code == 500
