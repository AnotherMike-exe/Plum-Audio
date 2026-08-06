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


def _headphones(active=True):
    """The Pi's onboard output, with its REAL descriptions.

    They matter: `PLUM_DAC_DEVICE=bcm2835` resolves through find_device's substring pass against
    `card_description`, so a stand-in whose descriptions do not contain the fragment resolves to
    nothing and tests the wrong thing.
    """
    return AudioDevice(
        card=0,
        device=0,
        card_name="Headphones",
        card_description="bcm2835 Headphones",
        device_description="bcm2835 Headphones",
        type=DeviceType.BUILTIN_HEADPHONES,
        friendly_name="Built-in Headphones (3.5mm)",
        is_available=True,
        unavailable_reason=None,
        is_active=active,
    )


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
    """Hardware first, then the synthetic "No output" row — see the None-row tests at the end."""
    _present(monkeypatch, [_device(active=True)])
    body = client.get("/api/audio/devices/output").get_json()
    assert [d["id"] for d in body] == ["sndrpihifiberry:0", "none"]
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


def test_pending_until_the_player_echoes_the_new_device(client, monkeypatch, settings_file, tmp_path):
    """Saved is not playing. Between the two, the GUI must not claim the audio has moved."""
    state = tmp_path / "player_state.json"
    state.write_text(json.dumps({"volume": 100, "output_device": "Headphones:0"}))
    monkeypatch.setenv("PLUM_PLAYER_STATE_FILE", str(state))
    settings_file.write_text(json.dumps({"audio": {"output": {"device": "sndrpihifiberry:0"}}}))
    _present(monkeypatch, [_device()])

    body = client.get("/api/audio/output/current").get_json()
    assert body["pending"] is True
    assert body["playing_on"] == "Headphones:0"  # still the old one
    assert body["configured"] == "sndrpihifiberry:0"


def test_not_pending_once_the_echo_matches(client, monkeypatch, settings_file, tmp_path):
    state = tmp_path / "player_state.json"
    state.write_text(json.dumps({"volume": 100, "output_device": "sndrpihifiberry:0"}))
    monkeypatch.setenv("PLUM_PLAYER_STATE_FILE", str(state))
    settings_file.write_text(json.dumps({"audio": {"output": {"device": "sndrpihifiberry:0"}}}))
    _present(monkeypatch, [_device()])

    body = client.get("/api/audio/output/current").get_json()
    assert body["pending"] is False
    assert body["playing_on"] == "sndrpihifiberry:0"


def test_a_switch_that_never_opened_stays_pending(client, monkeypatch, settings_file, tmp_path):
    """The renderer restored the old device, so the echo never moves — and `pending` stays true.

    This is the case that must never render as success: settings.json says one thing, the speaker is
    audibly playing another, and only the echo can tell them apart.
    """
    state = tmp_path / "player_state.json"
    state.write_text(json.dumps({"output_device": "Headphones:0"}))
    monkeypatch.setenv("PLUM_PLAYER_STATE_FILE", str(state))
    settings_file.write_text(json.dumps({"audio": {"output": {"device": "vc4hdmi0:0"}}}))
    _present(monkeypatch, [_device(available=False)])

    body = client.get("/api/audio/output/current").get_json()
    assert body["pending"] is True
    assert body["playing_on"] == "Headphones:0"


def test_no_echo_yet_is_not_reported_as_pending(client, monkeypatch, settings_file, tmp_path):
    """A player that has never written its state (first boot) must not look mid-switch forever."""
    monkeypatch.setenv("PLUM_PLAYER_STATE_FILE", str(tmp_path / "absent.json"))
    settings_file.write_text(json.dumps({"audio": {"output": {"device": "sndrpihifiberry:0"}}}))
    _present(monkeypatch, [_device()])

    assert client.get("/api/audio/output/current").get_json()["pending"] is False


