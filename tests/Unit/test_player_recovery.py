"""Unit tests for the player's self-recovery: the paths that end a hang without a power cycle.

Each test here is for a state that used to be invisible. The player stayed RUNNING under
supervisord, held its port, and did nothing — so nothing restarted it. The fakes follow
test_player_health.py: methods are taken unbound from the class, so no PortAudio stream, no
aiosendspin client and no socket is constructed.

Run: `pytest tests/Unit/test_player_recovery.py`.
"""

import asyncio
import logging
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "scripts"))

pytest.importorskip("numpy", reason="sendspin_player imports numpy")
pytest.importorskip("aiosendspin", reason="aiosendspin is a real runtime dep")

import sendspin_player  # noqa: E402

LOG = "plum.sendspin_player"


# -- background tasks ------------------------------------------------------------------------------


def test_a_background_task_that_dies_is_logged(caplog):
    """A bare ensure_future drops the exception until the task is collected, if it ever is."""

    async def scenario():
        async def boom():
            raise RuntimeError("watcher fell over")

        task = sendspin_player._spawn(boom(), name="output watch")
        assert task in sendspin_player._BACKGROUND_TASKS, "a reference must be held or the task can be collected"
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert task not in sendspin_player._BACKGROUND_TASKS

    with caplog.at_level(logging.ERROR, logger=LOG):
        asyncio.run(scenario())
    messages = [r.getMessage() for r in caplog.records]
    assert any("output watch" in m and "watcher fell over" in m for m in messages)


def test_a_cancelled_or_clean_background_task_is_quiet(caplog):
    async def scenario():
        async def forever():
            await asyncio.sleep(3600)

        async def done():
            return None

        task = sendspin_player._spawn(forever(), name="rename watch")
        sendspin_player._spawn(done(), name="one shot")
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        assert not sendspin_player._BACKGROUND_TASKS

    with caplog.at_level(logging.DEBUG, logger=LOG):
        asyncio.run(scenario())
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
