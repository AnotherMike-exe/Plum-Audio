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


# -- stop() ----------------------------------------------------------------------------------------


class _StopRenderer:
    def __init__(self):
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class _StopClient:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.disconnected = False

    async def disconnect(self) -> None:
        if self.fail:
            raise RuntimeError("goodbye could not be sent")
        self.disconnected = True


class _StopListener:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.stopped = False

    async def stop(self) -> None:
        if self.fail:
            raise RuntimeError("runner cleanup failed")
        self.stopped = True


class _StopPlayer:
    """Only what stop() touches."""

    def __init__(self, tmp_path, *, listener_fails: bool = False, client_fails: bool = False):
        self.renderer = _StopRenderer()
        self.client = _StopClient(fail=client_fails)
        self._listener = _StopListener(fail=listener_fails)
        self._avahi = None
        self._report_task = self._relay_task = self._health_task = None
        self._save_task = None
        self._volume = 100
        self._muted = False
        self._state_file = str(tmp_path / "player_state.json")

    stop = sendspin_player.SendspinPlayer.stop
    _flush_render_state = sendspin_player.SendspinPlayer._flush_render_state
    _publish = sendspin_player.SendspinPlayer._publish_render_state
    _save_render_state = sendspin_player.SendspinPlayer._save_render_state


class _DetachedClient(_StopClient):
    connected = False
    _initial_volume = 100
    _initial_muted = False


def test_a_level_change_just_before_sigterm_is_saved(tmp_path):
    """The save is debounced 2 s. A stop inside that window used to drop it."""
    import player_state

    async def scenario():
        p = _StopPlayer(tmp_path)
        p.client = _DetachedClient()
        p._volume, p._muted = 23, True
        p._publish()  # starts the debounced save
        assert p._save_task is not None and not p._save_task.done()
        assert not Path(p._state_file).exists(), "the debounce has not expired yet"
        await p.stop()
        return p

    p = asyncio.run(scenario())
    assert player_state.load_render_state(p._state_file, default_volume=100) == (23, True)
    assert p._save_task is None


def test_stop_does_not_write_when_nothing_is_pending(tmp_path):
    p = _StopPlayer(tmp_path)
    asyncio.run(p.stop())
    assert not Path(p._state_file).exists()


def test_the_dac_is_released_when_the_listener_fails_to_stop(tmp_path, caplog):
    p = _StopPlayer(tmp_path, listener_fails=True)
    with caplog.at_level(logging.WARNING, logger=LOG):
        asyncio.run(p.stop())  # must not raise
    assert p.renderer.stopped
    assert any("listener" in r.getMessage() for r in caplog.records)


def test_the_listener_and_dac_are_released_when_the_client_fails_to_disconnect(tmp_path):
    p = _StopPlayer(tmp_path, client_fails=True)
    asyncio.run(p.stop())
    assert p._listener is None and p.renderer.stopped


def test_a_background_task_that_already_died_does_not_abort_stop(tmp_path):
    async def scenario():
        async def boom():
            raise RuntimeError("health loop died earlier")

        p = _StopPlayer(tmp_path)
        p._health_task = asyncio.ensure_future(boom())
        await asyncio.sleep(0)
        await p.stop()
        return p

    assert asyncio.run(scenario()).renderer.stopped