def test_a_PLUM_DAC_DEVICE_fragment_matching_the_echo_is_not_pending(client, monkeypatch, settings_file, tmp_path):
    """`pending` compares RESOLVED identities, not the strings that named them.

    Every other test here configures a `<card_name>:<device>` id, which is what the GUI writes — so
    the raw string compare happened to work and this was invisible. A unit whose output comes from
    PLUM_DAC_DEVICE is named by a PortAudio *fragment* ("bcm2835"), while the player's echo is always
    the resolved id ("Headphones:0"). Comparing those two reported a switch pending FOREVER, to the
    device the unit was already playing on: the Audio tab showed "switching to Built-in Headphones"
    beside that same device's own "playing" tag, on a unit where nothing had ever been changed.

    Seen on both mesh-pair units after the greenfield alpha deploy, 2026-08-06 — i.e. on every unit that
    has never had an output picked in the GUI.
    """
    state = tmp_path / "player_state.json"
    state.write_text(json.dumps({"output_device": "Headphones:0"}))
    monkeypatch.setenv("PLUM_PLAYER_STATE_FILE", str(state))
    # No audio.output.device at all — so configured_output_spec() falls back to the environment.
    settings_file.write_text(json.dumps({"version": 1, "audio": {"output": {"device": None}}}))
    monkeypatch.setenv("PLUM_DAC_DEVICE", "bcm2835")
    _present(monkeypatch, [_headphones()])

    body = client.get("/api/audio/output/current").get_json()
    assert body["configured"] == "bcm2835"       # the fragment, unresolved
    assert body["playing_on"] == "Headphones:0"  # the echo, resolved
    assert body["id"] == "Headphones:0"          # and they are the SAME device
    assert body["resolved"] is True
    assert body["pending"] is False, "a unit playing on exactly the device it is configured for"


def test_a_fragment_naming_a_DIFFERENT_card_than_the_echo_is_still_pending(
    client, monkeypatch, settings_file, tmp_path
):
    """The fix must not blunt the real signal: resolving both sides still has to detect a mismatch."""
    state = tmp_path / "player_state.json"
    state.write_text(json.dumps({"output_device": "Headphones:0"}))
    monkeypatch.setenv("PLUM_PLAYER_STATE_FILE", str(state))
    settings_file.write_text(json.dumps({"version": 1, "audio": {"output": {"device": None}}}))
    monkeypatch.setenv("PLUM_DAC_DEVICE", "snd_rpi_hifiberry_dacplus")
    _present(monkeypatch, [_headphones(active=False), _device()])

    body = client.get("/api/audio/output/current").get_json()
    assert body["id"] == "sndrpihifiberry:0"      # the fragment resolves to the HAT...
    assert body["playing_on"] == "Headphones:0"   # ...but the player has the onboard jack open
    assert body["pending"] is True


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


# -- "No output": the unit that runs no player ------------------------------------------------------


def _echo(monkeypatch, tmp_path, output_device):
    state = tmp_path / "player_state.json"
    state.write_text(json.dumps({"volume": 100, "output_device": output_device}))
    monkeypatch.setenv("PLUM_PLAYER_STATE_FILE", str(state))
    return state


def test_the_none_row_is_offered_on_a_unit_with_hardware(client, monkeypatch):
    """Turning any unit into an ingest-only node is a choice, not just a fallback."""
    _present(monkeypatch, [_device()])
    rows = client.get("/api/audio/devices/output").get_json()

    assert [r["id"] for r in rows] == ["sndrpihifiberry:0", "none"]
    none_row = rows[-1]
    assert none_row["type"] == "NONE"
    assert none_row["is_available"] is True
    assert none_row["is_active"] is False


def test_the_none_row_is_the_only_row_on_a_card_less_host(client, monkeypatch, settings_file):
    settings_file.write_text(json.dumps({"audio": {"output": {"device": "none"}}}))
    _present(monkeypatch, [])
    rows = client.get("/api/audio/devices/output").get_json()

    assert len(rows) == 1
    assert rows[0]["id"] == "none"
    assert rows[0]["is_active"] is True


