#!/usr/bin/env python3
"""
Plum-Audio — paired Bluetooth devices, read and forgotten over BlueZ's D-Bus API.

The GUI needs this because a bond can go stale on the phone's side alone: the user taps "Forget This
Device" there, the unit keeps its link key, and every later connection then fails authentication
(`br-connection-key-missing`) — the phone reports "pairing unsuccessful" while the unit still lists
it as paired. Recovering that used to need `bluetoothctl remove` over SSH, which is not a thing a
shipped unit can ask of anyone. `sources/bluetooth_adapter.py` now also drops such a bond on its
own after repeated failed connections; this is the manual door for every case that heuristic should
not touch (a phone the user simply wants gone, a device that is failing for some other reason).

WHY THIS TALKS TO BLUEZ DIRECTLY, from the Flask process: bonds are BlueZ state, not our state —
there is nothing in the audio loop to consult, and RemoveDevice is idempotent and instantaneous.
That is different in kind from the endpoint CRUD in integrations_api.py, which is pure persistence
precisely because applying it needs the audio loop. Removing a bond that is currently connected is
safe: BlueZ drops the link, and the adapter's existing disconnect path tears the capture down.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

BLUEZ = "org.bluez"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"
OBJECT_MANAGER_IFACE = "org.freedesktop.DBus.ObjectManager"

DBUS_TIMEOUT_S = 10


class BlueZUnavailable(RuntimeError):
    """BlueZ could not be reached — no system bus, or bluetoothd is not running on the host."""


async def _managed_objects():
    from dbus_next.aio import MessageBus
    from dbus_next.constants import BusType

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        intro = await bus.introspect(BLUEZ, "/")
        manager = bus.get_proxy_object(BLUEZ, "/", intro).get_interface(OBJECT_MANAGER_IFACE)
        return bus, await manager.call_get_managed_objects()
    except Exception:
        bus.disconnect()
        raise


def _unwrap(props: dict, key, default=None):
    """dbus-next hands back Variants; callers want the plain value."""
    value = props.get(key)
    return getattr(value, "value", default if value is None else value)


async def _list_devices() -> list[dict]:
    bus, objects = await _managed_objects()
    try:
        devices = []
        for path, interfaces in objects.items():
            props = interfaces.get(DEVICE_IFACE)
            if not props or not _unwrap(props, "Paired", False):
                continue
            address = _unwrap(props, "Address", "")
            devices.append(
                {
                    "address": address,
                    # Alias is what BlueZ shows and what the user renamed it to, if they ever did;
                    # Name is the device's own. Either can be missing on a bond restored from disk.
                    "name": _unwrap(props, "Alias") or _unwrap(props, "Name") or address,
                    "connected": bool(_unwrap(props, "Connected", False)),
                    "trusted": bool(_unwrap(props, "Trusted", False)),
                    "adapter": str(_unwrap(props, "Adapter", "") or ""),
                    "path": path,
                }
            )
        devices.sort(key=lambda d: (not d["connected"], d["name"].lower()))
        return devices
    finally:
        bus.disconnect()


async def _remove_device(address: str) -> tuple[bool, str]:
    wanted = address.strip().upper()
    bus, objects = await _managed_objects()
    try:
        for path, interfaces in objects.items():
            props = interfaces.get(DEVICE_IFACE)
            if not props or str(_unwrap(props, "Address", "")).upper() != wanted:
                continue
            adapter_path = str(_unwrap(props, "Adapter", "") or "")
            if not adapter_path:
                return False, "device has no adapter"
            intro = await bus.introspect(BLUEZ, adapter_path)
            adapter = bus.get_proxy_object(BLUEZ, adapter_path, intro).get_interface(ADAPTER_IFACE)
            # RemoveDevice both drops the link key from /var/lib/bluetooth and disconnects it if it
            # is currently connected, which is exactly what "Forget" has to mean for a stale bond.
            await adapter.call_remove_device(path)
            logger.info("removed Bluetooth bond %s (%s)", wanted, path)
            return True, "removed"
        # Already gone. Report success: the caller asked for it to not be paired, and it is not.
        return True, "not paired"
    finally:
        bus.disconnect()


def list_paired_devices() -> list[dict]:
    """Every paired device BlueZ knows about, across adapters. Raises BlueZUnavailable."""
    try:
        return asyncio.run(asyncio.wait_for(_list_devices(), DBUS_TIMEOUT_S))
    except Exception as e:  # noqa: BLE001
        logger.warning("listing Bluetooth bonds failed: %s", e)
        raise BlueZUnavailable(str(e)) from e


def forget_device(address: str) -> tuple[bool, str]:
    """Drop one device's bond (link key included). Returns (ok, message)."""
    try:
        return asyncio.run(asyncio.wait_for(_remove_device(address), DBUS_TIMEOUT_S))
    except Exception as e:  # noqa: BLE001
        logger.warning("removing Bluetooth bond %s failed: %s", address, e)
        raise BlueZUnavailable(str(e)) from e
