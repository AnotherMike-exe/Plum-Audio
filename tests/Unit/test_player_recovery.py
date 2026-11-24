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


# -- consume relay ---------------------------------------------------------------------------------


def test_the_relay_retry_delay_doubles_and_stops_at_thirty_seconds():
    delays = [sendspin_player._relay_backoff(n) for n in range(1, 9)]
    assert delays == [2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0, 30.0]


class _RelayPlayer:
    """_relay_loop with a scripted _relay_session: True = connects then closes, False = fails."""

    relay_url = "ws://127.0.0.1:5001/api/mesh/consume?role=player"

    def __init__(self, script):
        self._script = list(script)
        self._relay_failures = 0
        self._relay_down_since = 0.0

    async def _relay_session(self) -> None:
        if not self._script:
            raise asyncio.CancelledError
        if not self._script.pop(0):
            raise ConnectionRefusedError("mesh API is down")
        self._relay_connected()

    _relay_loop = sendspin_player.SendspinPlayer._relay_loop
    _relay_connected = sendspin_player.SendspinPlayer._relay_connected


def _run_relay(script, monkeypatch, caplog):
    delays: list[float] = []

    async def fake_sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr(sendspin_player.asyncio, "sleep", fake_sleep)
    with caplog.at_level(logging.INFO, logger=LOG), pytest.raises(asyncio.CancelledError):
        asyncio.run(_RelayPlayer(script)._relay_loop())
    return delays


