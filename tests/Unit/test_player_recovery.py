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


def test_the_player_uses_the_shared_spawn_helper():
    """One helper holds the reference and logs the failure. The player had a copy of it."""
    import background_tasks

    assert sendspin_player.spawn is background_tasks.spawn
    assert not hasattr(sendspin_player, "_spawn") and not hasattr(sendspin_player, "_BACKGROUND_TASKS")


def test_a_player_task_that_dies_is_logged(caplog):
    """A bare ensure_future drops the exception until the task is collected, if it ever is."""
    import background_tasks

    async def scenario():
        async def boom():
            raise RuntimeError("watcher fell over")

        sendspin_player.spawn(boom(), "output watch")
        assert background_tasks.pending() == 1, "a reference must be held or the task can be collected"
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert background_tasks.pending() == 0

    with caplog.at_level(logging.ERROR, logger="plum.background_tasks"):
        asyncio.run(scenario())
    assert any("output watch" in r.getMessage() and "watcher fell over" in str(r.exc_info[1]) for r in caplog.records)


def test_the_last_playing_server_is_saved_through_a_coroutine(tmp_path):
    """`spawn` takes a coroutine. The save used to hand it an executor future."""
    import json

    class P:
        _state_file = str(tmp_path / "player_state.json")
        _save_last_playing_server = sendspin_player.SendspinPlayer._save_last_playing_server

    async def scenario():
        await sendspin_player.spawn(P()._save_last_playing_server("server-b"), "last playing server save")

    asyncio.run(scenario())
    assert json.loads((tmp_path / "player_state.json").read_text())["last_playing_server_id"] == "server-b"


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


def test_a_reopen_that_does_not_hold_backs_off_and_never_exits(tmp_path, monkeypatch):
    """An output that opens and then stalls again (HDMI with no sink is the likely one).

    Re-opening it every few seconds forever is a PortAudio re-initialisation loop, and exiting over
    it only moves the loop into supervisord. So: a doubling delay, a state that stays 'recovering',
    and no exit — the exit is for a card that will not OPEN.
    """
    clock = [1000.0]
    monkeypatch.setattr(sendspin_player.time, "monotonic", lambda: clock[0])
    p = _OutputPlayer(tmp_path, _WatchedRenderer(recover_results=[True] * 50))

    async def run(polls):
        for _ in range(polls):
            clock[0] += sendspin_player.HEALTH_POLL_S
            await p._check_output()  # the counter never moves: every re-open stalls again

    asyncio.run(run(400))  # 20 minutes
    assert p.exit_code is None
    assert p._output_state == "recovering"
    assert 5 <= p.renderer.recover_calls <= 12, "bounded: not one re-open every 9 s for 20 minutes"


