"""Unit tests for the source-id and FIFO-path rules.

A source id reaches the filesystem: it names the FIFO the server creates with `os.mkfifo` and then
opens. `POST /api/mesh/source` took both the id and an optional path straight from the request body,
on an API that is unauthenticated and bound to 0.0.0.0 — so a caller on the VLAN could create a FIFO
anywhere the process could write, or point a source at any readable file and hear it played out a
speaker. CodeQL py/path-injection, three alerts, found on `main` 2026-09-13.

The rule is enforced twice on purpose (the API for a 400, `start_source` as the single choke point
every caller goes through), so these tests cover the shared module both of them use.

The accept cases matter as much as the reject cases: `cal:<player_id>` carries a colon and a
base64url key, so a validator that only allowed `[a-z0-9-]` would silently break every calibration
run. That is why the charset is what real ids contain rather than the narrowest set that passes a
scanner.

Run: `pytest tests/Unit/test_fifo_paths.py`.
"""

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "scripts"))

import fifo_paths  # noqa: E402

# -- source ids ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source_id",
    [
        "airplay-1",
        "spotify-2",
        "bluetooth-1",
        "cal:FjXD88ok30V-65lmRsztHRqAZ4mc2VGyqNjw_L0gxRM",  # the real shape: prefix + base64url key
        "a",
        "source.1",
    ],
)
def test_real_source_ids_are_accepted(source_id):
    assert fifo_paths.valid_source_id(source_id) is True


@pytest.mark.parametrize(
    "source_id",
    [
        "",
        "../../etc/passwd",
        "..",
        "a/b",
        "/tmp/evil",
        ".hidden",  # must not start with a dot
        "a\x00b",
        "a" * 129,  # past the length cap
        "a b",
        "a;rm -rf /",
        "a$(id)",
    ],
)
def test_path_bearing_ids_are_refused(source_id):
    assert fifo_paths.valid_source_id(source_id) is False


# -- FIFO paths ------------------------------------------------------------------------------------


def test_the_default_path_sits_in_the_fifo_dir():
    path = fifo_paths.fifo_path_for("airplay-1")
    assert path == f"{fifo_paths.FIFO_DIR}/airplay-1-fifo"
    assert fifo_paths.fifo_path_is_safe(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "/tmp/../etc/passwd",
        "/tmp/nested/dir/x-fifo",  # flat names only — every real FIFO is one
        "/root/.ssh/authorized_keys",
        "",
        "relative-fifo",  # resolves against the process cwd, which is not FIFO_DIR
    ],
)
def test_a_path_outside_the_fifo_dir_is_refused(path):
    assert fifo_paths.fifo_path_is_safe(path) is False


def test_a_traversal_that_lands_back_inside_is_accepted():
    """The check resolves the path rather than matching its text, so this is genuinely /tmp."""
    assert fifo_paths.fifo_path_is_safe(f"{fifo_paths.FIFO_DIR}/sub/../airplay-1-fifo") is True


def test_a_null_byte_is_refused():
    assert fifo_paths.fifo_path_is_safe(f"{fifo_paths.FIFO_DIR}/x\x00-fifo") is False


# -- the guard at the syscall ------------------------------------------------------------------------


def test_the_feeder_refuses_a_bad_path_at_the_syscall_itself():
    """`start_source` checks too, and checking twice is the point.

    `os.mkfifo` and `os.open` are the sink. A guard one frame up in `start_source` protects only the
    callers that go through it, and a SourceFeeder built by anything else would trust its caller.
    """
    pytest.importorskip("numpy", reason="sendspin_server imports numpy at module scope")
    pytest.importorskip("aiosendspin", reason="sendspin_server imports aiosendspin at module scope")
    import sendspin_server

    feeder = sendspin_server.SourceFeeder.__new__(sendspin_server.SourceFeeder)
    feeder.fifo_path = "/etc/passwd"
    with pytest.raises(ValueError, match="directly inside"):
        feeder._ensure_fifo()  # noqa: SLF001

    # A real path passes, and the checked value is what the syscalls get. It is RESOLVED, so a
    # traversal that lands back inside is normalised rather than merely tolerated — and on a Mac,
    # where /tmp is a symlink to /private/tmp, the expected value has to be resolved too.
    expected = os.path.realpath(fifo_paths.fifo_path_for("airplay-1"))

    feeder.fifo_path = fifo_paths.fifo_path_for("airplay-1")
    assert feeder._checked_fifo_path() == expected  # noqa: SLF001

    feeder.fifo_path = f"{fifo_paths.FIFO_DIR}/sub/../airplay-1-fifo"
    assert feeder._checked_fifo_path() == expected  # noqa: SLF001
