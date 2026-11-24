"""Unit tests for BluetoothAdapter device tracking: one capture loop, whatever order signals land in.

`_on_device_props` and `_drop_device` each run as their own task, one for every D-Bus signal, and
both end in `_start_capture` / `_stop_capture`, which await. With no lock, two signals back to back
could each start a `_capture_loop`. The second assignment to `_capture_task` then orphaned the
first loop — and its `arecord`, which holds the bluealsa PCM, so the capture that was kept could
never open it.

No BlueZ and no arecord: the D-Bus reads and the capture loop are replaced. Run: `pytest tests/Unit`.
"""

import asyncio
import contextlib
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend" / "scripts"))

from sources.bluetooth_adapter import BluetoothAdapter  # noqa: E402

ADAPTER_PATH = "/org/bluez/hci0"
PHONE_A = ADAPTER_PATH + "/dev_AA_AA_AA_AA_AA_AA"
PHONE_B = ADAPTER_PATH + "/dev_BB_BB_BB_BB_BB_BB"


class _Props:
    """Stands in for a device's org.freedesktop.DBus.Properties proxy."""

    def __init__(self, connected=True):
        self.connected = connected

    async def call_get(self, _iface, name):
        await asyncio.sleep(0)  # a D-Bus round trip: the point where another signal's task runs
        assert name == "Connected"
        return SimpleNamespace(value=self.connected)


def make_adapter():
    instance = SimpleNamespace(source_id="bluetooth-1", adapter_path=ADAPTER_PATH, fifo_path="/tmp/x")
    adapter = BluetoothAdapter(instance)
    adapter.loops = []  # every capture loop that was ever started: (address, task)
    adapter.device_props = {}  # path -> _Props

    async def props_iface(path):
        await asyncio.sleep(0)
        return adapter.device_props.get(path)

    async def is_audio_source(_props):
        await asyncio.sleep(0)
        return True

    async def address_of(_props, path):
        # The real one reads `Address` over D-Bus, BETWEEN the "already adopted?" check and the
        # write to `_connected`. That await is what lets two signals both pass the check.
        await asyncio.sleep(0)
        return path.rsplit("dev_", 1)[1].replace("_", ":")

    async def persist_pairing(_path, _props):
        await asyncio.sleep(0)

    async def judge_connection(_path, _address, _connected_at):
        await asyncio.sleep(0)

    async def capture_loop(address):
        adapter.loops.append((address, asyncio.current_task()))
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await asyncio.sleep(0.02)  # killing arecord takes time; this is where the race opens
            raise

    adapter._props_iface = props_iface
    adapter._is_audio_source = is_audio_source
    adapter._address_of = address_of
    adapter._persist_pairing = persist_pairing
    adapter._judge_connection = judge_connection
    adapter._capture_loop = capture_loop
    return adapter


def live_loops(adapter):
    return [(address, task) for address, task in adapter.loops if not task.done()]


async def cleanup(adapter):
    for _address, task in adapter.loops:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def test_two_signals_for_a_second_phone_start_one_capture_loop_not_two():
    """Retargeting from phone A: both tasks used to pass through _stop_capture and each start a loop."""

    async def scenario():
        adapter = make_adapter()
        adapter.device_props = {PHONE_A: _Props(), PHONE_B: _Props()}
        await adapter._on_device_props(PHONE_A, {})
        await asyncio.sleep(0)  # let the capture loop task start
        assert [a for a, _ in live_loops(adapter)] == ["AA:AA:AA:AA:AA:AA"]

        # PropertiesChanged(Connected) and PropertiesChanged(UUIDs) arrive back to back.
        await asyncio.gather(adapter._on_device_props(PHONE_B, {}), adapter._on_device_props(PHONE_B, {}))
        await asyncio.sleep(0.05)

        live = live_loops(adapter)
        assert [a for a, _ in live] == ["BB:BB:BB:BB:BB:BB"]  # exactly one, and it is phone B
        assert live[0][1] is adapter._capture_task  # the one we can still stop: no orphan
        await cleanup(adapter)

    asyncio.run(scenario())


def test_a_disconnect_racing_a_connect_leaves_one_tracked_loop():
    async def scenario():
        adapter = make_adapter()
        adapter.device_props = {PHONE_A: _Props(), PHONE_B: _Props()}
        await adapter._on_device_props(PHONE_A, {})
        await asyncio.sleep(0)

        adapter.device_props[PHONE_A] = _Props(connected=False)
        await asyncio.gather(
            adapter._drop_device(PHONE_A),
            adapter._on_device_props(PHONE_B, {}),
            adapter._on_device_props(PHONE_A, {}),
        )
        await asyncio.sleep(0.05)

        live = live_loops(adapter)
        assert len(live) == 1
        assert live[0][0] == "BB:BB:BB:BB:BB:BB"
        assert live[0][1] is adapter._capture_task
        assert adapter.active_address == "BB:BB:BB:BB:BB:BB"
        await cleanup(adapter)

    asyncio.run(scenario())


def test_the_last_phone_leaving_stops_capture():
    async def scenario():
        adapter = make_adapter()
        adapter.device_props = {PHONE_A: _Props()}
        await adapter._on_device_props(PHONE_A, {})
        await adapter._drop_device(PHONE_A)
        await asyncio.sleep(0.05)
        assert live_loops(adapter) == []
        assert adapter.active_address is None

    asyncio.run(scenario())
