"""Unit tests for the container healthcheck (backend/scripts/healthcheck.py).

The old probe was one curl against the config API, which is a separate process from everything that
makes sound: it stayed green while the player was FATAL, wedged or deaf. These tests pin what the
new one must call unhealthy, and the one thing it must NOT: a unit with no audio output has no
player program at all, and that is a healthy unit.

Run: `pytest tests/Unit/test_healthcheck.py`.
"""

import json
import socket
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "scripts"))

import healthcheck  # noqa: E402

STATUS_ALL_RUNNING = """\
config_api                       RUNNING   pid 19, uptime 0:10:02
nginx                            RUNNING   pid 20, uptime 0:10:02
sendspin_player                  RUNNING   pid 21, uptime 0:10:02
sendspin_server                  RUNNING   pid 18, uptime 0:10:02
"""

STATUS_PLAYER_FATAL = STATUS_ALL_RUNNING.replace(
    "sendspin_player                  RUNNING   pid 21, uptime 0:10:02",
    "sendspin_player                  FATAL     Exited too quickly (process log may have details)",
)

FOUR = ["config_api", "nginx", "sendspin_player", "sendspin_server"]
THREE = ["config_api", "nginx", "sendspin_server"]


def _program_dir(tmp_path, names):
    for name in names:
        (tmp_path / f"{name}.ini").write_text(f"; comment\n[program:{name}]\ncommand=true\n")
    return str(tmp_path)


def test_the_expected_set_is_the_runtime_program_files(tmp_path):
    assert healthcheck.expected_programs(_program_dir(tmp_path, FOUR)) == FOUR


def test_the_shipped_program_files_declare_the_four_programs():
    """The real templates, so a renamed program cannot silently fall out of the check."""
    shipped = healthcheck.expected_programs(str(REPO / "backend" / "supervisord" / "conf.d"))
    assert sorted(shipped) == FOUR


def test_status_output_is_parsed():
    states = healthcheck.parse_status(STATUS_PLAYER_FATAL)
    assert states["sendspin_player"] == "FATAL" and states["nginx"] == "RUNNING"
    assert healthcheck.parse_status("unix:///run/supervisor.sock no such file\n") == {}


def test_all_running_is_healthy():
    assert healthcheck.check_programs(FOUR, healthcheck.parse_status(STATUS_ALL_RUNNING)) == []


def test_a_fatal_player_is_unhealthy():
    problems = healthcheck.check_programs(FOUR, healthcheck.parse_status(STATUS_PLAYER_FATAL))
    assert problems == ["program sendspin_player is FATAL, not RUNNING"]


def test_a_program_supervisord_does_not_know_is_unhealthy():
    states = healthcheck.parse_status(STATUS_ALL_RUNNING)
    del states["sendspin_server"]
    assert healthcheck.check_programs(FOUR, states) == ["program sendspin_server is MISSING, not RUNNING"]


def test_a_unit_with_no_output_is_healthy_with_three_programs():
    """output_gate.py removed the player's program file. Its absence is correct, not a fault."""
    states = healthcheck.parse_status(STATUS_ALL_RUNNING)
    del states["sendspin_player"]
    assert healthcheck.check_programs(THREE, states) == []


def _patch(
    monkeypatch, tmp_path, *, programs, status, port_problem=None, api_problem=None, mesh_problem=None, calls=None
):
    monkeypatch.setattr(healthcheck, "PROGRAM_DIR", _program_dir(tmp_path, programs))
    monkeypatch.setattr(healthcheck, "PLAYER_STATE_FILE", str(tmp_path / "player_state.json"))
    monkeypatch.setattr(healthcheck, "supervisor_status", lambda: (healthcheck.parse_status(status), None))
    monkeypatch.setattr(healthcheck, "check_config_api", lambda port: api_problem)
    monkeypatch.setattr(healthcheck, "check_mesh_api", lambda port: mesh_problem)

    def fake_port(host, port, **kwargs):
        if calls is not None:
            calls.append(port)
        return port_problem

    monkeypatch.setattr(healthcheck, "check_port", fake_port)


def _player_state(tmp_path, *, ts, output_state="ok"):
    report = {"ts": ts, "output": {"state": output_state, "fault": "device removed" if output_state != "ok" else None}}
    (tmp_path / "player_state.json").write_text(json.dumps({"volume": 40, "health": report}))


def test_a_healthy_unit_passes_every_check(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path, programs=FOUR, status=STATUS_ALL_RUNNING)
    _player_state(tmp_path, ts=1000.0)
    assert healthcheck.evaluate(1010.0)[0] == []


