"""Unit tests for the HOST health watchdog, `plum-updater.sh health`.

Docker never restarts an unhealthy container, and the container holds no Docker socket. The
healthcheck writes its verdict to `/data/health.json`, and this host action reads it from the bind
mount and restarts the container. The agent runs as root and the container can write that file, so
the file is UNTRUSTED input: these tests feed it shell syntax, wrong types, a symlink and a FIFO.

The tests run the real script with stub `docker` and `flock` on the PATH.

Run: `pytest tests/Unit/test_plum_watchdog.py`.
"""

import json
import os
import shutil
import stat
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HOST_SETUP = REPO / "scripts" / "host-setup"
SCRIPT = HOST_SETUP / "plum-updater.sh"
TIMER = HOST_SETUP / "plum-updater-health.timer"
SERVICE = HOST_SETUP / "plum-updater-health.service"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

DOCKER_STUB = """#!/bin/sh
echo "docker $*" >> "$STUB_LOG"
if [ "$1" = inspect ]; then
    [ -n "$STUB_INSPECT" ] || exit 1
    echo "$STUB_INSPECT"
fi
exit 0
"""


FLOCK_STUB = """#!/bin/sh
echo "flock $*" >> "$STUB_LOG"
exit ${STUB_FLOCK:-0}
"""


