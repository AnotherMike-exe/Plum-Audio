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


# -- the integration-test helpers and hotpatch.sh ----------------------------------------------
#
# tests/Integration/lib.sh kept `sshpass -p` and `echo '$PW' | sudo -S` after deploy.sh lost them.
# Both put the rig password in a process list: the first on the workstation, the second on both.

LIB = REPO / "tests" / "Integration" / "lib.sh"
HOTPATCH = REPO / "docker" / "hotpatch.sh"
SHELL_TESTS = sorted((REPO / "tests" / "Integration").glob("*.sh"))


@pytest.mark.parametrize("script", [*SHELL_TESTS, HOTPATCH], ids=lambda p: p.name)
def test_no_integration_script_puts_the_password_on_a_command_line(script):
    text = script.read_text()
    assert "sshpass -p" not in text
    assert "'$PW'" not in text  # `echo '$PW' | sudo -S` inside a remote command string


def _lib_harness(tmp_path: Path, body: str, sshpass_body: str) -> subprocess.CompletedProcess:
    text = LIB.read_text()
    stub = tmp_path / "bin" / "sshpass"
    stub.parent.mkdir(exist_ok=True)
    stub.write_text(f"#!/bin/bash\n{sshpass_body}\n")
    stub.chmod(0o755)
    names = ["retry_ssh_", "ssh_", "sudo_ssh_", "dexec_", "feed_fifo_", "stop_feed_"]
    functions = "\n".join(_function(text, name) for name in names)
    program = f'set -uo pipefail\nPW="$PLUM_TEST_PW"\nUSER_=pi\nSSH_OPTS="-o X=y"\nsleep() {{ :; }}\n{functions}\n{body}\n'
    return subprocess.run(
        ["bash", "-c", program],
        env={"PATH": f"{stub.parent}:/usr/bin:/bin", "PLUM_TEST_PW": PASSWORD, "OUT": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_lib_ssh_sends_the_password_in_the_environment(tmp_path):
    result = _lib_harness(tmp_path, 'ssh_ unit-1 "uname -a"', RECORD)
    assert result.returncode == 0, result.stderr
    argv = (tmp_path / "argv").read_text()
    assert PASSWORD not in argv
    assert argv.splitlines()[:2] == ["-e", "ssh"]
    assert (tmp_path / "env").read_text() == PASSWORD
    assert (tmp_path / "stdin").read_text() == "", "a plain ssh_ gets no password on stdin"


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        ("dexec_ unit-1 ls /tmp", "sudo -S -p '' docker exec plum-audio ls /tmp"),
        ("feed_fifo_ unit-1 /tmp/a-fifo 3", "of=/tmp/a-fifo bs=17640 count=3"),
        ("stop_feed_ unit-1 /tmp/a-fifo", "pkill -f 'dd if=/dev/zero of=/tmp/a-fifo'"),
    ],
    ids=["dexec", "feed", "stop-feed"],
)
def test_lib_sudo_gets_the_password_on_stdin(tmp_path, call, expected):
    result = _lib_harness(tmp_path, call, RECORD)
    assert result.returncode == 0, result.stderr
    argv = (tmp_path / "argv").read_text()
    assert PASSWORD not in argv
    assert expected in argv
    assert "sudo -S -p ''" in argv
    assert (tmp_path / "stdin").read_text() == PASSWORD + "\n"
    assert (tmp_path / "env").read_text() == PASSWORD


def test_lib_sudo_sends_the_password_again_on_a_retry(tmp_path):
    """A retry with stdin at end-of-file gives sudo no password."""
    stub = (
        'n=$(cat "$OUT/n" 2>/dev/null || echo 0); echo $((n+1)) > "$OUT/n"; '
        'cat > "$OUT/stdin.$n"; [ "$n" = 0 ] && exit 255; exit 0'
    )
    result = _lib_harness(tmp_path, 'dexec_ unit-1 true; echo "rc=$?"', stub)
    assert "rc=0" in result.stdout, result.stderr
    assert (tmp_path / "stdin.0").read_text() == PASSWORD + "\n"
    assert (tmp_path / "stdin.1").read_text() == PASSWORD + "\n"


def test_lib_sudo_passes_the_remote_status_and_does_not_retry_it(tmp_path):
    stub = 'echo run >> "$OUT/runs"; cat > /dev/null; exit 7'
    result = _lib_harness(tmp_path, 'dexec_ unit-1 false; echo "rc=$?"', stub)
    assert "rc=7" in result.stdout
    assert (tmp_path / "runs").read_text().count("run") == 1


# -- the fleet PSK -----------------------------------------------------------------------------


def test_the_fleet_psk_is_not_a_remote_argument():
    text = DEPLOY.read_text()
    assert "'${PLUM_FLEET_PSK:-}'" not in text
    assert 'FLEET_PSK="${12:-}"' not in text
    for line in text.splitlines():
        if "bash -s" in line and "ssh_sudo_ " in line:
            assert "PSK" not in line.split("ssh_sudo_ ", 1)[1], line  # the remote command