def test_the_player_checks_are_skipped_on_a_unit_with_no_output(monkeypatch, tmp_path):
    calls: list[int] = []
    status = "\n".join(line for line in STATUS_ALL_RUNNING.splitlines() if "player" not in line)
    _patch(monkeypatch, tmp_path, programs=THREE, status=status, port_problem="refused", calls=calls)
    # No player_state.json health block at all — output_gate.py writes only the echo.
    assert healthcheck.evaluate(1010.0)[0] == []
    assert calls == [], "port 8928 must not be probed when there is no player"


def test_a_closed_player_port_is_unhealthy(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path, programs=FOUR, status=STATUS_ALL_RUNNING, port_problem="player port 8928 refused")
    _player_state(tmp_path, ts=1000.0)
    assert healthcheck.evaluate(1010.0)[0] == ["player port 8928 refused"]


def test_a_stale_health_report_is_unhealthy(monkeypatch, tmp_path):
    """RUNNING, port open, and an event loop that stopped ten minutes ago."""
    _patch(monkeypatch, tmp_path, programs=FOUR, status=STATUS_ALL_RUNNING)
    _player_state(tmp_path, ts=1000.0)
    problems = healthcheck.evaluate(1000.0 + 600)[0]
    assert len(problems) == 1 and "stale (600s old" in problems[0]


def test_a_lost_output_is_unhealthy(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path, programs=FOUR, status=STATUS_ALL_RUNNING)
    _player_state(tmp_path, ts=1000.0, output_state="lost")
    assert healthcheck.evaluate(1010.0)[0] == ["player output is lost (device removed)"]


def test_a_missing_health_report_is_unhealthy_for_a_running_player(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path, programs=FOUR, status=STATUS_ALL_RUNNING)
    (tmp_path / "player_state.json").write_text(json.dumps({"volume": 40}))
    assert healthcheck.evaluate(1010.0)[0] == ["player wrote no health report"]


def test_a_player_that_is_not_running_is_reported_once(monkeypatch, tmp_path):
    """Its stale report is the same fault. Two lines for one cause hides the cause."""
    _patch(monkeypatch, tmp_path, programs=FOUR, status=STATUS_PLAYER_FATAL)
    _player_state(tmp_path, ts=1.0)
    assert healthcheck.evaluate(99999.0)[0] == ["program sendspin_player is FATAL, not RUNNING"]


def test_a_dead_config_api_is_still_unhealthy(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path, programs=FOUR, status=STATUS_ALL_RUNNING, api_problem="config API does not answer")
    _player_state(tmp_path, ts=1000.0)
    assert healthcheck.evaluate(1010.0)[0] == ["config API does not answer"]


def test_a_real_closed_port_is_detected():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert "does not accept TCP" in healthcheck.check_port("127.0.0.1", port, timeout=0.5)


def test_a_real_open_port_passes():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        assert healthcheck.check_port("127.0.0.1", server.getsockname()[1], timeout=0.5) is None


# -- the report the host agent reads ---------------------------------------------------------------


def test_the_report_counts_consecutive_failures_and_keeps_the_start_time():
    first = healthcheck.build_report(None, ["program sendspin_player is FATAL, not RUNNING"], {}, 100.0)
    second = healthcheck.build_report(first, ["program sendspin_player is FATAL, not RUNNING"], {}, 130.0)
    assert (first["healthy"], first["since"], first["consecutive_failures"]) == (False, 100.0, 1)
    assert (second["since"], second["checked_at"], second["consecutive_failures"]) == (100.0, 130.0, 2)


def test_a_recovery_restarts_the_clock():
    bad = healthcheck.build_report(None, ["x"], {}, 100.0)
    good = healthcheck.build_report(bad, [], {"nginx": "RUNNING"}, 160.0)
    assert good == {
        "healthy": True,
        "problems": [],
        "since": 160.0,
        "checked_at": 160.0,
        "consecutive_failures": 0,
        "programs": {"nginx": "RUNNING"},
        "restart_advised": False,
    }


def test_an_unhealthy_verdict_is_written_every_run_and_a_healthy_one_is_throttled():
    good = healthcheck.build_report(None, [], {}, 100.0)
    assert healthcheck.should_write(None, good, 100.0)
    again = healthcheck.build_report(good, [], {}, 130.0)
    assert not healthcheck.should_write(good, again, 130.0), "no SD-card write for an unchanged healthy verdict"
    later = healthcheck.build_report(good, [], {}, 100.0 + healthcheck.HEALTHY_REFRESH_S)
    assert healthcheck.should_write(good, later, 100.0 + healthcheck.HEALTHY_REFRESH_S)
    bad = healthcheck.build_report(good, ["x"], {}, 140.0)
    assert healthcheck.should_write(good, bad, 140.0)
    still_bad = healthcheck.build_report(bad, ["x"], {}, 170.0)
    assert healthcheck.should_write(bad, still_bad, 170.0)
    recovered = healthcheck.build_report(still_bad, [], {}, 200.0)
    assert healthcheck.should_write(still_bad, recovered, 200.0)


