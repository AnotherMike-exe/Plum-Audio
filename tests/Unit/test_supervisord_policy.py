"""The supervisord restart policy, read from the shipped program files.

supervisord's default `startretries` is 3. A program that fails four fast starts goes FATAL and is
never tried again, while PID 1 stays up — so a DAC or a port that was not ready at boot left a unit
dead until a power cycle. These tests keep every program on an explicit policy.

Run: `pytest tests/Unit/test_supervisord_policy.py`.
"""

import configparser
from pathlib import Path

import pytest

CONF_D = Path(__file__).resolve().parents[2] / "backend" / "supervisord" / "conf.d"
PROGRAMS = sorted(CONF_D.glob("*.ini"))


def _section(path: Path) -> configparser.SectionProxy:
    parser = configparser.RawConfigParser(inline_comment_prefixes=None)
    parser.read(path)
    (name,) = [s for s in parser.sections() if s.startswith("program:")]
    return parser[name]


def test_there_are_four_programs():
    assert len(PROGRAMS) == 4


@pytest.mark.parametrize("path", PROGRAMS, ids=lambda p: p.name)
def test_every_program_retries_a_failed_start_far_past_the_default(path):
    retries = int(_section(path)["startretries"])
    # Linear backoff, 1 s more per failure and no cap: n retries end in an n-second gap and take
    # n(n+1)/2 seconds in total. Under 100 gives up inside 90 minutes; over 1800 ends in gaps longer
    # than 30 minutes, which is a unit that stays silent that long after its DAC returns.
    assert 100 <= retries <= 1800


@pytest.mark.parametrize("path", PROGRAMS, ids=lambda p: p.name)
def test_every_program_restarts_after_any_exit(path):
    section = _section(path)
    assert section["autostart"] == "true"
    # `true`, not `unexpected`: the player exits non-zero ON PURPOSE to be replaced, and no exit of
    # any of these four is one a unit should stay down after.
    assert section["autorestart"] == "true"


@pytest.mark.parametrize("path", PROGRAMS, ids=lambda p: p.name)
def test_startsecs_is_long_enough_to_count_a_slow_failure_as_a_failure(path):
    section = _section(path)
    secs = int(section["startsecs"])
    audio = "sendspin" in path.name
    assert secs >= (10 if audio else 5)