def started(seconds_ago):
    """`.State.StartedAt` as Docker prints it: RFC 3339, nanoseconds, Z."""
    moment = datetime.fromtimestamp(time.time() - seconds_ago, tz=UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + ".123456789Z"


def inspect(running=True, up_for=3600, docker_health="unhealthy"):
    return f"{'true' if running else 'false'} {started(up_for)} {docker_health}"


@pytest.fixture()
def root(tmp_path):
    """A unit's /opt/plum-audio, a state directory outside it, and a bin/ of stubs."""
    root = tmp_path / "plum-audio"
    (root / "config").mkdir(parents=True)
    (root / "data").mkdir()
    (root / ".env").write_text("PLUM_IMAGE=ghcr.io/anothermike-exe/plum-audio\nPLUM_TAG=dev\n")
    stubs = tmp_path / "bin"
    stubs.mkdir()
    for name, body in {"docker": DOCKER_STUB, "flock": FLOCK_STUB}.items():
        (stubs / name).write_text(body)
        (stubs / name).chmod(0o755)
    return root


def run(root, inspect_line=None, action="health", **extra):
    env = {
        "PATH": f"{root.parent / 'bin'}:/usr/bin:/bin:/usr/sbin:/sbin",
        "PLUM_ROOT": str(root),
        "PLUM_WATCHDOG_STATE_DIR": str(root.parent / "var"),
        "STUB_LOG": str(root.parent / "stub.log"),
        "STUB_INSPECT": inspect() if inspect_line is None else inspect_line,
        **extra,
    }
    return subprocess.run(
        ["bash", str(SCRIPT), action], cwd=root.parent, env=env, capture_output=True, text=True, timeout=60
    )


def twice(root, inspect_line=None, **extra):
    """A restart needs the same finding on two runs in a row. Returns the second run."""
    run(root, inspect_line, **extra)
    return run(root, inspect_line, **extra)


def health(root, healthy=False, since_ago=600, checked_ago=5, problems=None, **more):
    now = time.time()
    report = {
        "healthy": healthy,
        "problems": ["program sendspin_player is FATAL, not RUNNING"] if problems is None else problems,
        "since": now - since_ago,
        "checked_at": now - checked_ago,
        "consecutive_failures": 4,
        **more,
    }
    (root / "data" / "health.json").write_text(json.dumps(report))


def compose_calls(root):
    """The compose commands that change the container. `docker compose version` is a probe."""
    log = root.parent / "stub.log"
    lines = log.read_text().splitlines() if log.exists() else []
    return [line for line in lines if line.startswith("docker compose ") and line != "docker compose version"]


def state(root):
    return json.loads((root.parent / "var" / "watchdog.state").read_text())


# -- the decision ------------------------------------------------------------------------------


def test_an_unhealthy_container_is_restarted_on_the_second_run(root):
    health(root)
    first = run(root)
    assert compose_calls(root) == [], "one sighting is not enough: a clock step can fake one"
    assert "program sendspin_player is FATAL" in first.stdout
    second = run(root)
    assert second.returncode == 0, second.stdout + second.stderr
    assert compose_calls(root) == ["docker compose restart"]
    assert "program sendspin_player is FATAL" in second.stdout
    assert len(state(root)["restarts"]) == 1


def test_a_healthy_container_is_left_alone(root):
    health(root, healthy=True, problems=[])
    twice(root)
    assert compose_calls(root) == []


# -- restart_advised: the healthcheck says whether a restart can fix the problem ----------------


def test_no_restart_when_the_healthcheck_does_not_advise_one(root):
    health(root, problems=["no output device is configured"], restart_advised=False)
    first = run(root)
    run(root)
    third = run(root)
    assert compose_calls(root) == []
    assert "unhealthy, restart not advised" in first.stdout
    assert "no output device is configured" in first.stdout
    assert "restart not advised" not in third.stdout, "one line for each change of state"
    assert state(root)["restarts"] == []


def test_the_not_advised_line_shows_again_after_a_change_of_state(root):
    health(root, restart_advised=False)
    run(root)
    health(root, healthy=True, problems=[])
    run(root)
    health(root, restart_advised=False)
    assert "unhealthy, restart not advised" in run(root).stdout


def test_a_restart_that_is_advised_is_made(root):
    health(root, restart_advised=True)
    twice(root)
    assert compose_calls(root) == ["docker compose restart"]


@pytest.mark.parametrize("value", [None, 0, "false", "no", [], {}])
def test_only_a_literal_false_stops_a_restart(root, value):
    """An absent key is an older image. A value of the wrong type is not a `false`."""
    health(root, restart_advised=value)
    twice(root)
    assert compose_calls(root) == ["docker compose restart"]


def test_a_change_to_not_advised_clears_the_first_sighting(root):
    health(root)
    run(root)
    health(root, restart_advised=False)
    run(root)
    health(root)
    run(root)
    assert compose_calls(root) == [], "the sighting before the change does not count"


def test_a_stale_verdict_is_a_restart_even_when_not_advised(root):
    """The healthcheck itself stopped, so its advice is old. Only a restart starts it again."""
    health(root, checked_ago=900, restart_advised=False)
    result = twice(root)
    assert compose_calls(root) == ["docker compose restart"]
    assert "stale" in result.stdout


def test_a_missing_file_means_unknown_and_nothing_runs(root):
    result = twice(root)
    assert result.returncode == 0
    assert compose_calls(root) == []


def test_unhealthy_for_less_than_the_limit_is_left_alone(root):
    health(root, since_ago=120)
    twice(root)
    assert compose_calls(root) == []


def test_a_container_that_started_a_moment_ago_is_left_alone(root):
    """After a restart the file still holds the old `since`. The new container gets its time."""
    health(root)
    twice(root, inspect(up_for=90))
    assert compose_calls(root) == []


def test_a_stale_verdict_from_a_running_container_is_a_restart(root):
    """The healthcheck itself stopped: `checked_at` is old and Docker does not report healthy."""
    health(root, healthy=True, problems=[], checked_ago=1000)
    result = twice(root)
    assert compose_calls(root) == ["docker compose restart"]
    assert "stale" in result.stdout


def test_a_stale_verdict_is_left_alone_while_docker_reports_healthy(root):
    """The check runs and passes but cannot write the file (a full disk). A restart fixes nothing."""
    health(root, healthy=True, problems=[], checked_ago=1000)
    first = run(root, inspect(docker_health="healthy"))
    second = run(root, inspect(docker_health="healthy"))
    assert compose_calls(root) == []
    assert "stale" in first.stdout
    assert second.stdout == "", "a finding that did not change is not logged again each minute"


def test_a_stale_verdict_from_a_stopped_container_is_left_alone(root):
    """An operator stopped it. A stale file says nothing about health."""
    health(root, checked_ago=1000)
    twice(root, inspect(running=False))
    assert compose_calls(root) == []


def test_a_healthy_verdict_inside_the_refresh_period_is_not_stale(root):
    """healthcheck.py rewrites a healthy verdict every 300 s only. 330 s old is normal."""
    health(root, healthy=True, problems=[], checked_ago=330)
    twice(root)
    assert compose_calls(root) == []


@pytest.mark.parametrize("line", [inspect(running=False), ""], ids=["stopped", "no-container"])
def test_a_container_that_is_not_running_is_brought_up(root, line):
    health(root)
    twice(root, line)
    assert compose_calls(root) == ["docker compose up -d"]


def test_a_recovery_clears_the_first_sighting(root):
    health(root)
    run(root)
    health(root, healthy=True, problems=[])
    run(root)
    health(root)
    run(root)
    assert compose_calls(root) == [], "the two sightings were not in a row"


def test_an_old_first_sighting_does_not_count(root):
    health(root)
    run(root)
    saved = state(root)
    saved["strike"]["mono"] -= 3600
    (root.parent / "var" / "watchdog.state").write_text(json.dumps(saved))
    run(root)
    assert compose_calls(root) == []


# -- the rate limit ----------------------------------------------------------------------------


def test_at_most_three_restarts_in_an_hour(root):
    health(root)
    var = root.parent / "var"
    var.mkdir()
    now = time.time()
    (var / "watchdog.state").write_text(json.dumps({"restarts": [now - 3000, now - 2000, now - 1000]}))
    result = twice(root)
    assert compose_calls(root) == []
    assert "rate limit" in result.stdout
    assert "program sendspin_player is FATAL" in result.stdout


def test_a_restart_older_than_an_hour_does_not_count(root):
    health(root)
    var = root.parent / "var"
    var.mkdir()
    now = time.time()
    (var / "watchdog.state").write_text(json.dumps({"restarts": [now - 4000, now - 2000, now - 1000]}))
    twice(root)
    assert compose_calls(root) == ["docker compose restart"]
    assert len(state(root)["restarts"]) == 3


@pytest.mark.parametrize("content", ["{ not json", "[1]", '{"restarts": "x"}', '{"restarts": [true, "a", null]}'])
def test_a_damaged_state_file_does_not_stop_the_watchdog(root, content):
    health(root)
    var = root.parent / "var"
    var.mkdir()
    (var / "watchdog.state").write_text(content)
    assert twice(root).returncode == 0
    assert compose_calls(root) == ["docker compose restart"]


def test_the_state_file_is_outside_the_container_tree_and_private(root):
    health(root)
    twice(root)
    path = root.parent / "var" / "watchdog.state"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    for mounted in ("config", "data"):
        assert [p.name for p in (root / mounted).iterdir() if "watchdog" in p.name] == []
    text = SCRIPT.read_text()
    assert 'WATCHDOG_STATE_DIR="${PLUM_WATCHDOG_STATE_DIR:-/var/lib/plum-audio}"' in text


def test_no_restart_without_a_state_file_to_count_it_in(root):
    """A restart that cannot be counted has no rate limit."""
    health(root)
    blocker = root.parent / "blocker"
    blocker.write_text("a file where the state directory must go")
    twice(root, PLUM_WATCHDOG_STATE_DIR=str(blocker / "var"))
    assert compose_calls(root) == []


# -- the switches ------------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["0", "false", "off", "no"])
def test_the_env_switch_disables_the_watchdog(root, value):
    health(root)
    result = twice(root, PLUM_WATCHDOG=value)
    assert compose_calls(root) == []
    assert result.returncode == 0


def test_the_file_switch_disables_the_watchdog(root):
    health(root)
    (root / "watchdog.disabled").write_text("")
    twice(root)
    assert compose_calls(root) == []


def test_no_restart_while_an_update_holds_the_lock(root):
    health(root)
    result = twice(root, STUB_FLOCK="1")
    assert compose_calls(root) == []
    assert "lock" in result.stdout


def flock_calls(root):
    log = root.parent / "stub.log"
    return [line for line in log.read_text().splitlines() if line.startswith("flock ")]


def test_the_watchdog_does_not_wait_for_the_lock(root):
    health(root)
    run(root)
    assert flock_calls(root) == ["flock -n 9"]


@pytest.mark.parametrize("action", ["watch", "update"])
def test_an_update_waits_for_the_lock_the_watchdog_holds(root, action):
    """The watchdog takes the lock once a minute. A `watch` that does not wait leaves the request."""
    run(root, action=action)
    assert flock_calls(root) == ["flock -w 60 9"]


def test_a_watch_that_cannot_get_the_lock_leaves_the_request(root):
    (root / "config" / "update.request").write_text('{"channel": "dev"}')
    result = run(root, action="watch", STUB_FLOCK="1")
    assert result.returncode == 0
    assert (root / "config" / "update.request").exists()
    assert compose_calls(root) == []
    assert "lock" in result.stdout


def test_no_restart_while_an_update_request_waits(root):
    health(root)
    (root / "config" / "update.request").write_text('{"channel": "dev"}')
    result = twice(root)
    assert compose_calls(root) == []
    assert (root / "config" / "update.request").exists(), "the watchdog must not consume it"
    assert "update" in result.stdout


# -- the file is untrusted ---------------------------------------------------------------------


INJECTIONS = [
    "$(touch PWNED)",
    "`touch PWNED`",
    "'; touch PWNED; '",
    '"; touch PWNED; "',
    "x\ntouch PWNED",
    "\x1b[2J\x07 bell",
    "%s%s%n",
    "-d --rm",
]


@pytest.mark.parametrize("problem", INJECTIONS)
def test_a_problem_string_is_never_run_or_put_in_a_command(root, problem):
    health(root, problems=[problem, {"nested": [problem]}, 7])
    result = twice(root)
    assert not (root.parent / "PWNED").exists() and not (root / "PWNED").exists()
    assert compose_calls(root) == ["docker compose restart"]
    assert "\x1b" not in result.stdout and "\x07" not in result.stdout
    # One journal line for each message: a newline in a problem does not start a second line.
    assert all("plum-updater:" in line for line in result.stdout.splitlines() if line.strip())


def test_the_problems_in_the_journal_are_capped(root):
    health(root, problems=["x" * 1000] * 40)  # 40 kB: under the size cap, so it is read
    result = twice(root)
    assert compose_calls(root) == ["docker compose restart"]
    assert max(len(line) for line in result.stdout.splitlines()) < 2000


@pytest.mark.parametrize(
    "content",
    [
        "",
        "{ not json",
        "[]",
        '"unhealthy"',
        '{"healthy": "false", "since": 1, "checked_at": 1}',
        '{"healthy": 0, "since": 1, "checked_at": 1}',
        '{"healthy": false, "since": "1", "checked_at": "now"}',
        '{"healthy": false, "since": true, "checked_at": true}',
        '{"healthy": false, "since": NaN, "checked_at": Infinity}',
        '{"healthy": false, "since": 1e999, "checked_at": -1e999}',
        '{"healthy": false}',
        '{"healthy": false, "since": [1], "checked_at": {"a": 1}}',
    ],
)
def test_a_file_that_is_not_a_verdict_means_unknown(root, content):
    (root / "data" / "health.json").write_text(content)
    result = twice(root)
    assert result.returncode == 0, result.stdout + result.stderr
    assert compose_calls(root) == []


def test_a_very_large_file_means_unknown(root):
    health(root, padding="x" * 300_000)
    twice(root)
    assert compose_calls(root) == []


def test_a_symlink_at_the_health_path_is_not_followed(root):
    """The container can link the path to any file on the host. Root must not read through it."""
    health(root)
    real = root.parent / "elsewhere.json"
    (root / "data" / "health.json").rename(real)
    os.symlink(real, root / "data" / "health.json")
    twice(root)
    assert compose_calls(root) == []


def test_a_fifo_at_the_health_path_does_not_hang_the_agent(root):
    os.mkfifo(root / "data" / "health.json")
    assert twice(root).returncode == 0
    assert compose_calls(root) == []


def test_the_script_has_no_eval_of_the_health_file():
    """`load_state` evals shlex-quoted output for update.state. The watchdog must not copy that."""
    text = SCRIPT.read_text()
    body = text[text.index("do_health() {") : text.index("do_watch() {")]
    assert "eval" not in body
    assert "HEALTH_FILE" in body


# -- the systemd units and the two installers --------------------------------------------------


def test_the_timer_runs_about_once_a_minute():
    timer = TIMER.read_text()
    assert "OnUnitActiveSec=60" in timer
    assert "OnBootSec=" in timer
    assert "WantedBy=timers.target" in timer
    service = SERVICE.read_text()
    assert "ExecStart=/usr/local/bin/plum-updater.sh health" in service
    assert "Type=oneshot" in service
    assert "EnvironmentFile=-/etc/default/plum-updater" in service


AGENT_SERVICES = ["plum-updater-health.service", "plum-updater.service", "plum-updater-check.service"]


def directives(name):
    lines = (HOST_SETUP / name).read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line and not line.startswith("#"))