def test_main_writes_the_report_and_exits_non_zero(monkeypatch, tmp_path, capsys):
    _patch(monkeypatch, tmp_path, programs=FOUR, status=STATUS_PLAYER_FATAL)
    health_file = tmp_path / "health.json"
    monkeypatch.setattr(healthcheck, "HEALTH_FILE", str(health_file))
    assert healthcheck.main() == 1
    report = json.loads(health_file.read_text())
    assert report["healthy"] is False
    assert report["problems"] == ["program sendspin_player is FATAL, not RUNNING"]
    assert report["programs"]["sendspin_player"] == "FATAL"
    assert "FATAL" in capsys.readouterr().out
    assert sorted(p.name for p in tmp_path.glob("*health*")) == ["health.json"], "no temp file left behind"


def test_main_exits_zero_when_healthy(monkeypatch, tmp_path):
    import time

    _patch(monkeypatch, tmp_path, programs=FOUR, status=STATUS_ALL_RUNNING)
    _player_state(tmp_path, ts=time.time())
    monkeypatch.setattr(healthcheck, "HEALTH_FILE", str(tmp_path / "health.json"))
    assert healthcheck.main() == 0
    assert json.loads((tmp_path / "health.json").read_text())["healthy"] is True


# -- the server event loop ---------------------------------------------------------------------------
# sendspin_server hosts :5001 and :8927 in ONE loop. A loop that stopped leaves the process RUNNING.


def _http_server(handler):
    import http.server
    import threading

    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_the_mesh_probe_passes_when_the_loop_answers():
    import http.server

    seen = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen["path"], seen["host"], seen["origin"] = self.path, self.headers["Host"], self.headers["Origin"]
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = _http_server(Handler)
    try:
        port = server.server_address[1]
        assert healthcheck.check_mesh_api(port, timeout=2.0) is None
    finally:
        server.shutdown()
        server.server_close()
    assert seen == {"path": "/api/mesh/view", "host": f"127.0.0.1:{port}", "origin": None}


def test_the_mesh_probe_reports_a_loop_that_accepts_and_never_answers():
    """The kernel completes the TCP handshake for a wedged loop. Only the missing reply shows it."""
    with socket.socket() as wedged:
        wedged.bind(("127.0.0.1", 0))
        wedged.listen(1)
        problem = healthcheck.check_mesh_api(wedged.getsockname()[1], timeout=0.3)
    assert problem is not None and "mesh API does not answer" in problem


def test_the_mesh_probe_passes_the_api_guard():
    """No Origin and an IP literal as Host: the guard lets that through with an empty host set."""
    import cors_policy

    refusal = cors_policy.guard(
        method="GET",
        origin=None,
        host=healthcheck.mesh_probe_host(5001),
        content_type=None,
        has_body=False,
        hosts=set(),
        extras=[],
    )
    assert refusal is None


def test_a_wedged_server_loop_is_unhealthy(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path, programs=FOUR, status=STATUS_ALL_RUNNING, mesh_problem="mesh API does not answer")
    _player_state(tmp_path, ts=1000.0)
    problems, _states, advised = healthcheck.evaluate(1010.0)
    assert problems == ["mesh API does not answer"] and advised is True


def test_the_probe_timeouts_fit_inside_the_docker_healthcheck_timeout():
    import re

    dockerfile = (REPO / "backend" / "Dockerfile").read_text()
    limit = float(re.search(r"HEALTHCHECK[^\n]*--timeout=(\d+)s", dockerfile).group(1))
    assert healthcheck.PROBE_TIMEOUT_TOTAL_S < limit
    assert healthcheck.PROBE_TIMEOUT_TOTAL_S == (
        healthcheck.SUPERVISOR_TIMEOUT_S
        + healthcheck.CONFIG_API_TIMEOUT_S
        + healthcheck.PLAYER_PORT_TIMEOUT_S
        + healthcheck.MESH_API_TIMEOUT_S
    )


# -- restart_advised ---------------------------------------------------------------------------------
# The host watchdog restarts the whole container. That is right only for a fault a restart can fix
# and supervisord is not already handling.


def _status(**states):
    lines = STATUS_ALL_RUNNING.splitlines()
    for name, state in states.items():
        lines = [f"{name:<33}{state:<10}detail" if line.startswith(name + " ") else line for line in lines]
    return "\n".join(lines) + "\n"


def _advised(monkeypatch, tmp_path, *, status=STATUS_ALL_RUNNING, output_state="ok", ts=1000.0, **problems):
    _patch(monkeypatch, tmp_path, programs=FOUR, status=status, **problems)
    _player_state(tmp_path, ts=ts, output_state=output_state)
    found, _states, advised = healthcheck.evaluate(1010.0)
    return found, advised


