"""Unit tests for the container's half of the update mechanism.

`updater.py` writes a request and reads a state file across the /config bind mount. It never runs
Docker — the host agent does — so everything here is file behaviour, and the properties that matter
are the ones whose failure would be invisible in the GUI:

`test_status_never_raises_*` — this is polled per unit during an update, straight through the window
where the container is being recreated. A raise here is a 500 on a page whose whole job is to report
what is happening, at the moment it is hardest to debug by other means.

`test_request_is_refused_without_an_agent` — writing the request anyway would leave the GUI polling
a file nothing will ever consume, and the unit would read as hung rather than as unprovisioned.
Every unit built before the agent existed is in exactly this state.

`test_a_stale_request_is_reported` — the only signal that the agent is installed but not running.
Without it a dead agent is indistinguishable from a slow pull.

Run: `pytest tests/Unit/test_updater.py`.
"""

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "scripts"))

import updater  # noqa: E402


@pytest.fixture
def config(tmp_path, monkeypatch):
    """Point the module at a writable directory instead of /config."""
    monkeypatch.setenv("PLUM_CONFIG_DIR", str(tmp_path))
    return tmp_path


def _write_state(config: Path, **fields) -> None:
    payload = {"agentVersion": "1.0.0", "writtenAt": "2026-09-13T00:00:00+00:00"}
    payload.update(fields)
    (config / updater.STATE_NAME).write_text(json.dumps(payload), encoding="utf-8")


# --- reading ---------------------------------------------------------------------------------


def test_status_reports_no_agent_on_a_bare_unit(config):
    status = updater.status()
    assert status["agent"]["installed"] is False
    assert status["pending"] is False


def test_status_never_raises_on_a_missing_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("PLUM_CONFIG_DIR", str(tmp_path / "nope"))
    assert updater.status()["agent"]["installed"] is False


def test_status_never_raises_on_a_half_written_state(config):
    # The agent writes this file while the container is being recreated, so a poll CAN land
    # mid-write despite the atomic rename. Unparseable must read as "no answer yet".
    (config / updater.STATE_NAME).write_text('{"agentVersion": "1.0.0", "pha', encoding="utf-8")
    assert updater.status()["agent"]["installed"] is False


def test_status_never_raises_on_a_json_array(config):
    # Defensive against a future agent writing a different shape: a list is valid JSON and would
    # otherwise reach `.get()` and raise.
    (config / updater.STATE_NAME).write_text("[1, 2, 3]", encoding="utf-8")
    assert updater.status()["agent"]["installed"] is False


def test_status_passes_the_agent_fields_through(config):
    _write_state(
        config,
        channel="dev",
        image="ghcr.io/anothermike-exe/plum-audio:dev",
        digest="sha256:aaa",
        available="sha256:bbb",
        phase="pulling",
    )
    status = updater.status()
    assert status["agent"]["installed"] is True
    assert status["digest"] == "sha256:aaa"
    assert status["available"] == "sha256:bbb"
    assert status["phase"] == "pulling"


def test_the_running_block_comes_from_the_build_environment(config, monkeypatch):
    monkeypatch.setenv("PLUM_APP_VERSION", "1.4.0")
    monkeypatch.setenv("PLUM_BUILD_TYPE", "release")
    running = updater.status()["running"]
    assert running["version"] == "1.4.0"
    assert running["buildType"] == "release"


# --- writing ---------------------------------------------------------------------------------


def test_request_is_refused_without_an_agent(config):
    with pytest.raises(updater.UpdateError) as exc:
        updater.request_update("dev")
    # The message has to name the fix — this is the state of every pre-agent unit, and the operator
    # reading it has no other clue that provisioning is what is missing.
    assert "provision.sh" in str(exc.value)
    assert not (config / updater.REQUEST_NAME).exists()


def test_request_is_written_when_an_agent_exists(config):
    _write_state(config)
    accepted = updater.request_update("dev")
    assert accepted["channel"] == "dev"
    on_disk = json.loads((config / updater.REQUEST_NAME).read_text(encoding="utf-8"))
    assert on_disk["channel"] == "dev"
    assert on_disk["checkOnly"] is False


def test_an_unknown_channel_is_refused(config):
    _write_state(config)
    with pytest.raises(updater.UpdateError):
        updater.request_update("nightly")
    assert not (config / updater.REQUEST_NAME).exists()


def test_a_check_only_request_is_marked_as_such(config):
    _write_state(config)
    updater.request_update("latest", check_only=True)
    on_disk = json.loads((config / updater.REQUEST_NAME).read_text(encoding="utf-8"))
    assert on_disk["checkOnly"] is True


def test_the_default_channel_is_dev(config):
    _write_state(config)
    assert updater.request_update()["channel"] == "dev"


# --- the pending/stale signal ------------------------------------------------------------------


def test_a_pending_request_is_reported(config):
    _write_state(config)
    updater.request_update("dev")
    status = updater.status()
    assert status["pending"] is True
    assert status["stale"] is False


def test_a_stale_request_is_reported(config):
    _write_state(config)
    stale_at = 0.0  # the epoch is comfortably older than REQUEST_STALE_S
    (config / updater.REQUEST_NAME).write_text(
        json.dumps({"channel": "dev", "checkOnly": False, "requestedAt": stale_at}),
        encoding="utf-8",
    )
    status = updater.status()
    assert status["pending"] is True
    assert status["stale"] is True


def test_a_request_without_a_timestamp_is_pending_but_not_stale(config):
    # A hand-written request (an operator testing the agent with `echo`) has no timestamp. It must
    # not be declared stale, because we cannot know that it is.
    _write_state(config)
    (config / updater.REQUEST_NAME).write_text('{"channel": "dev"}', encoding="utf-8")
    status = updater.status()
    assert status["pending"] is True
    assert status["stale"] is False


def test_the_request_write_is_atomic_leaving_no_temp_files(config):
    _write_state(config)
    updater.request_update("dev")
    leftovers = [p.name for p in config.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
