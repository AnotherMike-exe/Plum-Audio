"""Unit tests for how the deploy scripts handle the rig password and the unit env file.

`sshpass -p "$PW"` puts the password in the workstation's process list for every connection, and
`bash -s -- '$PW'` put it in the unit's process list for the whole remote block. Both scripts now
send it by environment (`sshpass -e`) and on stdin.

The helpers are extracted from the real scripts and run against a stub `sshpass` that records its
argv, its environment and its stdin. Nothing here opens a connection.

Run: `pytest tests/Unit/test_deploy_secrets.py`.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DEPLOY = REPO / "docker" / "deploy.sh"
PROVISION = REPO / "scripts" / "host-setup" / "provision.sh"
PLUM_INIT = REPO / "scripts" / "plum-init.sh"

PASSWORD = 'p\'w $x "q" \\z'  # a quote, a space, a dollar: all broke the old remote argument

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _function(text: str, name: str) -> str:
    """The source of one shell function, one-line or multi-line."""
    match = re.search(rf"^{name}\(\)\s*\{{.*?^\}}$|^{name}\(\)\s*\{{.*\}}$", text, re.M | re.S)
    assert match, f"{name}() not found"
    one_line = re.search(rf"^{name}\(\)\s*\{{.*\}}$", text, re.M)
    return one_line.group(0) if one_line else match.group(0)


def _harness(script: Path, tmp_path: Path, body: str, sshpass_body: str) -> subprocess.CompletedProcess:
    text = script.read_text()
    stub = tmp_path / "bin" / "sshpass"
    stub.parent.mkdir(exist_ok=True)
    stub.write_text(f"#!/bin/bash\n{sshpass_body}\n")
    stub.chmod(0o755)
    names = ["sshpass_", "retry_", "ssh_", "ssh_sudo_"]
    if "retry_ssh_()" in text:  # deploy.sh retries a plain command on a transport failure only
        names.append("retry_ssh_")
    functions = "\n".join(_function(text, name) for name in names)
    program = (
        f'set -euo pipefail\nPW="$PLUM_TEST_PW"\nUSER_=pi\nSSH_OPTS="-o X=y"\nsleep() {{ :; }}\n{functions}\n{body}\n'
    )
    return subprocess.run(
        ["bash", "-c", program],
        env={"PATH": f"{stub.parent}:/usr/bin:/bin", "PLUM_TEST_PW": PASSWORD, "OUT": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=30,
    )


RECORD = 'printf "%s\\n" "$@" > "$OUT/argv"; printf "%s" "${SSHPASS-unset}" > "$OUT/env"; cat > "$OUT/stdin"'


@pytest.mark.parametrize("script", [DEPLOY, PROVISION], ids=["deploy", "provision"])
def test_the_password_is_never_an_argument(script):
    text = script.read_text()
    assert "sshpass -p" not in text
    assert "'$PW'" not in text  # the old remote argument, `bash -s -- '$PW'`
    assert "sshpass -e" in text


@pytest.mark.parametrize("script", [DEPLOY, PROVISION], ids=["deploy", "provision"])
def test_ssh_sends_the_password_in_the_environment(script, tmp_path):
    result = _harness(script, tmp_path, 'ssh_ unit-1 "uname -a"', RECORD)
    assert result.returncode == 0, result.stderr
    argv = (tmp_path / "argv").read_text()
    assert PASSWORD not in argv
    assert argv.splitlines()[:2] == ["-e", "ssh"]
    assert "pi@unit-1" in argv
    assert (tmp_path / "env").read_text() == PASSWORD


@pytest.mark.parametrize("script", [DEPLOY, PROVISION], ids=["deploy", "provision"])
def test_the_remote_block_gets_the_password_on_stdin_as_its_first_argument(script, tmp_path):
    """Run the stdin the helper sends through a real `bash -s`, as the unit would."""
    block = 'PW="$1"; A="$2"\nprintf \'%s|%s|%s\' "$PW" "$A" "$#" > "$OUT/seen"\n'
    body = f"ssh_sudo_ unit-1 \"bash -s -- 'payload dir'\" <<'EOS'\n{block}EOS"
    # The remote command is the last argument. The stub runs it with a local bash.
    remote = 'printf "%s\\n" "$@" > "$OUT/argv"; exec bash -c "${@: -1}"'
    result = _harness(script, tmp_path, body, remote)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "seen").read_text() == f"{PASSWORD}|payload dir|2"
    assert PASSWORD not in (tmp_path / "argv").read_text()


@pytest.mark.parametrize("script", [DEPLOY, PROVISION], ids=["deploy", "provision"])
def test_a_failed_remote_block_is_a_failure_and_does_not_run_twice(script, tmp_path):
    """Before: the retry ran with stdin at end-of-file, `bash -s` ran nothing, and the step passed."""
    stub = 'echo run >> "$OUT/runs"; cat > /dev/null; exit 7'
    result = _harness(script, tmp_path, "ssh_sudo_ unit-1 'bash -s --' <<'EOS' || echo \"rc=$?\"\ntrue\nEOS", stub)
    assert "rc=7" in result.stdout
    assert (tmp_path / "runs").read_text().count("run") == 1


@pytest.mark.parametrize("script", [DEPLOY, PROVISION], ids=["deploy", "provision"])
def test_a_refused_connection_is_retried_with_the_whole_script(script, tmp_path):
    stub = (
        'n=$(cat "$OUT/n" 2>/dev/null || echo 0); echo $((n+1)) > "$OUT/n"; '
        'cat > "$OUT/stdin.$n"; [ "$n" = 0 ] && exit 255; exit 0'
    )
    body = "ssh_sudo_ unit-1 'bash -s --' <<'EOS'\necho the-block\nEOS\necho \"rc=$?\""
    result = _harness(script, tmp_path, body, stub)
    assert "rc=0" in result.stdout, result.stderr
    first, second = (tmp_path / "stdin.0").read_text(), (tmp_path / "stdin.1").read_text()
    assert "echo the-block" in second
    assert first == second


@pytest.mark.parametrize("script", [DEPLOY, PROVISION], ids=["deploy", "provision"])
def test_a_block_that_ends_before_it_reads_all_of_stdin_is_not_a_failure(script, tmp_path):
    """pipefail would turn the writer's SIGPIPE into a failed step."""
    body = "ssh_sudo_ unit-1 'bash -s --' <<'EOS'\n" + ("# padding line\n" * 20000) + 'EOS\necho "rc=$?"'
    result = _harness(script, tmp_path, body, "exit 0")
    assert "rc=0" in result.stdout, result.stderr


# -- the env file ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("script", "write"),
    [(DEPLOY, 'cat > "$ROOT/plum-audio.env" <<ENV'), (PLUM_INIT, 's tee "$OLD_ENV" >/dev/null <<ENV')],
    ids=["deploy", "plum-init"],
)
def test_the_env_file_is_made_mode_600_before_the_secret_is_written(script, write):
    """plum-audio.env holds PLUM_FLEET_PSK. The line before the write must create it 0600."""
    lines = script.read_text().splitlines()
    index = lines.index(write)
    assert re.search(r"install -m 600 /dev/null \"\$(ROOT/plum-audio\.env|OLD_ENV)\"", lines[index - 1])


def test_install_gives_mode_600_and_replaces_a_readable_file(tmp_path):
    """The exact command, on a file that already exists with mode 644."""
    target = tmp_path / "plum-audio.env"
    target.write_text("PLUM_FLEET_PSK=old\n")
    target.chmod(0o644)
    subprocess.run(["install", "-m", "600", "/dev/null", str(target)], check=True)
    with open(target, "w") as fh:  # `cat >` and `tee` truncate and keep the mode
        fh.write("PLUM_FLEET_PSK=new\n")
    assert target.stat().st_mode & 0o777 == 0o600