def test_selecting_no_output_never_enumerates_devices(client, monkeypatch, settings_file, tmp_path):
    """Proves the short-circuit is BEFORE list_output_devices, and so before the 404 that follows."""
    _echo(monkeypatch, tmp_path, "sndrpihifiberry:0")
    monkeypatch.setattr(
        audio_devices, "list_output_devices", lambda **k: pytest.fail("enumerated for the sentinel")
    )

    resp = client.post("/api/audio/output/device", json={"id": "none"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    assert body["restart_required"] is True
    assert "restart" in body["message"].lower()
    assert json.loads(settings_file.read_text())["audio"]["output"]["device"] == "none"


def test_no_output_reads_as_resolved_not_as_a_missing_device(client, monkeypatch, settings_file, tmp_path):
    """Without its own branch this renders as "Not present ('none')" — a fault, not a choice."""
    _echo(monkeypatch, tmp_path, "none")
    settings_file.write_text(json.dumps({"audio": {"output": {"device": "none"}}}))
    _present(monkeypatch, [])

    body = client.get("/api/audio/output/current").get_json()
    assert body["configured"] == "none"
    assert body["resolved"] is True
    assert body["friendly_name"] == "No output"
    assert body["pending"] is False
    assert body["restart_required"] is False
    assert body["is_active"] is True


def test_leaving_no_output_is_restart_required(client, monkeypatch, settings_file, tmp_path):
    _echo(monkeypatch, tmp_path, "none")
    settings_file.write_text(json.dumps({"audio": {"output": {"device": "sndrpihifiberry:0"}}}))
    _present(monkeypatch, [_device()])

    body = client.get("/api/audio/output/current").get_json()
    assert body["pending"] is True
    assert body["restart_required"] is True


def test_entering_no_output_is_restart_required(client, monkeypatch, settings_file, tmp_path):
    _echo(monkeypatch, tmp_path, "sndrpihifiberry:0")
    settings_file.write_text(json.dumps({"audio": {"output": {"device": "none"}}}))
    _present(monkeypatch, [_device()])

    body = client.get("/api/audio/output/current").get_json()
    assert body["pending"] is True
    assert body["restart_required"] is True
    assert body["playing_on"] == "sndrpihifiberry:0"  # still audible until the restart


def test_a_device_to_device_switch_still_applies_live(client, monkeypatch, settings_file, tmp_path):
    """The existing live-switch path must not start demanding a restart."""
    _echo(monkeypatch, tmp_path, "Headphones:0")
    settings_file.write_text(json.dumps({"audio": {"output": {"device": "sndrpihifiberry:0"}}}))
    _present(monkeypatch, [_device()])

    body = client.get("/api/audio/output/current").get_json()
    assert body["pending"] is True
    assert body["restart_required"] is False


def test_no_echo_yet_never_claims_a_restart_is_needed(client, monkeypatch, settings_file, tmp_path):
    """A player that has not reported is unknown, not wrong — same guard as `pending`."""
    monkeypatch.setenv("PLUM_PLAYER_STATE_FILE", str(tmp_path / "absent.json"))
    settings_file.write_text(json.dumps({"audio": {"output": {"device": "none"}}}))
    _present(monkeypatch, [])

    body = client.get("/api/audio/output/current").get_json()
    assert body["restart_required"] is False


def test_choosing_a_device_while_playerless_asks_for_a_restart(client, monkeypatch, settings_file, tmp_path):
    """Coming BACK needs a restart too — there is no player process to hand the device to."""
    _echo(monkeypatch, tmp_path, "none")
    _present(monkeypatch, [_device()])

    body = client.post("/api/audio/output/device", json={"id": "sndrpihifiberry:0"}).get_json()
    assert body["success"] is True
    assert body["restart_required"] is True
    assert "restart" in body["message"].lower()


def test_choosing_a_device_on_a_normal_unit_does_not_ask_for_a_restart(client, monkeypatch, tmp_path):
    _echo(monkeypatch, tmp_path, "Headphones:0")
    _present(monkeypatch, [_device()])

    body = client.post("/api/audio/output/device", json={"id": "sndrpihifiberry:0"}).get_json()
    assert body["restart_required"] is False
    assert body["message"] == "Output set to HiFiBerry DAC+ Pro (HAT)"


def test_testing_no_output_is_refused(client, monkeypatch):
    monkeypatch.setattr(audio_devices, "test_device", lambda *a, **k: pytest.fail("ran speaker-test"))
    resp = client.post("/api/audio/output/test", json={"id": "none"})
    assert resp.status_code == 409
    assert "no output to test" in resp.get_json()["message"]
