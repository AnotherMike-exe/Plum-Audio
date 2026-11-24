"""Unit tests for the HOST update agent, `scripts/host-setup/plum-updater.sh`.

The agent runs as root and reads `/config/update.request`, which the container can write. The
container's own check (`updater.CHANNELS`) is no defence: anything that can write the file skips
it. The agent printed the channel into `$ROOT/.env`, so a channel with a newline added a
`PLUM_IMAGE=` line, and the next `docker compose up -d` ran an image the attacker chose, as root.

These tests run the real script with stub `docker`, `curl` and `flock` on the PATH.

Run: `pytest tests/Unit/test_plum_updater_agent.py`.
"""

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "host-setup" / "plum-updater.sh"

ENV = "PLUM_IMAGE=ghcr.io/anothermike-exe/plum-audio\nPLUM_TAG=dev\nCOMPOSE_PROFILES=player\n"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


@pytest.fixture()
def root(tmp_path):
    """A unit's /opt/plum-audio, and a bin/ of stubs so no real docker or network is touched."""
    root = tmp_path / "plum-audio"
    (root / "config").mkdir(parents=True)
    (root / ".env").write_text(ENV)
    stubs = tmp_path / "bin"
    stubs.mkdir()
    for name, body in {
        "docker": 'echo "docker $*" >> "$STUB_LOG"; exit 0',
        "docker-compose": 'echo "docker-compose $*" >> "$STUB_LOG"; exit 0',
        "curl": "exit 1",  # the registry is unreachable: availability unknown
        "flock": "exit 0",  # macOS has no flock, and the lock is not what is tested here
    }.items():
        path = stubs / name
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)
    return root


def run(root, *args):
    env = {
        "PATH": f"{root.parent / 'bin'}:/usr/bin:/bin:/usr/sbin:/sbin",
        "PLUM_ROOT": str(root),
        "STUB_LOG": str(root.parent / "stub.log"),
    }
    return subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60)


def request(root, channel, check_only=False):
    (root / "config" / "update.request").write_text(json.dumps({"channel": channel, "checkOnly": check_only}))


def stub_log(root):
    log = root.parent / "stub.log"
    return log.read_text() if log.exists() else ""


# -- the channel -------------------------------------------------------------------------------


def test_a_real_channel_still_moves_the_tag(root):
    request(root, "latest")
    result = run(root, "watch")
    assert result.returncode == 0, result.stdout + result.stderr
    env = (root / ".env").read_text()
    assert "PLUM_TAG=latest\n" in env
    assert "PLUM_IMAGE=ghcr.io/anothermike-exe/plum-audio\n" in env
    assert "COMPOSE_PROFILES=player\n" in env
    assert "pull" in stub_log(root)
    assert not (root / "config" / "update.request").exists()


@pytest.mark.parametrize(
    "channel",
    [
        "dev\nPLUM_IMAGE=evil.example/owned",
        "dev\rPLUM_IMAGE=evil.example/owned",
        "latest PLUM_IMAGE=x",
        "dev;reboot",
        "$(reboot)",
        "../../etc",
        "-rf",
        ".hidden",
        "a" * 65,
        "evil.example/owned:latest",
    ],
)
def test_a_channel_that_is_not_a_tag_is_ignored(root, channel):
    request(root, channel)
    result = run(root, "watch")
    assert (root / ".env").read_text() == ENV  # byte for byte: no new line, no moved tag
    assert stub_log(root) == ""  # no pull, no up
    assert "ignor" in result.stdout
    assert not (root / "config" / "update.request").exists()  # consumed, so it is not replayed


@pytest.mark.parametrize("check_only", [True, False])
def test_a_bad_channel_is_ignored_for_a_check_too(root, check_only):
    request(root, "dev\nPLUM_IMAGE=evil.example/owned", check_only)
    run(root, "watch")
    assert (root / ".env").read_text() == ENV
    state = root / "config" / "update.state"
    assert not state.exists() or "evil" not in state.read_text()


@pytest.mark.parametrize("channel", [["dev"], {"a": 1}, 7, True])
def test_a_channel_that_is_not_a_string_is_ignored(root, channel):
    request(root, channel)
    run(root, "watch")
    assert (root / ".env").read_text() == ENV
    assert stub_log(root) == ""


def test_a_request_with_no_channel_keeps_the_current_tag(root):
    (root / "config" / "update.request").write_text("{}")
    assert run(root, "watch").returncode == 0
    assert "PLUM_TAG=dev\n" in (root / ".env").read_text()
    assert "pull" in stub_log(root)


def test_a_bad_channel_on_the_command_line_is_refused(root):
    result = run(root, "update", "dev\nPLUM_IMAGE=evil.example/owned")
    assert result.returncode != 0
    assert (root / ".env").read_text() == ENV


def test_a_bad_tag_already_in_the_env_file_is_not_carried_forward(root):
    """`current_tag` feeds the same sinks when a request names no channel."""
    (root / ".env").write_text("PLUM_IMAGE=x/y\nPLUM_TAG=dev;reboot\n")
    (root / "config" / "update.request").write_text("{}")
    run(root, "watch")
    assert stub_log(root) == ""


# -- the state file ----------------------------------------------------------------------------


def test_the_state_file_is_written_readable_and_leaves_no_temp(root):
    assert run(root, "init").returncode == 0
    state = root / "config" / "update.state"
    assert json.loads(state.read_text())["agentVersion"]
    assert stat.S_IMODE(state.stat().st_mode) == 0o644  # the container reads it as a non-root user
    assert sorted(p.name for p in (root / "config").iterdir()) == ["update.state"]
    assert [p.name for p in root.iterdir() if "tmp" in p.name or p.name.startswith(".update.state")] == []


def test_a_symlink_at_the_state_path_is_replaced_not_followed(root):
    """/config is container-writable and the agent is root: a followed link is a root file write."""
    victim = root.parent / "victim"
    victim.write_text("untouched")
    victim.chmod(0o600)
    os.symlink(victim, root / "config" / "update.state")

    assert run(root, "init").returncode == 0
    state = root / "config" / "update.state"
    assert victim.read_text() == "untouched"
    assert stat.S_IMODE(victim.stat().st_mode) == 0o600  # chmod did not follow the link
    assert not state.is_symlink()
    assert json.loads(state.read_text())["agentVersion"]


def test_the_temp_file_is_not_made_in_the_container_writable_directory(root):
    """The old temp was `$CONFIG/update.state.<pid>.tmp`: a name the container could link first."""
    text = SCRIPT.read_text()
    assert "${STATE}.$$" not in text
    assert "${ENV_FILE}.$$" not in text
    assert "mktemp" in text
