"""Unit tests for the output gate — the decision that composes the unit's process tree.

This runs once, before supervisord, and its answer is what makes a unit playerless. Two properties
matter more than the precedence table:

`test_the_gate_never_raises` — a gate that throws takes the unit's audio with it. Every failure mode
must fall open to `device`, which is what every unit on the rig already does.

`test_the_echo_write_preserves_the_persisted_volume` — the gate writes the echo the player would
have written, because `pending`/`restart_required` are both defined as "the choice versus what the
player reports", and on a playerless unit nobody else ever reports. It must merge, not replace:
clobbering the stored volume would silently reset a unit to 100% the moment it came back to a real
device.

Run: `pytest tests/Unit/test_output_gate.py`.
"""

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "scripts"))
sys.path.insert(0, str(REPO / "backend" / "scripts" / "apis"))

import audio_devices  # noqa: E402
import output_gate  # noqa: E402
from output_gate import MODE_DEVICE, MODE_NONE, decide  # noqa: E402


# -- the precedence table (pure) --------------------------------------------------------------------


def test_the_operator_override_wins_over_present_hardware():
    """deploy.sh writes this from a units.conf row, before any settings.json exists."""
    mode, _why, auto = decide(settings_spec="sndrpihifiberry:0", stored_spec="sndrpihifiberry:0", player_enabled=False, hardware_present=True)
    assert (mode, auto) == (MODE_NONE, False)


def test_the_configured_sentinel_makes_a_unit_playerless():
    mode, _why, auto = decide(settings_spec="none", stored_spec="none", player_enabled=True, hardware_present=True)
    assert (mode, auto) == (MODE_NONE, False)


def test_no_hardware_auto_selects_no_output():
    """A genuinely card-less box: nothing STORED, so the env default does not count as a choice."""
    mode, _why, auto = decide(
        settings_spec="bcm2835", stored_spec=None, player_enabled=True, hardware_present=False
    )
    assert (mode, auto) == (MODE_NONE, True)


def test_a_stored_choice_is_never_overwritten_when_the_card_is_merely_LATE():
    """The destructive case, and not a rare one.

    An I2S HAT registers asynchronously (overlay probe + i2c) — which is the very mechanism that
    makes card numbers move on these units. If the container wins that race, the old code persisted
    `none` over the user's deliberate choice, ran no player, and never re-checked. Still answers
    `none` (a player cannot open an absent card, and crash-looping is worse) but writes NOTHING, so
    the next start with the card present recovers on its own.
    """
    mode, why, auto = decide(
        settings_spec="sndrpihifiberry:0", stored_spec="sndrpihifiberry:0",
        player_enabled=True, hardware_present=False,
    )
    assert mode == MODE_NONE
    assert auto is False  # <- the write is what auto_selected gates
    assert "keeping the stored choice" in why


def test_a_normal_unit_keeps_its_player():
    mode, _why, auto = decide(settings_spec="sndrpihifiberry:0", stored_spec="sndrpihifiberry:0", player_enabled=True, hardware_present=True)
    assert (mode, auto) == (MODE_DEVICE, False)


def test_an_unset_output_with_hardware_still_keeps_its_player():
    """settings.json ships with device=None; that means "not chosen yet", never "no output"."""
    mode, _why, auto = decide(settings_spec=None, stored_spec=None, player_enabled=True, hardware_present=True)
    assert (mode, auto) == (MODE_DEVICE, False)


# -- main(): stdout contract and side effects -------------------------------------------------------


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """A unit whose settings and player state live in tmp_path."""
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"version": 1, "deviceName": "Test Unit"}))
    state = tmp_path / "player_state.json"
    monkeypatch.setenv("PLUM_SETTINGS_FILE", str(settings))
    monkeypatch.setenv("PLUM_PLAYER_STATE_FILE", str(state))
    monkeypatch.delenv("PLUM_DAC_DEVICE", raising=False)
    monkeypatch.delenv("PLUM_PLAYER_ENABLED", raising=False)
    monkeypatch.setattr(audio_devices.unit_identity, "settings_path", lambda: str(settings))
    import settings_api

    monkeypatch.setattr(settings_api, "SETTINGS_FILE", str(settings))
    return {"settings": settings, "state": state}


def _run(capsys):
    output_gate.main([])
    return capsys.readouterr().out.strip()


def test_a_normal_unit_prints_device_and_writes_nothing(rig, monkeypatch, capsys):
    rig["settings"].write_text(json.dumps({"audio": {"output": {"device": "sndrpihifiberry:0"}}}))
    monkeypatch.setattr(audio_devices, "has_output_hardware", lambda: True)

    assert _run(capsys) == MODE_DEVICE
    assert not rig["state"].exists()  # the player owns the echo on a unit that has one


def test_the_sentinel_prints_none_and_writes_the_echo(rig, monkeypatch, capsys):
    rig["settings"].write_text(json.dumps({"audio": {"output": {"device": "none"}}}))
    monkeypatch.setattr(audio_devices, "has_output_hardware", lambda: True)

    assert _run(capsys) == MODE_NONE
    assert json.loads(rig["state"].read_text())["output_device"] == "none"


