#!/usr/bin/env python3
"""Container healthcheck: is every process this unit is meant to run actually doing its job?

Run by Docker's HEALTHCHECK (see the Dockerfile). Exit 0 = healthy, 1 = not, with one line per
problem on stdout, which Docker keeps in `docker inspect`'s health log.

Why this replaced `curl :5002/api/settings`. That probe proved one thing: the config API answers.
The config API is a separate process from everything that makes sound, so the probe stayed green
through every failure that needs a power cycle today — a player that supervisord gave up on
(FATAL, while PID 1 stays up), a player that is RUNNING and no longer accepts a dial, and a player
whose event loop stopped. Five checks, each for one of those:

  1. Every supervisord program is RUNNING. "Every" means the RUNTIME set in /run/plum-supervisor.d,
     which entrypoint.sh composes: a unit with no audio output has no sendspin_player program at
     all (output_gate.py), and that is three healthy programs, not four with one missing.
  2. The config API answers (the old probe, kept).
  3. The player port accepts TCP — only on a unit that has a player.
  4. The player refreshed its health report recently (`health.ts` in player_state.json, written
     every 30 s from the player's own health loop) and does not report its output as lost.
  5. The mesh API answers a GET on :5001. sendspin_server hosts :5001 and the Sendspin port :8927
     in ONE event loop, so a loop that stopped leaves the program RUNNING and both ports open in
     the kernel. Only a request that gets no reply shows it. Skipped while supervisord reports the
     server as not RUNNING: check 1 already says so.

The timeouts of the probes add up to PROBE_TIMEOUT_TOTAL_S. That total must stay under the
`--timeout` of the Dockerfile HEALTHCHECK, or Docker ends the check before it can write a verdict.

Docker does NOT restart an unhealthy container, and there is deliberately no Docker socket in here
(CLAUDE.md), so "unhealthy" alone changes nothing. The verdict therefore also goes to
/data/health.json, where the mesh snapshot can serve it to the GUI and where a HOST agent can read
it from the bind mount and act:

    {"healthy": false, "problems": ["..."], "since": <epoch>, "checked_at": <epoch>,
     "consecutive_failures": 4, "programs": {"sendspin_player": "FATAL", ...}}

`since` is when the CURRENT verdict began, so "unhealthy for longer than N seconds" is one
subtraction. `checked_at` proves the check itself still runs: a stale one means the container, or
its healthcheck, is gone, and says nothing about health.

Standard library only, and no import of the rest of this tree: a healthcheck that needs the thing
it is checking to import cleanly reports nothing exactly when it matters.
"""

from __future__ import annotations

import contextlib
import glob
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

SUPERVISOR_CONF = os.environ.get("PLUM_SUPERVISOR_CONF", "/app/supervisord/supervisord.conf")
PROGRAM_DIR = os.environ.get("PLUM_SUPERVISOR_PROGRAM_DIR", "/run/plum-supervisor.d")
HEALTH_FILE = os.environ.get("PLUM_HEALTH_FILE", "/data/health.json")
PLAYER_STATE_FILE = os.environ.get("PLUM_PLAYER_STATE_FILE", "/data/player_state.json")
PLAYER_PROGRAM = "sendspin_player"
SERVER_PROGRAM = "sendspin_server"
# One timeout for each probe. They run in sequence, so the worst case is the sum, and the sum must
# stay under the Dockerfile HEALTHCHECK `--timeout` (15 s). tests/Unit/test_healthcheck.py reads
# the Dockerfile and holds this to it.
SUPERVISOR_TIMEOUT_S = 5.0
CONFIG_API_TIMEOUT_S = 3.0
PLAYER_PORT_TIMEOUT_S = 2.0
MESH_API_TIMEOUT_S = 3.0
PROBE_TIMEOUT_TOTAL_S = SUPERVISOR_TIMEOUT_S + CONFIG_API_TIMEOUT_S + PLAYER_PORT_TIMEOUT_S + MESH_API_TIMEOUT_S
# The player refreshes its report every 30 s (sendspin_player.HEALTH_REPORT_S). Four missed
# refreshes is a stopped event loop, not a slow SD card.
PLAYER_STALE_S = float(os.environ.get("PLUM_HEALTH_STALE_S", "120"))
# A healthy verdict is rewritten at most this often: it is an SD-card write every 30 s otherwise,
# for a file whose content did not change. An UNHEALTHY verdict is written on every run.
HEALTHY_REFRESH_S = 300.0