def test_the_fleet_psk_reaches_the_remote_block_on_stdin(tmp_path):
    """Run the real line from deploy.sh through a local `bash -s`, with a PSK that needs quotes."""
    psk = "k'e y$(touch PWNED)\"z\\"
    lines = DEPLOY.read_text().splitlines()
    (line,) = [item.strip() for item in lines if "FLEET_PSK=%q" in item and not item.strip().startswith("#")]
    assert line.endswith("|| return 1")
    block = 'printf \'%s|%s|%s\' "$FLEET_PSK" "$2" "$#" > "$OUT/seen"\n'
    names = ["host", "unit_id", "unit_name", "player_id", "player_name", "dac", "profile", "player_enabled"]
    setup = "\n".join(f"{name}=v" for name in names) + "\nREMOTE_ROOT=/opt/x\nPLUM_IMAGE_NAME=i\nPLUM_IMAGE_TAG=t\n"
    body = f'PLUM_FLEET_PSK="$TEST_PSK"\n{setup}{line[: -len("|| return 1")]}\n{block}EOS'
    remote = 'printf "%s\\n" "$@" > "$OUT/argv"; cd "$OUT"; exec bash -c "${@: -1}"'
    text = DEPLOY.read_text()
    stub = tmp_path / "bin" / "sshpass"
    stub.parent.mkdir()
    stub.write_text(f"#!/bin/bash\n{remote}\n")
    stub.chmod(0o755)
    functions = "\n".join(_function(text, name) for name in ["sshpass_", "ssh_sudo_"])
    program = f'set -euo pipefail\nPW="$PLUM_TEST_PW"\nUSER_=pi\nSSH_OPTS="-o X=y"\nsleep() {{ :; }}\n{functions}\n{body}\n'
    env = {"PATH": f"{stub.parent}:/usr/bin:/bin", "PLUM_TEST_PW": PASSWORD, "OUT": str(tmp_path), "TEST_PSK": psk}
    result = subprocess.run(["bash", "-c", program], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "seen").read_text() == f"{psk}|/opt/x|11"
    assert "k'e" not in (tmp_path / "argv").read_text()
    assert not (tmp_path / "PWNED").exists()


# -- the fleet PSK on the workstation and on the unit's command line ---------------------------


def test_deploy_makes_its_env_file_mode_600_before_the_psk_is_written(tmp_path):
    """`printf >>` on a file that does not exist creates it with the default umask."""
    text = DEPLOY.read_text()
    assert ">> \"${HERE}/.deploy.env\"" not in text, "every write goes through save_fleet_psk"
    assert text.count('save_fleet_psk "$PLUM_FLEET_PSK"') == 2
    target = tmp_path / ".deploy.env"
    program = f'set -euo pipefail\numask 022\nHERE="$OUT"\n{_function(text, "save_fleet_psk")}\nsave_fleet_psk first\n'
    env = {"PATH": "/usr/bin:/bin", "OUT": str(tmp_path)}
    subprocess.run(["bash", "-c", program], env=env, check=True, timeout=30)
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.read_text() == "PLUM_FLEET_PSK=first\n"

    target.write_text("PLUM_TEST_PW=x\n")
    target.chmod(0o644)
    subprocess.run(["bash", "-c", program], env=env, check=True, timeout=30)
    assert target.stat().st_mode & 0o777 == 0o600, "a file that exists gets the mode too"
    assert target.read_text() == "PLUM_TEST_PW=x\nPLUM_FLEET_PSK=first\n"


def _init_arguments(tmp_path: Path, args: list[str], stdin: str = "") -> subprocess.CompletedProcess:
    """Run the real argument parser of plum-init.sh, then print what it found."""
    text = PLUM_INIT.read_text()
    parser = text[text.index("PSK_ON_ARGV=0") : text.index("say()  {")]
    program = (
        'set -euo pipefail\nNAME=""\nFLEET_PSK=""\nusage() { :; }\n'
        f'{parser}\nprintf "%s|%s|%s" "$NAME" "$FLEET_PSK" "$PSK_ON_ARGV"\n'
    )
    return subprocess.run(
        ["bash", "-c", program, "plum-init.sh", *args],
        env={"PATH": "/usr/bin:/bin"},
        input=stdin,
        capture_output=True,
        text=True,
        timeout=30,
    )


PSK = "k'e y$(touch PWNED)\"z"


def test_plum_init_reads_the_psk_from_a_file(tmp_path):
    secret = tmp_path / "psk"
    secret.write_text(PSK + "\nsecond line\n")
    result = _init_arguments(tmp_path, ["Kitchen", "--fleet-psk-file", str(secret)])
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"Kitchen|{PSK}|0"


def test_plum_init_takes_a_line_copied_from_an_env_file(tmp_path):
    secret = tmp_path / "psk"
    secret.write_text("PLUM_FLEET_PSK=abc-123_x\r\n")
    assert _init_arguments(tmp_path, ["--fleet-psk-file", str(secret), "Kitchen"]).stdout == "Kitchen|abc-123_x|0"


def test_plum_init_reads_the_psk_from_stdin(tmp_path):
    result = _init_arguments(tmp_path, ["Kitchen", "--fleet-psk", "-"], stdin=PSK + "\n")
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"Kitchen|{PSK}|0"


def test_plum_init_keeps_the_old_flag_and_marks_it_for_a_warning(tmp_path):
    assert _init_arguments(tmp_path, ["Kitchen", "--fleet-psk", "abc"]).stdout == "Kitchen|abc|1"
    text = PLUM_INIT.read_text()
    (line,) = [item for item in text.splitlines() if item.startswith('[[ "$PSK_ON_ARGV" == 1 ]] && warn ')]
    assert "--fleet-psk-file" in line


@pytest.mark.parametrize("args", [["--fleet-psk-file", "/nonexistent/psk"], ["--fleet-psk", "-"]])
def test_plum_init_refuses_a_psk_source_with_no_secret(tmp_path, args):
    result = _init_arguments(tmp_path, ["Kitchen", *args])
    assert result.returncode == 2
    assert "Kitchen|" not in result.stdout


def test_plum_init_does_not_print_a_command_that_holds_the_psk():
    text = PLUM_INIT.read_text()
    assert "--fleet-psk ${FLEET_PSK}" not in text
    assert '--fleet-psk-file <the file>' in text
