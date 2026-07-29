#!/usr/bin/env python3
"""
Plum-Audio — BlueZ adapter control, auto-pairing, device tracking, and A2DP capture.

This is the Bluetooth equivalent of "the daemon", except most of it isn't a daemon at all: BlueZ is
already running on the host, so we drive it over the SYSTEM D-Bus with async dbus-next from inside
the audio event loop — exactly the arrangement mesh/avahi.py uses for Avahi, and for the same
reason (the host owns the radio; a second bluetoothd would fight it for hci0).

What this replaces from Plum-Snapcast, and why none of it is ported as-was:
  bluetooth-init.sh          — bluetoothctl heredocs + gdbus fallbacks → Adapter1 property writes
  bluetooth-auto-pair-agent  — dbus-python + a GLib main loop in a thread → an exported ServiceInterface
  bluetooth-connection-monitor — a 3 s `bluetoothctl devices Connected` poll → PropertiesChanged signals
  bluetooth-fifo-keeper.sh   — gone entirely; SourceFeeder opens the FIFO O_RDONLY|O_NONBLOCK

CAPTURE. `arecord` is deliberately NOT a DaemonSpec: DaemonSpecs are per-endpoint and static, but
capture is per-CONNECTED-DEVICE. So we spawn it when an A2DP device connects and kill it when the
last one leaves — which is also what gives us the spec's idle contract for free: killing arecord
closes the FIFO's only writer, the feeder sees EOF, and it announces playback_state=stopped. No
lifecycle manager, no signal files, no stream add/remove (the 932 lines that did this on Snapcast).

Two details worth knowing before editing:
  * `Powered = true` FAILS with a bare "Failed" while the adapter is rfkill soft-blocked, and BlueZ
    will NOT clear the block itself. That unblock is host provisioning (see docs) — deliberately not
    something we attempt, since it would need /dev/rfkill + CAP_NET_ADMIN in the container.
  * We ALWAYS call Pair() on a connecting A2DP device even when it reports Paired=true. iOS
    "Just Works" SSP creates an in-memory pairing that never reaches disk, so without this the phone
    silently stops reconnecting after a reboot. AlreadyExists is the success case, not an error.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import time
from collections.abc import Callable

from dbus_next import BusType, Message, MessageType, Variant
from dbus_next.aio import MessageBus

from sources import bluetooth_config
from sources.bluetooth_agent import AutoPairAgent

logger = logging.getLogger("plum.bluetooth_adapter")

BLUEZ = "org.bluez"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"
AGENT_IFACE = "org.bluez.Agent1"
AGENT_MANAGER_IFACE = "org.bluez.AgentManager1"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
OBJECT_MANAGER_IFACE = "org.freedesktop.DBus.ObjectManager"

# A2DP Audio Source: what a PHONE advertises when it wants to send us audio. (The Sink UUID is what
# a speaker advertises; Plum-Snapcast matched both, but we are the sink — a device offering us
# only 110d has nothing to send.)
A2DP_SOURCE_UUID = "0000110a-0000-1000-8000-00805f9b34fb"
A2DP_SINK_UUID = "0000110d-0000-1000-8000-00805f9b34fb"

CAPTURE_RESTART_DELAY_S = 1.0  # let BlueZ finish setting the transport up before arecord opens it
CAPTURE_RETRY_S = 1.0  # backoff step after a failed/exited capture (see _capture_loop)
CAPTURE_RETRY_MAX_S = 15.0
CAPTURE_HEALTHY_S = 5.0  # a capture that ran this long counts as a real session, not a failure
CAPTURE_RENEGOTIATE_AFTER = 3  # immediate failures before we ask BlueZ to re-connect A2DP
BLUEALSA_NAME = "org.bluealsa"
BLUEALSA_WAIT_S = 20.0  # how long to wait for the daemon before falling back to plain retries


class BluetoothAdapter:
    """Owns one adapter: its BlueZ config, the pairing agent, device tracking, and A2DP capture.

    Also owns the single system-bus connection for this endpoint and re-broadcasts BlueZ's
    PropertiesChanged / InterfacesAdded / InterfacesRemoved to listeners, so bluetooth_avrcp.py can
    watch MediaPlayer1 and MediaTransport1 without opening a second bus and a second match rule.
    """

    def __init__(self, instance: bluetooth_config.BluetoothInstance) -> None:
        self.instance = instance
        self._bus: MessageBus | None = None
        self._agent: AutoPairAgent | None = None
        self._connected: dict[str, str] = {}  # device object path -> MAC address
        self._active: str | None = None  # address currently being captured
        self._capture: asyncio.subprocess.Process | None = None
        self._capture_task: asyncio.Task | None = None
        self._capture_log = None
        self._props_listeners: list[Callable[[str, str, dict], None]] = []
        self._object_listeners: list[Callable[[str, list[str], bool], None]] = []

    # -- listener registration (bluetooth_avrcp hooks in here) ----------------

    def add_props_listener(self, cb: Callable[[str, str, dict], None]) -> None:
        """cb(object_path, interface, changed_properties) for every BlueZ PropertiesChanged."""
        self._props_listeners.append(cb)

    def add_object_listener(self, cb: Callable[[str, list[str], bool], None]) -> None:
        """cb(object_path, interfaces, present) as BlueZ objects come and go."""
        self._object_listeners.append(cb)

    @property
    def bus(self) -> MessageBus | None:
        return self._bus

    @property
    def active_address(self) -> str | None:
        """The device we are currently capturing from, if any."""
        return self._active

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        logger.info("[%s] connected to system D-Bus", self.instance.source_id)
        await self._subscribe()
        await self.apply_settings(self.instance)
        await self._register_agent()
        await self._scan_existing()

    async def stop(self) -> None:
        await self._stop_capture()
        await self._unregister_agent()
        if self._bus is not None:
            with contextlib.suppress(Exception):
                self._bus.disconnect()
            self._bus = None
        self._connected.clear()
        self._active = None

    # -- adapter configuration -----------------------------------------------

    async def apply_settings(self, instance: bluetooth_config.BluetoothInstance) -> None:
        """Push the endpoint's name/discoverable/pairable onto the adapter. Idempotent.

        Powered comes first and everything else depends on it: BlueZ rejects Discoverable on a
        powered-down adapter. A failure here is logged, not raised — the commonest cause is an
        rfkill soft block, which is host provisioning we can't fix from in here, and the source
        should still come up (silent) rather than crash-loop the manager.
        """
        self.instance = instance
        if not await self._set_adapter_prop("Powered", Variant("b", True)):
            logger.error(
                "[%s] could not power on %s — is it rfkill soft-blocked? "
                "BlueZ will not clear that itself; run `rfkill unblock bluetooth` on the host.",
                instance.source_id, instance.adapter,
            )
            return
        await self._set_adapter_prop("Alias", Variant("s", instance.device_name))
        await self._set_adapter_prop("Pairable", Variant("b", True))
        # 0 = never time out. Without this the adapter silently stops advertising after 180 s and
        # the speaker "disappears" from the phone's list — the bug bluetooth-connection-monitor.py
        # existed to paper over with a 3 s poll.
        await self._set_adapter_prop("DiscoverableTimeout", Variant("u", 0))
        await self._set_adapter_prop("Discoverable", Variant("b", instance.discoverable))
        logger.info(
            "[%s] adapter %s configured (alias=%r discoverable=%s)",
            instance.source_id, instance.adapter, instance.device_name, instance.discoverable,
        )

    async def _set_adapter_prop(self, name: str, value: Variant) -> bool:
        props = await self._props_iface(self.instance.adapter_path)
        if props is None:
            return False
        try:
            await props.call_set(ADAPTER_IFACE, name, value)
            return True
        except Exception:  # noqa: BLE001 - adapter missing, blocked, or busy
            logger.debug("[%s] set %s failed", self.instance.source_id, name, exc_info=True)
            return False

    async def _props_iface(self, path: str):
        if self._bus is None:
            return None
        try:
            intro = await self._bus.introspect(BLUEZ, path)
            return self._bus.get_proxy_object(BLUEZ, path, intro).get_interface(PROPS_IFACE)
        except Exception:  # noqa: BLE001 - object vanished between signal and use
            logger.debug("[%s] no properties on %s", self.instance.source_id, path, exc_info=True)
            return None

    # -- pairing agent -------------------------------------------------------

    async def _register_agent(self) -> None:
        if not self.instance.auto_pair or self._bus is None:
            logger.info("[%s] auto-pairing disabled; no agent registered", self.instance.source_id)
            return
        try:
            self._agent = AutoPairAgent(self.instance.instance_id)
            self._bus.export(self.instance.agent_path, self._agent)
            intro = await self._bus.introspect(BLUEZ, "/org/bluez")
            manager = self._bus.get_proxy_object(BLUEZ, "/org/bluez", intro).get_interface(AGENT_MANAGER_IFACE)
            # A leftover registration from a crashed run owns the path and blocks us.
            with contextlib.suppress(Exception):
                await manager.call_unregister_agent(self.instance.agent_path)
            await manager.call_register_agent(self.instance.agent_path, "NoInputNoOutput")
            await manager.call_request_default_agent(self.instance.agent_path)
            logger.info("[%s] auto-pairing agent registered (NoInputNoOutput)", self.instance.source_id)
        except Exception:  # noqa: BLE001 - another agent holds default, or policy denies us
            logger.warning(
                "[%s] could not register the pairing agent; pairing will need bluetoothctl",
                self.instance.source_id, exc_info=True,
            )
            self._agent = None

    async def _unregister_agent(self) -> None:
        if self._agent is None or self._bus is None:
            return
        with contextlib.suppress(Exception):
            intro = await self._bus.introspect(BLUEZ, "/org/bluez")
            manager = self._bus.get_proxy_object(BLUEZ, "/org/bluez", intro).get_interface(AGENT_MANAGER_IFACE)
            await manager.call_unregister_agent(self.instance.agent_path)
        with contextlib.suppress(Exception):
            self._bus.unexport(self.instance.agent_path, self._agent)
        self._agent = None

    # -- BlueZ object / signal plumbing --------------------------------------

    async def _subscribe(self) -> None:
        """Watch BlueZ's whole object tree: one match rule, one handler, every interface.

        A raw match rule rather than per-object proxies because the objects we care about
        (Device1, MediaPlayer1, MediaTransport1) appear and disappear as phones come and go —
        proxying each one as it arrives would race the signals that announce it.
        """
        assert self._bus is not None
        await self._bus.call(
            Message(
                destination="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus",
                member="AddMatch",
                signature="s",
                body=[f"type='signal',sender='{BLUEZ}',interface='{PROPS_IFACE}',member='PropertiesChanged'"],
            )
        )
        self._bus.add_message_handler(self._on_message)

        intro = await self._bus.introspect(BLUEZ, "/")
        obj_manager = self._bus.get_proxy_object(BLUEZ, "/", intro).get_interface(OBJECT_MANAGER_IFACE)
        obj_manager.on_interfaces_added(self._on_interfaces_added)
        obj_manager.on_interfaces_removed(self._on_interfaces_removed)
        self._obj_manager = obj_manager

    def _on_message(self, msg: Message):
        """Raw PropertiesChanged fan-out. Returns None so other handlers still see the message."""
        if msg.message_type is not MessageType.SIGNAL:
            return None
        if msg.interface != PROPS_IFACE or msg.member != "PropertiesChanged":
            return None
        try:
            iface, changed, _invalidated = msg.body
            unwrapped = {k: (v.value if isinstance(v, Variant) else v) for k, v in changed.items()}
        except (ValueError, AttributeError):
            return None
        if iface == DEVICE_IFACE:
            asyncio.ensure_future(self._on_device_props(msg.path, unwrapped))
        for cb in self._props_listeners:
            with contextlib.suppress(Exception):
                cb(msg.path, iface, unwrapped)
        return None

    def _on_interfaces_added(self, path: str, interfaces: dict) -> None:
        names = list(interfaces)
        if DEVICE_IFACE in names:
            asyncio.ensure_future(self._on_device_props(path, {}))
        for cb in self._object_listeners:
            with contextlib.suppress(Exception):
                cb(path, names, True)

    def _on_interfaces_removed(self, path: str, interfaces: list[str]) -> None:
        if DEVICE_IFACE in interfaces and path in self._connected:
            asyncio.ensure_future(self._drop_device(path))
        for cb in self._object_listeners:
            with contextlib.suppress(Exception):
                cb(path, list(interfaces), False)

    async def _scan_existing(self) -> None:
        """Adopt devices already connected when we started (a restart mid-session)."""
        if self._bus is None:
            return
        try:
            objects = await self._obj_manager.call_get_managed_objects()
        except Exception:  # noqa: BLE001 - BlueZ not up yet
            logger.debug("[%s] could not enumerate BlueZ objects", self.instance.source_id, exc_info=True)
            return
        for path, interfaces in objects.items():
            names = list(interfaces)
            if DEVICE_IFACE in names:
                await self._on_device_props(path, {})
            for cb in self._object_listeners:
                with contextlib.suppress(Exception):
                    cb(path, names, True)

    # -- device tracking -----------------------------------------------------

    async def _on_device_props(self, path: str, changed: dict) -> None:
        """Decide whether `path` is an A2DP source we should be capturing, and act on it.

        Called for both PropertiesChanged and object discovery, so it must be idempotent. We
        re-read Connected/UUIDs rather than trusting `changed`: UUIDs often arrive AFTER Connected
        (service discovery finishes late), which is the race Plum-Snapcast handled with a second
        'UUIDs' branch and a "known paired device reconnecting" special case.
        """
        if not path.startswith(self.instance.adapter_path + "/"):
            return  # a device on another adapter
        props = await self._props_iface(path)
        if props is None:
            if path in self._connected:
                await self._drop_device(path)
            return
        try:
            connected = bool((await props.call_get(DEVICE_IFACE, "Connected")).value)
        except Exception:  # noqa: BLE001 - device vanished
            if path in self._connected:
                await self._drop_device(path)
            return

        if not connected:
            if path in self._connected:
                await self._drop_device(path)
            return
        if not await self._is_audio_source(props):
            # Connected but not (yet) an audio source. If we adopted it earlier on a partial UUID
            # list, let it go again.
            if path in self._connected:
                await self._drop_device(path)
            return
        if path in self._connected:
            return  # already adopted; nothing changed that matters

        address = await self._address_of(props, path)
        self._connected[path] = address
        logger.info("[%s] A2DP device connected: %s (%s)", self.instance.source_id, address, path)
        await self._persist_pairing(path, props)
        # Most-recently-connected wins, matching the ALSA plugin's own default device semantics
        # (/etc/alsa/conf.d/20-bluealsa.conf uses 00:00:00:00:00:00 = most recent).
        await self._start_capture(address)

    async def _drop_device(self, path: str) -> None:
        address = self._connected.pop(path, None)
        if address is None:
            return
        logger.info("[%s] A2DP device disconnected: %s", self.instance.source_id, address)
        if self._active != address:
            return
        if self._connected:
            # Another phone is still connected — hand capture over rather than going idle.
            await self._start_capture(next(iter(self._connected.values())))
        else:
            await self._stop_capture()

    async def _is_audio_source(self, props) -> bool:
        try:
            uuids = (await props.call_get(DEVICE_IFACE, "UUIDs")).value or []
        except Exception:  # noqa: BLE001 - UUIDs not populated yet; we'll be called again
            return False
        lowered = {str(u).lower() for u in uuids}
        return A2DP_SOURCE_UUID in lowered or A2DP_SINK_UUID in lowered

    async def _address_of(self, props, path: str) -> str:
        with contextlib.suppress(Exception):
            return str((await props.call_get(DEVICE_IFACE, "Address")).value)
        # Fall back to the object path: /org/bluez/hci0/dev_AA_BB_.. -> AA:BB:..
        tail = path.rsplit("/", 1)[-1]
        return tail[4:].replace("_", ":") if tail.startswith("dev_") else ""

    async def _persist_pairing(self, path: str, props) -> None:
        """Trust the device and force the pairing to disk (see module docstring on iOS SSP)."""
        with contextlib.suppress(Exception):
            if not bool((await props.call_get(DEVICE_IFACE, "Trusted")).value):
                await props.call_set(DEVICE_IFACE, "Trusted", Variant("b", True))
                logger.info("[%s] trusted %s", self.instance.source_id, path)
        try:
            intro = await self._bus.introspect(BLUEZ, path)
            device = self._bus.get_proxy_object(BLUEZ, path, intro).get_interface(DEVICE_IFACE)
            await device.call_pair()
            logger.info("[%s] pairing persisted for %s", self.instance.source_id, path)
        except Exception as e:  # noqa: BLE001
            if "AlreadyExists" in str(e):
                logger.debug("[%s] pairing already on disk for %s", self.instance.source_id, path)
            else:
                logger.debug("[%s] Pair() on %s failed", self.instance.source_id, path, exc_info=True)

    # -- A2DP capture --------------------------------------------------------

    def _capture_argv(self, address: str) -> list[str]:
        """arecord reading one device's A2DP stream as raw PCM into the source FIFO.

        Wrapped in `plug` on purpose: the PCM bluealsa exposes runs at whatever rate the phone
        negotiated for SBC (44.1 kHz almost always, but 48 kHz is legal), while the feeder is fixed
        at DEFAULT_FORMAT 44100/16/2. plug resamples if it has to, so an unusual negotiation
        degrades to a resample instead of arecord failing to open the device. The brace form keeps
        the whole slave definition in ONE argv token, so the commas in DEV=..,PROFILE=.. can't be
        misparsed (there is no shell here to quote for).
        """
        slave = f"bluealsa:DEV={address},PROFILE=a2dp"
        return [
            bluetooth_config.DEFAULT_ARECORD_BIN,
            "-D", "plug:{SLAVE=\"%s\"}" % slave,
            "-f", "cd",          # S16_LE, 44100, stereo — DEFAULT_FORMAT
            "-t", "raw",
            self.instance.fifo_path,
        ]

    async def _start_capture(self, address: str) -> None:
        """Begin (or retarget) supervised capture of `address`."""
        if self._active == address and self._capture_task is not None and not self._capture_task.done():
            return
        await self._stop_capture()
        self._active = address
        self._capture_task = asyncio.ensure_future(self._capture_loop(address))

    async def _capture_loop(self, address: str) -> None:
        """Keep arecord running for as long as this device is the active one.

        Supervised rather than fire-and-forget because arecord legitimately dies in two situations
        we must recover from, and in neither case does anything else notice:

          1. STARTUP ORDER. SourceManagerBase brings the SOURCE up before its DAEMONS (so the feeder
             creates the FIFO first), which means that when a phone is already connected at server
             start, _scan_existing() spawns us milliseconds BEFORE bluealsa owns org.bluealsa.
             arecord then exits immediately with "The name org.bluealsa was not provided by any
             .service files". Observed on hardware: arecord at 21:18:47.827, bluealsa at .842.
          2. DAEMON RESPOOL. Editing the endpoint restarts bluealsa, killing the PCM under us.

        A one-shot spawn leaves the source silently mute in both cases — the phone stays connected
        and everything else looks healthy, so there is no other signal that audio is missing.
        """
        # Close case (1) properly rather than papering over it with retries: the daemon appears
        # within a second, so waiting for the bus name means the first attempt normally succeeds.
        await self._wait_for_bluealsa()
        attempt = 0
        while self._active == address:
            started = time.monotonic()
            proc = await self._spawn_capture(address)
            if proc is None:
                attempt += 1
            else:
                await proc.wait()
                if self._active != address:
                    return  # we were stopped or retargeted; not a failure
                if time.monotonic() - started >= CAPTURE_HEALTHY_S:
                    # A real session that ended (phone stopped sending). Retry promptly.
                    attempt = 0
                    logger.info("[%s] capture for %s ended; reopening", self.instance.source_id, address)
                else:
                    attempt += 1
                    logger.warning(
                        "[%s] capture for %s exited immediately (rc=%s, attempt %d)",
                        self.instance.source_id, address, proc.returncode, attempt,
                    )
                    # Case (2): bluealsa is up but has no PCM for this device, because the A2DP
                    # transport was negotiated against a PREVIOUS daemon instance and died with it.
                    # The phone stays "Connected" at the ACL level, so nothing reconnects on its
                    # own and we would retry forever. Ask BlueZ to re-establish just the A2DP
                    # profile — far less invasive than a full Disconnect(), which many phones do
                    # not auto-recover from.
                    if attempt == CAPTURE_RENEGOTIATE_AFTER:
                        await self._renegotiate_a2dp(address)
            await asyncio.sleep(max(min(CAPTURE_RETRY_S * attempt, CAPTURE_RETRY_MAX_S), 0.5))

    async def _wait_for_bluealsa(self, timeout_s: float = BLUEALSA_WAIT_S) -> bool:
        """Block until bluealsa owns org.bluealsa, so the first capture attempt can succeed.

        The manager starts our daemons AFTER this source, so on a restart with a phone already
        connected we would otherwise open the PCM before the daemon exists. Bounded, and a timeout
        is not fatal — the retry loop still covers a daemon that is slow or genuinely broken.
        """
        if self._bus is None:
            return False
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline and self._active is not None:
            try:
                reply = await self._bus.call(
                    Message(
                        destination="org.freedesktop.DBus",
                        path="/org/freedesktop/DBus",
                        interface="org.freedesktop.DBus",
                        member="NameHasOwner",
                        signature="s",
                        body=[BLUEALSA_NAME],
                    )
                )
                if reply is not None and reply.body and reply.body[0]:
                    return True
            except Exception:  # noqa: BLE001 - bus hiccup; just retry until the deadline
                logger.debug("[%s] NameHasOwner check failed", self.instance.source_id, exc_info=True)
            await asyncio.sleep(0.25)
        logger.warning(
            "[%s] %s did not appear within %.0fs; capture will retry",
            self.instance.source_id, BLUEALSA_NAME, timeout_s,
        )
        return False

    async def _renegotiate_a2dp(self, address: str) -> None:
        """Ask BlueZ to re-establish the A2DP profile for a connected device."""
        path = next((p for p, addr in self._connected.items() if addr == address), None)
        if path is None or self._bus is None:
            return
        try:
            intro = await self._bus.introspect(BLUEZ, path)
            device = self._bus.get_proxy_object(BLUEZ, path, intro).get_interface(DEVICE_IFACE)
            await device.call_connect_profile(A2DP_SOURCE_UUID)
            logger.info("[%s] re-negotiated A2DP with %s", self.instance.source_id, address)
        except Exception:  # noqa: BLE001 - phone may refuse; the retry loop carries on regardless
            logger.debug("[%s] ConnectProfile failed for %s", self.instance.source_id, address, exc_info=True)

    async def _spawn_capture(self, address: str):
        # BlueZ sets the A2DP transport up slightly after it announces the connection; opening the
        # PCM too early fails with ENODEV and we'd back off for no reason.
        await asyncio.sleep(CAPTURE_RESTART_DELAY_S)
        if self._active != address:
            return None
        argv = self._capture_argv(address)
        log_path = os.path.join(self.instance.config_dir, "arecord.log")
        try:
            os.makedirs(self.instance.config_dir, exist_ok=True)
            self._capture_log = open(log_path, "ab", buffering=0)  # noqa: SIM115 - closed in _stop_capture
            self._capture = await asyncio.create_subprocess_exec(
                *argv,
                stdout=self._capture_log, stderr=self._capture_log, stdin=subprocess.DEVNULL,
                start_new_session=True,  # own process group: kill it without touching the server
            )
        except OSError as e:
            logger.warning("[%s] could not start capture: %s", self.instance.source_id, e)
            self._close_capture_log()
            return None
        logger.info("[%s] capturing %s (arecord pid=%d)", self.instance.source_id, address, self._capture.pid)
        return self._capture

    async def _stop_capture(self) -> None:
        self._active = None  # tells _capture_loop to exit rather than treat the kill as a failure
        task, self._capture_task = self._capture_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        proc, self._capture = self._capture, None
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                with contextlib.suppress(Exception):
                    await proc.wait()
            logger.info("[%s] capture stopped", self.instance.source_id)
        self._close_capture_log()

    def _close_capture_log(self) -> None:
        if self._capture_log is not None:
            with contextlib.suppress(Exception):
                self._capture_log.close()
            self._capture_log = None