_PROGRAM_HEADER = re.compile(r"^\[program:([^\]]+)\]", re.MULTILINE)


def expected_programs(program_dir: str) -> list[str]:
    """Program names supervisord was started with — the files entrypoint.sh left in place."""
    names: list[str] = []
    for path in sorted(glob.glob(os.path.join(program_dir, "*.ini"))):
        try:
            with open(path, encoding="utf-8") as fh:
                names += _PROGRAM_HEADER.findall(fh.read())
        except OSError:
            continue
    return names


def parse_status(output: str) -> dict[str, str]:
    """`supervisorctl status` -> {program: STATE}. Lines that are not a status row are skipped."""
    states: dict[str, str] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].isupper():
            states[parts[0]] = parts[1]
    return states


def supervisor_status() -> tuple[dict[str, str], str | None]:
    """(states, error). supervisorctl exits 3 when any program is not RUNNING — that is data."""
    try:
        done = subprocess.run(
            ["supervisorctl", "-c", SUPERVISOR_CONF, "status"],
            capture_output=True,
            text=True,
            timeout=SUPERVISOR_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {}, f"supervisorctl did not run ({exc})"
    states = parse_status(done.stdout)
    if not states:
        detail = (done.stdout or done.stderr).strip().splitlines()
        return {}, f"supervisord did not answer ({detail[0] if detail else f'exit {done.returncode}'})"
    return states, None


def check_programs(expected: list[str], states: dict[str, str]) -> list[str]:
    problems = [
        f"program {name} is {states.get(name, 'MISSING')}, not RUNNING"
        for name in expected
        if states.get(name) != "RUNNING"
    ]
    # A program supervisord knows that the runtime set does not name: still judged, never skipped.
    problems += [
        f"program {name} is {state}, not RUNNING"
        for name, state in states.items()
        if name not in expected and state != "RUNNING"
    ]
    return problems


def check_port(host: str, port: int, *, timeout: float = PLAYER_PORT_TIMEOUT_S) -> str | None:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return None
    except OSError as exc:
        return f"player port {port} does not accept TCP ({exc})"


def check_config_api(port: int, *, timeout: float = CONFIG_API_TIMEOUT_S) -> str | None:
    url = f"http://127.0.0.1:{port}/api/settings"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            if response.status != 200:
                return f"config API answered {response.status} on :{port}"
            return None
    except Exception as exc:  # noqa: BLE001 - every failure here is the same verdict
        return f"config API does not answer on :{port} ({exc})"


def mesh_probe_host(port: int) -> str:
    """The `Host` header the mesh probe sends.

    The mesh API guard (cors_policy.guard) refuses a request with no `Origin` unless its `Host` is
    an IP literal, loopback or a name of this unit. An IP literal always passes, with any
    PLUM_ALLOWED_ORIGINS, so the probe names the address it connects to and sends no `Origin`.
    """
    return f"127.0.0.1:{port}"


def check_mesh_api(port: int, *, timeout: float = MESH_API_TIMEOUT_S) -> str | None:
    """Does the sendspin_server event loop answer? GET /api/mesh/view on loopback.

    Any HTTP reply proves that the loop runs, an error status included: the question here is a
    stopped loop, and a handler fault is not one. No reply inside `timeout` is the problem.
    """
    url = f"http://127.0.0.1:{port}/api/mesh/view"
    request = urllib.request.Request(url, headers={"Host": mesh_probe_host(port)})
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            return None
    except urllib.error.HTTPError:
        return None
    except Exception as exc:  # noqa: BLE001 - refused, reset or timed out: the loop gave no reply
        return f"mesh API does not answer on :{port} ({exc})"


def check_player_health(path: str, now: float, *, stale_s: float = PLAYER_STALE_S) -> list[str]:
    """The player's own report: is it recent, and does it say the output is gone?"""
    try:
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
    except FileNotFoundError:
        return [f"player state file {path} does not exist"]
    except (OSError, ValueError) as exc:
        return [f"player state file {path} is unreadable ({exc})"]
    report = state.get("health") if isinstance(state, dict) else None
    if not isinstance(report, dict) or not isinstance(report.get("ts"), (int, float)):
        return ["player wrote no health report"]
    problems: list[str] = []
    age = now - report["ts"]
    if age > stale_s:
        problems.append(f"player health report is stale ({age:.0f}s old, limit {stale_s:.0f}s)")
    output = report.get("output")
    if isinstance(output, dict) and output.get("state") == "lost":
        problems.append(f"player output is lost ({output.get('fault') or 'no detail'})")
    return problems


def evaluate(now: float) -> tuple[list[str], dict[str, str]]:
    """Run every check. Returns (problems, program states)."""
    problems: list[str] = []
    expected = expected_programs(PROGRAM_DIR)
    states, error = supervisor_status()
    if error:
        problems.append(error)
    else:
        problems += check_programs(expected, states)
    api_problem = check_config_api(int(os.environ.get("PLUM_CONFIG_API_PORT", "5002")))
    if api_problem:
        problems.append(api_problem)
    # A server that is not RUNNING is already reported above. An unknown state is still probed.
    if error or states.get(SERVER_PROGRAM) == "RUNNING":
        mesh_problem = check_mesh_api(int(os.environ.get("PLUM_MESH_API_PORT", "5001")))
        if mesh_problem:
            problems.append(mesh_problem)
    # The gate's decision, as entrypoint.sh applied it: no program file, no player to check.
    if PLAYER_PROGRAM in expected:
        port_problem = check_port("127.0.0.1", int(os.environ.get("PLUM_PLAYER_PORT", "8928")))
        if port_problem:
            problems.append(port_problem)
        # A player that is not RUNNING is already reported above; its stale report is the same
        # fault said twice, and a fresh process has not written one yet.
        if states.get(PLAYER_PROGRAM) == "RUNNING":
            problems += check_player_health(PLAYER_STATE_FILE, now)
    return problems, states


def build_report(previous: dict | None, problems: list[str], states: dict[str, str], now: float) -> dict:
    healthy = not problems
    same_verdict = isinstance(previous, dict) and previous.get("healthy") is healthy
    since = previous.get("since") if same_verdict and isinstance(previous.get("since"), (int, float)) else now
    failures = 0
    if not healthy:
        earlier = previous.get("consecutive_failures") if same_verdict else 0
        failures = (earlier if isinstance(earlier, int) else 0) + 1
    return {
        "healthy": healthy,
        "problems": problems,
        "since": round(since, 1),
        "checked_at": round(now, 1),
        "consecutive_failures": failures,
        "programs": states,
    }


def should_write(previous: dict | None, report: dict, now: float) -> bool:
    if not report["healthy"] or not isinstance(previous, dict):
        return True
    if previous.get("healthy") is not True:
        return True  # recovery: record it at once
    checked = previous.get("checked_at")
    return not isinstance(checked, (int, float)) or now - checked >= HEALTHY_REFRESH_S


def read_report(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def write_report(path: str, report: dict) -> None:
    """Atomic, so a reader on the host never sees half a file. Best-effort: never fails the check."""
    try:
        directory = os.path.dirname(path) or "."
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".health-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(report, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o644)  # mkstemp makes it 0600; the host agent may not run as root
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    except Exception as exc:  # noqa: BLE001
        print(f"healthcheck: could not write {path}: {exc}", file=sys.stderr)


def main() -> int:
    now = time.time()
    problems, states = evaluate(now)
    previous = read_report(HEALTH_FILE)
    report = build_report(previous, problems, states, now)
    if should_write(previous, report, now):
        write_report(HEALTH_FILE, report)
    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