def test_a_reopen_that_holds_ends_the_backoff(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(sendspin_player.time, "monotonic", lambda: clock[0])
    p = _OutputPlayer(tmp_path, _WatchedRenderer(recover_results=[True] * 50))

    async def run(polls, *, advance):
        for _ in range(polls):
            clock[0] += sendspin_player.HEALTH_POLL_S
            if advance:
                p.renderer.callbacks += 300
            await p._check_output()

    asyncio.run(run(40, advance=False))
    assert p._output_state == "recovering" and p._output_unproven >= 2
    asyncio.run(run(3, advance=True))
    assert p._output_state == "ok" and p._output_unproven == 0


def test_the_output_watchdog_has_a_kill_switch(tmp_path, monkeypatch):
    monkeypatch.setattr(sendspin_player, "OUTPUT_WATCHDOG", False)
    p = _OutputPlayer(tmp_path, _WatchedRenderer())
    p.renderer.fault = "no output stream is open"
    _ticks(p, 10)
    assert p.renderer.recover_calls == 0 and p.exit_code is None


def test_an_output_switch_in_flight_is_not_a_stall(tmp_path):
    p = _OutputPlayer(tmp_path, _WatchedRenderer())
    _ticks(p, 1)
    p.renderer.reopening = True
    _ticks(p, 5, advance=False)
    p.renderer.reopening = False
    _ticks(p, 1, advance=False)
    assert p.renderer.recover_calls == 0, "the count starts again after the switch"


# -- dead-peer watchdog ----------------------------------------------------------------------------


class _Msg:
    def __init__(self, kind: str):
        self.type = type("T", (), {"name": kind})()


class _FakeWs:
    """An inbound websocket whose peer answers only when the test says so."""

    def __init__(self, *, close_ends_session: bool = True):
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list = []
        self.closed = False
        self.close_args: dict | None = None
        self.aborted = False
        self._close_ends_session = close_ends_session
        self._req = type("Req", (), {"remote": "10.0.0.9", "transport": self})()

    async def receive(self):
        return await self.inbox.get()

    async def send_bytes(self, data):
        self.sent.append(data)

    async def send_str(self, data):
        self.sent.append(data)

    async def close(self, **kwargs):
        self.close_args = kwargs
        self.closed = True
        if self._close_ends_session:
            self.inbox.put_nowait(_Msg("CLOSING"))
        return True

    def abort(self):  # the transport
        self.aborted = True
        self.inbox.put_nowait(_Msg("CLOSED"))


def test_the_watched_websocket_forwards_everything_the_library_uses():
    async def scenario():
        ws = _FakeWs()
        watched = sendspin_player._WatchedWebSocket(ws)
        await watched.send_str("hello")
        await watched.send_bytes(b"\x01")
        ws.inbox.put_nowait(_Msg("BINARY"))
        assert (await watched.receive()).type.name == "BINARY"
        assert watched.closed is False
        await watched.close()
        return ws, watched

    ws, watched = asyncio.run(scenario())
    assert ws.sent == ["hello", b"\x01"] and ws.closed and watched.closed is True
    assert watched.rx_frames == 1 and watched.tx_frames == 2


def _watched(*, unanswered: int, first_tx_ago: float, last_rx_ago: float):
    watched = sendspin_player._WatchedWebSocket(_FakeWs())
    now = 1000.0
    watched.opened = now - 600
    watched.last_rx = now - last_rx_ago
    watched.unanswered_tx = unanswered
    watched.first_unanswered_tx = (now - first_tx_ago) if unanswered else None
    return watched, now


def test_a_parked_mute_session_is_not_silent():
    """THE false positive to avoid. A parked server sends no audio — and still answers time sync."""

    async def scenario():
        ws = _FakeWs()
        watched = sendspin_player._WatchedWebSocket(ws)
        for _ in range(40):  # two minutes of idle time sync at the 3 s cadence
            await watched.send_bytes(b"client/time")
            ws.inbox.put_nowait(_Msg("BINARY"))
            await watched.receive()
        return watched

    watched = asyncio.run(scenario())
    assert watched.unanswered_tx == 0 and watched.first_unanswered_tx is None
    assert watched.silence(watched.last_rx + 3600, 30.0) is None, "nothing unanswered, however long ago"


def test_unanswered_frames_past_the_threshold_are_silence():
    watched, now = _watched(unanswered=12, first_tx_ago=36.0, last_rx_ago=36.2)
    evidence = watched.silence(now, 30.0)
    assert evidence is not None
    assert evidence["unanswered_tx"] == 12 and round(evidence["silent_for"]) == 36
    assert evidence["remote"] == "10.0.0.9"


def test_silence_below_the_threshold_is_not_reported():
    watched, now = _watched(unanswered=9, first_tx_ago=27.0, last_rx_ago=27.2)
    assert watched.silence(now, 30.0) is None


def test_a_pairing_wait_is_not_silence():
    """A pairing window waits up to 300 s for an operator with ONE frame outstanding."""
    watched, now = _watched(unanswered=1, first_tx_ago=280.0, last_rx_ago=281.0)
    assert watched.silence(now, 30.0) is None


def test_a_reply_ends_the_silence():
    async def scenario():
        ws = _FakeWs()
        watched = sendspin_player._WatchedWebSocket(ws)
        for _ in range(20):
            await watched.send_bytes(b"client/time")
        watched.first_unanswered_tx -= 60
        watched.last_rx -= 60
        assert watched.silence(watched.last_rx + 60, 30.0) is not None
        ws.inbox.put_nowait(_Msg("BINARY"))
        await watched.receive()
        return watched

    watched = asyncio.run(scenario())
    assert watched.silence(watched.last_rx + 1, 30.0) is None


def test_a_close_frame_is_not_inbound_traffic():
    async def scenario():
        ws = _FakeWs()
        watched = sendspin_player._WatchedWebSocket(ws)
        before = watched.last_rx
        await watched.send_bytes(b"x")
        ws.inbox.put_nowait(_Msg("CLOSING"))
        await watched.receive()
        return watched, before

    watched, before = asyncio.run(scenario())
    assert watched.rx_frames == 0 and watched.last_rx == before and watched.unanswered_tx == 1


def test_the_watchdog_can_be_disabled():
    watched, now = _watched(unanswered=50, first_tx_ago=900.0, last_rx_ago=900.0)
    assert watched.silence(now, 0.0) is None


class _PeerClient:
    connected = True


class _PeerPlayer:
    def __init__(self):
        self.client = _PeerClient()
        self._audio_flowing = False
        self._last_audio_mono = 0.0
        self._init_recovery_state()

    for _name in (
        "_init_recovery_state",
        "request_exit",
        "_check_peer_silence",
        "_close_silent_session",
        "_peer_close_overdue",
    ):
        locals()[_name] = getattr(sendspin_player.SendspinPlayer, _name)
    del _name

    def hold(self, session: int, ws: _FakeWs, *, silent: bool):
        watched = sendspin_player._WatchedWebSocket(ws)
        if silent:
            watched.unanswered_tx = 15
            watched.first_unanswered_tx = watched.opened - 45
            watched.last_rx = watched.opened - 45
        self._session_ws[session] = watched
        # on_connection in miniature: the session ends when the reader sees the websocket close.
        self._attached = getattr(self, "_attached", [])
        self._attached.append(asyncio.ensure_future(self._serve(session, watched)))
        return watched

    async def _serve(self, session: int, watched) -> None:
        while (await watched.receive()).type.name not in ("CLOSING", "CLOSED", "CLOSE"):
            pass
        self._session_ws.pop(session, None)
        self._silence_closing.pop(session, None)


def test_a_silent_session_is_closed_and_logged_once(caplog):
    async def scenario():
        p = _PeerPlayer()
        dead, live = _FakeWs(), _FakeWs()
        p.hold(306, dead, silent=True)
        p.hold(307, live, silent=False)
        p._check_peer_silence()
        await asyncio.sleep(0.01)
        p._check_peer_silence()  # the next poll must not close or log it again
        # Wait for the detach, with a limit. A fixed 0.1 s was two poll periods of the close path
        # and failed on a loaded CI runner.
        for _ in range(500):
            if 306 not in p._session_ws:
                break
            await asyncio.sleep(0.01)
        assert 306 not in p._session_ws and 307 in p._session_ws
        return p, dead, live

    with caplog.at_level(logging.WARNING, logger=LOG):
        p, dead, live = asyncio.run(scenario())
    assert dead.closed and dead.close_args["code"] == 1001
    assert not live.closed, "an overlapping live session is left alone"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "session 306" in warnings[0] and "15 frames we sent went unanswered" in warnings[0]
    assert "10.0.0.9" in warnings[0]
    assert p.exit_code is None and p._peer_silence_closes == 1


def test_no_session_is_closed_when_the_timeout_is_zero(monkeypatch):
    monkeypatch.setattr(sendspin_player, "PEER_SILENCE_TIMEOUT_S", 0.0)

    async def scenario():
        p = _PeerPlayer()
        dead = _FakeWs()
        p.hold(1, dead, silent=True)
        p._check_peer_silence()
        await asyncio.sleep(0.01)
        return dead

    assert not asyncio.run(scenario()).closed


def test_a_session_that_survives_the_close_gets_its_transport_aborted(monkeypatch):
    async def scenario():
        p = _PeerPlayer()
        stuck = _FakeWs(close_ends_session=False)
        watched = p.hold(5, stuck, silent=True)
        await p._close_silent_session(5, watched)
        await asyncio.sleep(0.01)
        return p, stuck

    monkeypatch.setattr(sendspin_player, "PEER_DETACH_WAIT_S", 0.1)
    p, stuck = asyncio.run(scenario())
    assert stuck.aborted
    assert 5 not in p._session_ws, "the abort is what ends the session"


def test_the_process_exits_when_a_closed_session_stays_attached(monkeypatch):
    async def scenario():
        p = _PeerPlayer()
        p.hold(5, _FakeWs(close_ends_session=False), silent=True)
        p._check_peer_silence()
        await asyncio.sleep(0.01)
        assert p.exit_code is None, "the close gets its grace period first"
        p._silence_closing[5] -= sendspin_player.PEER_CLOSE_GRACE_S + 1
        p._check_peer_silence()
        return p

    p = asyncio.run(scenario())
    assert p.exit_code == sendspin_player.EXIT_PEER_WEDGED and p.exit_requested.is_set()


def test_the_process_does_not_exit_while_audio_is_flowing(caplog):
    """Another session may be playing. A restart there turns a hidden fault into a dropout."""
    import time as real_time

    async def scenario():
        p = _PeerPlayer()
        p.hold(5, _FakeWs(close_ends_session=False), silent=True)
        p._check_peer_silence()
        await asyncio.sleep(0.01)
        p._silence_closing[5] -= sendspin_player.PEER_CLOSE_GRACE_S + 1
        p._last_audio_mono = real_time.monotonic()  # a chunk just arrived
        p._check_peer_silence()
        p._check_peer_silence()
        assert p.exit_code is None
        p._last_audio_mono = 0.0  # the audio stopped
        p._check_peer_silence()
        return p

    with caplog.at_level(logging.WARNING, logger=LOG):
        p = asyncio.run(scenario())
    assert len([r for r in caplog.records if "audio is flowing" in r.getMessage()]) == 1
    assert p.exit_code == sendspin_player.EXIT_PEER_WEDGED


def test_closing_a_real_aiohttp_websocket_wakes_a_blocked_reader():
    """The claim the whole watchdog rests on, against the real transport rather than a fake.

    aiosendspin's reader sits in `receive()` on the inbound WebSocketResponse for the life of the
    session. Closing that websocket from ANOTHER task must wake the reader, or the session cannot
    be ended from our side at all. The peer here connects and then never reads, sends or closes.
    """
    aiohttp = pytest.importorskip("aiohttp")
    from aiohttp import web

    async def scenario():
        result: dict = {}
        blocked = asyncio.Event()

        async def handler(request):
            ws = web.WebSocketResponse()  # no heartbeat — exactly as aiosendspin's listener builds it
            await ws.prepare(request)
            watched = sendspin_player._WatchedWebSocket(ws)
            result["watched"] = watched
            blocked.set()
            result["msg"] = await watched.receive()  # the library's reader loop, in miniature
            return ws

        app = web.Application()
        app.router.add_get("/sendspin", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        async with aiohttp.ClientSession() as session:
            peer = await session.ws_connect(f"http://127.0.0.1:{port}/sendspin", autoclose=False, autoping=False)
            await asyncio.wait_for(blocked.wait(), 2)
            await asyncio.sleep(0.05)
            closer = asyncio.ensure_future(result["watched"].close(code=1001, message=b"peer silent"))
            for _ in range(100):
                if "msg" in result:
                    break
                await asyncio.sleep(0.01)
            woke = result.get("msg")
            result["watched"].abort()
            closer.cancel()
            await peer.close()
        await runner.cleanup()
        return woke

    woke = asyncio.run(scenario())
    assert woke is not None, "the reader stayed blocked: the session cannot be closed from our side"
    assert woke.type.name in ("CLOSING", "CLOSED", "CLOSE")


# -- a re-open that never returns --------------------------------------------------------------------


class _BlockingRenderer(_WatchedRenderer):
    """recover() blocks as PortAudio does on an unplugged USB DAC, until the test lets it go."""

    def __init__(self):
        super().__init__()
        import threading

        self.release = threading.Event()

    def recover(self) -> bool:
        self.recover_calls += 1
        self.reopening = True
        self.release.wait(10)
        return False


def test_a_recover_that_blocks_ends_in_an_exit_request(tmp_path, monkeypatch):
    """The health loop must not wait on PortAudio for ever: the other watchdogs live in it."""
    import json

    monkeypatch.setattr(sendspin_player, "OUTPUT_RECOVER_TIMEOUT_S", 0.05)
    p = _OutputPlayer(tmp_path, _BlockingRenderer())
    p.renderer.fault = "PortAudio finished the stream"

    async def scenario():
        try:
            await asyncio.wait_for(p._check_output(), 5)
            await asyncio.wait_for(p._check_output(), 5)  # the stuck re-open is not started again
        finally:
            p.renderer.release.set()  # let asyncio.run() join the worker thread

    asyncio.run(scenario())
    assert p.renderer.recover_calls == 1
    assert p.exit_code == sendspin_player.EXIT_OUTPUT_LOST and p.exit_requested.is_set()
    assert json.loads(Path(p._state_file).read_text())["health"]["output"]["state"] == "lost"


def test_renderer_stop_gives_up_on_a_lock_that_a_stuck_worker_holds(fake_sd, monkeypatch, caplog):
    """SIGTERM must reach the end of stop() before supervisord sends SIGKILL."""
    import threading

    monkeypatch.setattr(sendspin_player, "RENDERER_STOP_LOCK_TIMEOUT_S", 0.05)
    renderer = _renderer()
    held, release = threading.Event(), threading.Event()

    def worker():
        with renderer._reopen_lock:
            held.set()
            release.wait(10)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    assert held.wait(5)
    try:
        with caplog.at_level(logging.ERROR, logger=LOG):
            assert renderer.stop() is False
    finally:
        release.set()
        thread.join(5)
    assert any("did not release" in r.getMessage() for r in caplog.records)
    assert renderer.stop() is True, "a free lock still releases the stream"
    assert renderer._stream is None


def test_stop_records_a_dac_that_was_not_released(tmp_path):
    p = _StopPlayer(tmp_path)
    p.renderer.stop = lambda: False
    asyncio.run(p.stop())
    assert p.renderer_released is False
    q = _StopPlayer(tmp_path)
    asyncio.run(q.stop())
    assert q.renderer_released is True


def test_a_stuck_worker_thread_ends_in_a_hard_exit(monkeypatch):
    """asyncio.run() joins the executor threads. A thread inside PortAudio never returns."""
    exits = []
    monkeypatch.setattr(sendspin_player.os, "_exit", exits.append)

    class P:
        renderer_released = True
        exit_code = None

    sendspin_player._hard_exit_if_stuck(P)
    assert exits == [], "a clean stop returns through asyncio.run()"
    P.renderer_released = False
    sendspin_player._hard_exit_if_stuck(P)
    P.exit_code = sendspin_player.EXIT_OUTPUT_LOST
    sendspin_player._hard_exit_if_stuck(P)
    assert exits == [0, sendspin_player.EXIT_OUTPUT_LOST]


# -- the first health report -------------------------------------------------------------------------


class _ReportingPlayer(_OutputPlayer):
    _refresh_health_report = sendspin_player.SendspinPlayer._refresh_health_report
    _report_startup_health = getattr(sendspin_player.SendspinPlayer, "_report_startup_health", None)


def test_a_new_player_clears_the_lost_report_of_the_old_process(tmp_path):
    """The old process wrote `lost` and left. The healthcheck must not read that from the new one."""
    import json

    old = _ReportingPlayer(tmp_path, _WatchedRenderer())
    old._set_output_state("lost", "device removed")
    assert json.loads(Path(old._state_file).read_text())["health"]["output"]["state"] == "lost"

    new = _ReportingPlayer(tmp_path, _WatchedRenderer())
    new._report_startup_health()
    state = json.loads(Path(new._state_file).read_text())
    assert state["health"]["output"] == {
        "state": "ok",
        "fault": None,
        "device": "hifiberry:0",
        "reopens": 0,
        "failed_reopens": 0,
    }
    assert state["player_health"] == "synchronized"


def test_start_writes_the_first_report_after_the_output_opens():
    import inspect

    source = inspect.getsource(sendspin_player.SendspinPlayer.start)
    assert "self._report_startup_health()" in source
    assert source.index("self.renderer.start()") < source.index("self._report_startup_health()")


def test_the_first_periodic_report_does_not_wait_on_a_young_monotonic_clock(tmp_path, monkeypatch):
    """`0.0` as "never" skipped the first report on a host that booted under 30 s ago."""
    p = _ReportingPlayer(tmp_path, _WatchedRenderer())
    monkeypatch.setattr(sendspin_player.time, "monotonic", lambda: 5.0)
    asyncio.run(p._refresh_health_report())
    assert Path(p._state_file).exists()