@pytest.mark.parametrize("name", AGENT_SERVICES)
def test_the_agent_services_run_in_a_sandbox(name):
    found = directives(name)
    for key in ("NoNewPrivileges", "PrivateTmp", "ProtectKernelTunables", "ProtectKernelModules"):
        assert found.get(key) == "yes", f"{name}: {key}"


@pytest.mark.parametrize("name", AGENT_SERVICES)
def test_the_sandbox_leaves_docker_compose_working(name):
    """The docker CLI needs the registry login in /root, the socket in /run and the network."""
    found = directives(name)
    assert found.get("ProtectHome") == "read-only", "`yes` hides /root/.docker/config.json"
    for key in ("PrivateNetwork", "ProtectSystem", "ProtectControlGroups", "User", "DynamicUser"):
        assert key not in found, f"{name}: {key} is not tested with docker compose"


@pytest.mark.parametrize("installer", ["scripts/host-setup/provision.sh", "scripts/plum-init.sh"])
def test_both_installers_install_and_enable_the_timer(installer):
    text = (REPO / installer).read_text()
    assert "plum-updater-health.service" in text
    assert "systemctl enable --now plum-updater-health.timer" in text


def test_provision_copies_the_units_to_the_unit():
    text = (REPO / "scripts/host-setup/provision.sh").read_text()
    assert '"${HERE}/plum-updater-health.timer" "${HERE}/plum-updater-health.service"' in text