def test_a_healthy_unit_advises_no_restart(monkeypatch, tmp_path):
    assert _advised(monkeypatch, tmp_path) == ([], False)


@pytest.mark.parametrize("state", ["BACKOFF", "STARTING"])
def test_a_program_that_supervisord_is_retrying_advises_no_restart(monkeypatch, tmp_path, state):
    """A unit whose DAC is absent sits in BACKOFF. A restart cuts its sources for nothing."""
    found, advised = _advised(
        monkeypatch, tmp_path, status=_status(sendspin_player=state), port_problem="player port 8928 refused"
    )
    assert found and advised is False


@pytest.mark.parametrize("state", ["FATAL", "EXITED", "UNKNOWN"])
def test_a_program_that_supervisord_gave_up_on_advises_a_restart(monkeypatch, tmp_path, state):
    found, advised = _advised(monkeypatch, tmp_path, status=_status(sendspin_player=state))
    assert found and advised is True


def test_a_program_that_an_operator_stopped_advises_no_restart(monkeypatch, tmp_path):
    found, advised = _advised(monkeypatch, tmp_path, status=_status(nginx="STOPPED"))
    assert found and advised is False


def test_a_missing_program_advises_a_restart(monkeypatch, tmp_path):
    status = "\n".join(line for line in STATUS_ALL_RUNNING.splitlines() if "nginx" not in line)
    found, advised = _advised(monkeypatch, tmp_path, status=status)
    assert found == ["program nginx is MISSING, not RUNNING"] and advised is True


def test_a_lost_output_on_a_live_player_advises_no_restart(monkeypatch, tmp_path):
    found, advised = _advised(monkeypatch, tmp_path, output_state="lost")
    assert found == ["player output is lost (device removed)"] and advised is False


def test_a_running_player_with_a_closed_port_advises_a_restart(monkeypatch, tmp_path):
    found, advised = _advised(monkeypatch, tmp_path, port_problem="player port 8928 refused")
    assert found and advised is True


def test_a_running_player_with_a_stale_report_advises_a_restart(monkeypatch, tmp_path):
    found, advised = _advised(monkeypatch, tmp_path, ts=10.0, output_state="lost")
    assert len(found) == 2 and advised is True


def test_a_running_config_api_that_does_not_answer_advises_a_restart(monkeypatch, tmp_path):
    found, advised = _advised(monkeypatch, tmp_path, api_problem="config API does not answer")
    assert found and advised is True


def test_a_config_api_in_backoff_advises_no_restart(monkeypatch, tmp_path):
    found, advised = _advised(
        monkeypatch, tmp_path, status=_status(config_api="BACKOFF"), api_problem="config API does not answer"
    )
    assert len(found) == 2 and advised is False


def test_the_mesh_probe_is_skipped_while_the_server_is_not_running(monkeypatch, tmp_path):
    found, advised = _advised(
        monkeypatch, tmp_path, status=_status(sendspin_server="BACKOFF"), mesh_problem="mesh API does not answer"
    )
    assert found == ["program sendspin_server is BACKOFF, not RUNNING"] and advised is False


def test_one_fault_that_needs_a_restart_wins_over_one_that_does_not(monkeypatch, tmp_path):
    found, advised = _advised(monkeypatch, tmp_path, status=_status(sendspin_player="BACKOFF", nginx="FATAL"))
    assert len(found) == 2 and advised is True


def test_a_supervisord_that_does_not_answer_advises_a_restart(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path, programs=FOUR, status="")
    monkeypatch.setattr(healthcheck, "supervisor_status", lambda: ({}, "supervisord did not answer (x)"))
    found, _states, advised = healthcheck.evaluate(1010.0)
    assert "supervisord did not answer (x)" in found and advised is True


def test_the_report_carries_restart_advised():
    bad = healthcheck.build_report(None, ["x"], {}, 100.0, restart_advised=False)
    assert bad["healthy"] is False and bad["restart_advised"] is False
    assert healthcheck.build_report(None, ["x"], {}, 100.0, restart_advised=True)["restart_advised"] is True
    assert healthcheck.build_report(None, [], {}, 100.0, restart_advised=True)["restart_advised"] is False


def test_main_writes_restart_advised(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path, programs=FOUR, status=_status(sendspin_player="BACKOFF"))
    monkeypatch.setattr(healthcheck, "HEALTH_FILE", str(tmp_path / "health.json"))
    assert healthcheck.main() == 1
    report = json.loads((tmp_path / "health.json").read_text())
    assert report["healthy"] is False and report["restart_advised"] is False