def test_the_relay_backs_off_while_the_mesh_api_is_down(monkeypatch, caplog):
    delays = _run_relay([False] * 6, monkeypatch, caplog)
    assert delays == [2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


def test_the_relay_warns_once_for_an_outage_and_reports_the_recovery(monkeypatch, caplog):
    _run_relay([False, False, False, True], monkeypatch, caplog)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, "one line for the outage, not one per attempt"
    assert "mesh API is down" in warnings[0].getMessage()
    recovered = [r for r in caplog.records if r.levelno == logging.INFO and "after 3 failed" in r.getMessage()]
    assert len(recovered) == 1


def test_the_relay_backoff_restarts_after_a_recovery(monkeypatch, caplog):
    delays = _run_relay([False, False, True, False], monkeypatch, caplog)
    assert delays == [2.0, 4.0, 2.0, 2.0]
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2, "a second outage warns again"


class _CommandClient:
    async def send_group_command(self, cmd, **kwargs):
        raise RuntimeError("not connected")


def test_a_relay_command_that_fails_is_not_swallowed():
    class P:
        client = _CommandClient()
        _relay_command = sendspin_player.SendspinPlayer._relay_command

    with pytest.raises(RuntimeError):
        asyncio.run(P()._relay_command({"t": "cmd", "command": "play"}))


# -- output stream death ---------------------------------------------------------------------------


class _DeadableStream:
    """A PortAudio stream that can die the two ways a real one does."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.active = False
        self.latency = 0.01

    def start(self):
        self.active = True

    def stop(self):
        self.active = False
        self.kwargs["finished_callback"]()  # PortAudio fires this for OUR stop as well

    def close(self):
        pass

    def die(self):
        """The device was removed: PortAudio stops the stream and says so."""
        self.active = False
        self.kwargs["finished_callback"]()


class _DeadableSoundDevice:
    def __init__(self):
        self.fail_opens = 0
        self.streams: list[_DeadableStream] = []

    def RawOutputStream(self, **kwargs):  # noqa: N802 - mirrors the real API's name
        if self.fail_opens:
            self.fail_opens -= 1
            raise OSError("device unavailable")
        self.streams.append(_DeadableStream(**kwargs))
        return self.streams[-1]


@pytest.fixture
def fake_sd(monkeypatch):
    import audio_devices

    fake = _DeadableSoundDevice()
    monkeypatch.setattr(sendspin_player, "sd", fake)
    monkeypatch.setattr(audio_devices, "resolve_portaudio_index", lambda spec: (None, None))
    return fake


def _renderer(device="hifiberry"):
    renderer = sendspin_player.AlsaRenderer(44100, 2, 16, device=device, target_buffer_ms=300)
    renderer.start()
    return renderer


def test_a_running_stream_has_no_fault(fake_sd):
    assert _renderer().stream_fault() is None


def test_a_stream_that_portaudio_finished_is_a_fault(fake_sd):
    renderer = _renderer()
    fake_sd.streams[0].die()
    assert "finished" in renderer.stream_fault()


def test_an_inactive_stream_is_a_fault(fake_sd):
    renderer = _renderer()
    fake_sd.streams[0].active = False
    assert "not active" in renderer.stream_fault()


def test_our_own_stop_is_not_a_fault(fake_sd):
    """The finished callback fires for a deliberate stop too. That must not read as a death."""
    renderer = _renderer()
    renderer.stop()
    assert renderer.stream_finished is False
    renderer.start()
    assert renderer.stream_fault() is None


def test_recover_reopens_the_same_device_and_clears_the_fault(fake_sd):
    renderer = _renderer("hifiberry")
    fake_sd.streams[0].die()
    assert renderer.recover() is True
    assert len(fake_sd.streams) == 2 and fake_sd.streams[1].kwargs["device"] == "hifiberry"
    assert renderer.stream_fault() is None
    assert renderer.reopening is False


def test_recover_reports_a_device_that_is_still_away(fake_sd):
    renderer = _renderer()
    fake_sd.streams[0].die()
    fake_sd.fail_opens = 1
    assert renderer.recover() is False
    assert renderer.stream_fault() == "no output stream is open"
    assert renderer.recover() is True, "the next attempt must still target the device"
    assert fake_sd.streams[-1].kwargs["device"] == "hifiberry"


def test_recover_and_an_output_switch_never_overlap(fake_sd, monkeypatch):
    """Two PortAudio re-initialisations at once SIGSEGV the interpreter. Tested CONCURRENTLY."""
    import threading
    import time as real_time

    renderer = _renderer("a")
    inside = 0
    worst = 0
    guard = threading.Lock()
    real_open = renderer._open

    def slow_open(spec):
        nonlocal inside, worst
        with guard:
            inside += 1
            worst = max(worst, inside)
        real_time.sleep(0.02)
        try:
            real_open(spec)
        finally:
            with guard:
                inside -= 1

    monkeypatch.setattr(renderer, "_open", slow_open)
    threads = [threading.Thread(target=renderer.recover) for _ in range(4)]
    threads += [threading.Thread(target=renderer.reopen, args=(f"dev{n}",)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert worst == 1


def test_the_callback_is_counted(fake_sd):
    renderer = _renderer()
    out = bytearray(480 * 4)
    renderer._callback(out, 480, None, None)
    renderer._callback(out, 480, None, None)
    assert renderer.callbacks == 2


class _WatchedRenderer:
    """What _check_output reads, with a recover() that follows a script."""

    device = open_device = "hifiberry:0"

    def __init__(self, recover_results=()):
        self.callbacks = 0
        self.reopening = False
        self.fault: str | None = None
        self.recover_results = list(recover_results)
        self.recover_calls = 0

    def stream_fault(self):
        return self.fault

    def recover(self) -> bool:
        self.recover_calls += 1
        ok = self.recover_results.pop(0) if self.recover_results else False
        if ok:
            self.fault = None
        return ok

    def stats(self) -> str:
        return "[fake]"


class _OutputPlayer:
    def __init__(self, tmp_path, renderer):
        self.renderer = renderer
        self._state_file = str(tmp_path / "player_state.json")
        self._last_reported_state = sendspin_player.PlayerHealth.SYNCHRONIZED
        self._sessions = {}
        self._init_recovery_state()

    for _name in (
        "_init_recovery_state",
        "request_exit",
        "_check_output",
        "_set_output_state",
        "_health_report",
        "_write_health_report",
    ):
        locals()[_name] = getattr(sendspin_player.SendspinPlayer, _name)
    del _name


def _ticks(player, n, *, advance=True):
    async def run():
        for _ in range(n):
            if advance:
                player.renderer.callbacks += 300
            await player._check_output()

    asyncio.run(run())


def test_a_live_stream_is_left_alone(tmp_path):
    p = _OutputPlayer(tmp_path, _WatchedRenderer())
    _ticks(p, 10)
    assert p.renderer.recover_calls == 0 and p._output_state == "ok"
    assert not Path(p._state_file).exists(), "a healthy poll writes nothing from this path"


def test_a_stalled_callback_counter_triggers_a_reopen(tmp_path, caplog):
    """PortAudio still says active; only the counter shows the stream is dead."""
    p = _OutputPlayer(tmp_path, _WatchedRenderer(recover_results=[True]))
    _ticks(p, 2)
    with caplog.at_level(logging.WARNING, logger=LOG):
        _ticks(p, 1, advance=False)
        assert p.renderer.recover_calls == 0, "one quiet poll is not enough"
        _ticks(p, 1, advance=False)
    assert p.renderer.recover_calls == 1
    assert p._output_state == "ok" and p._output_reopens == 1
    assert any("counter stalled" in r.getMessage() for r in caplog.records)


def test_a_finished_stream_triggers_a_reopen_on_the_next_poll(tmp_path):
    p = _OutputPlayer(tmp_path, _WatchedRenderer(recover_results=[True]))
    p.renderer.fault = "PortAudio finished the stream"
    _ticks(p, 1)
    assert p.renderer.recover_calls == 1 and p._output_state == "ok"
    assert p.exit_code is None


def test_the_process_exits_non_zero_after_the_reopens_fail(tmp_path):
    import json

    p = _OutputPlayer(tmp_path, _WatchedRenderer())
    p.renderer.fault = "no output stream is open"
    _ticks(p, sendspin_player.OUTPUT_REOPEN_MAX - 1)
    assert p.exit_code is None and p._output_state == "recovering"
    _ticks(p, 1)
    assert p.renderer.recover_calls == sendspin_player.OUTPUT_REOPEN_MAX
    assert p.exit_code == sendspin_player.EXIT_OUTPUT_LOST and p.exit_requested.is_set()
    state = json.loads(Path(p._state_file).read_text())
    assert state["player_health"] == "output_lost"
    assert state["health"]["output"]["state"] == "lost"
    assert state["health"]["output"]["failed_reopens"] == sendspin_player.OUTPUT_REOPEN_MAX


def test_a_recovery_resets_the_failure_count(tmp_path):
    p = _OutputPlayer(tmp_path, _WatchedRenderer(recover_results=[False, False, True]))
    p.renderer.fault = "no output stream is open"
    _ticks(p, 3)
    assert p.exit_code is None and p._output_failed_reopens == 0 and p._output_state == "ok"


def test_an_output_switch_in_flight_is_not_a_stall(tmp_path):
    p = _OutputPlayer(tmp_path, _WatchedRenderer())
    _ticks(p, 1)
    p.renderer.reopening = True
    _ticks(p, 5, advance=False)
    p.renderer.reopening = False
    _ticks(p, 1, advance=False)
    assert p.renderer.recover_calls == 0, "the count starts again after the switch"