def test_a_deliberate_choice_is_not_rewritten(rig, monkeypatch, capsys):
    """Only an AUTO-selection is persisted; the user's own choice is already in the file."""
    rig["settings"].write_text(json.dumps({"version": 7, "audio": {"output": {"device": "none"}}}))
    monkeypatch.setattr(audio_devices, "has_output_hardware", lambda: True)

    _run(capsys)
    assert json.loads(rig["settings"].read_text())["version"] == 7  # untouched


def test_no_hardware_persists_the_auto_selection(rig, monkeypatch, capsys):
    """So Settings -> Audio shows "No output" as chosen, not a phantom "Not present (bcm2835)"."""
    monkeypatch.setenv("PLUM_DAC_DEVICE", "bcm2835")
    monkeypatch.setattr(audio_devices, "has_output_hardware", lambda: False)

    assert _run(capsys) == MODE_NONE
    stored = json.loads(rig["settings"].read_text())["audio"]["output"]
    assert stored["device"] == "none"
    assert stored["device_type"] == "NONE"
    assert json.loads(rig["state"].read_text())["output_device"] == "none"


def test_the_env_override_prints_none_even_with_hardware(rig, monkeypatch, capsys):
    monkeypatch.setenv("PLUM_PLAYER_ENABLED", "0")
    monkeypatch.setattr(audio_devices, "has_output_hardware", lambda: True)

    assert _run(capsys) == MODE_NONE


def test_the_echo_write_preserves_the_persisted_volume(rig, monkeypatch, capsys):
    """Coming back to a real device must not silently reset the unit to 100%."""
    rig["state"].write_text(json.dumps({"volume": 42, "muted": True, "output_device": "sndrpihifiberry:0"}))
    rig["settings"].write_text(json.dumps({"audio": {"output": {"device": "none"}}}))
    monkeypatch.setattr(audio_devices, "has_output_hardware", lambda: True)

    _run(capsys)
    state = json.loads(rig["state"].read_text())
    assert state["output_device"] == "none"
    assert state["volume"] == 42
    assert state["muted"] is True


def test_dry_run_decides_without_writing_anything(rig, monkeypatch, capsys):
    """`docker exec ... output_gate.py --dry-run` must be safe on a live unit."""
    rig["settings"].write_text(json.dumps({"version": 7, "audio": {"output": {"device": "none"}}}))
    monkeypatch.setattr(audio_devices, "has_output_hardware", lambda: True)

    output_gate.main(["--dry-run"])
    assert capsys.readouterr().out.strip() == MODE_NONE
    assert not rig["state"].exists()
    assert json.loads(rig["settings"].read_text())["version"] == 7


@pytest.mark.parametrize("boom", ["configured_output_spec", "has_output_hardware"])
def test_the_gate_never_raises(rig, monkeypatch, capsys, boom):
    """Fail open. A gate that throws silences a unit with a perfectly good DAC."""

    def explode(*_a, **_k):
        raise RuntimeError("aplay went missing mid-boot")

    monkeypatch.setattr(audio_devices, boom, explode)
    assert output_gate.main([]) == 0
    assert capsys.readouterr().out.strip() == MODE_DEVICE


def test_an_unwritable_echo_still_reports_the_mode(rig, monkeypatch, capsys):
    """The decision is the product; the echo is a courtesy. Losing the second must not lose the first.

    Without this the unit would come up WITH a player on a host that has no card — the crash-loop
    this whole module exists to prevent.
    """
    monkeypatch.setattr(audio_devices, "has_output_hardware", lambda: False)
    monkeypatch.setattr(output_gate, "state_file_path", lambda: "/nonexistent/dir/player_state.json")

    assert _run(capsys) == MODE_NONE
    assert json.loads(rig["settings"].read_text())["audio"]["output"]["device"] == "none"


def test_a_late_card_does_not_destroy_the_stored_device(rig, monkeypatch, capsys):
    """End-to-end version of the same case: settings.json must come out untouched."""
    rig["settings"].write_text(
        json.dumps({"version": 9, "audio": {"output": {"device": "sndrpihifiberry:0", "device_type": "HAT"}}})
    )
    monkeypatch.setattr(audio_devices, "has_output_hardware", lambda: False)

    assert _run(capsys) == MODE_NONE
    stored = json.loads(rig["settings"].read_text())
    assert stored["audio"]["output"]["device"] == "sndrpihifiberry:0"  # survived
    assert stored["version"] == 9  # not even a version bump


def test_stored_output_spec_ignores_the_environment(rig, monkeypatch):
    """The distinction the guard rests on: a PLUM_DAC_DEVICE is not a choice."""
    monkeypatch.setenv("PLUM_DAC_DEVICE", "bcm2835")
    assert audio_devices.stored_output_spec() is None
    assert audio_devices.configured_output_spec() == "bcm2835"

    rig["settings"].write_text(json.dumps({"audio": {"output": {"device": "sndrpihifiberry:0"}}}))
    assert audio_devices.stored_output_spec() == "sndrpihifiberry:0"
