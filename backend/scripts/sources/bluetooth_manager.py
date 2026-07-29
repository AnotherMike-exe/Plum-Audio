#!/usr/bin/env python3
"""
Plum-Audio — Bluetooth endpoints: the reconciling manager.

ONE daemon per enabled endpoint: `bluealsa --profile=a2dp-sink -i <adapter>`, which registers an
A2DP MediaEndpoint with the host's bluetoothd and exposes each connected phone as an ALSA capture
PCM. Everything else that Bluetooth needs — powering the adapter, naming it, the pairing agent,
device tracking, and the arecord capture child — is NOT a daemon: it lives in the audio loop in
bluetooth_adapter.py, driven over the system D-Bus.

That asymmetry is the whole reason capture isn't listed here: DaemonSpecs are per-ENDPOINT and
static, while capture is per-CONNECTED-DEVICE. See bluetooth_adapter.py.

bluealsa must run as a user allowed to own `org.bluealsa` on the system bus. Debian's own policy
grants that to root ONLY (group `audio` may send but not own), so config/bluealsa-plum-dbus.conf
has to be installed or the daemon exits immediately with "Couldn't acquire D-Bus name". Verified on
the rigs — see docs and [[bluetooth-rig-provisioning]].

Respool triggers (the signature): device name, adapter, autoPair, discoverable. The last two are
adapter properties rather than daemon argv, and update_source() applies them live — but they stay in
the signature so that a toggle taken while the daemon is wedged still forces a clean restart. Like
Spotify's bitrate, changing them interrupts active playback; the API surfaces that warning.
"""

from __future__ import annotations

import logging
import os

from sources import bluetooth_config
from sources.source_manager import DaemonSpec, SourceManagerBase

logger = logging.getLogger("plum.bluetooth_manager")

BUS_SETTLE_S = 0.5  # let dbus-daemon create its socket before obexd tries to connect


class BluetoothManager(SourceManagerBase):
    def __init__(
        self,
        server,
        *,
        settings_file: str | None = None,
        config_root: str = bluetooth_config.DEFAULT_CONFIG_ROOT,
        binary: str = bluetooth_config.DEFAULT_BLUEALSA_BIN,
        obexd_binary: str = bluetooth_config.DEFAULT_OBEXD_BIN,
        dbus_binary: str = bluetooth_config.DEFAULT_DBUS_BIN,
        **kwargs,
    ) -> None:
        super().__init__(server, name="bluetooth", settings_file=settings_file, **kwargs)
        self.config_root = config_root
        self.binary = binary
        self.obexd_binary = obexd_binary
        self.dbus_binary = dbus_binary
        # Sweep survivors of a crashed run. bluealsa still owns org.bluealsa, so a fresh one cannot
        # acquire the name and exits; the private dbus-daemon detaches and would otherwise pile up
        # one per restart (as with AirPlay). bluealsa is argv-configured with no per-instance path,
        # so it is matched by our own distinctive profile flag — safe because provisioning disables
        # the distro bluealsa.service; obexd and dbus-daemon are scoped to our config root.
        self.stale_pattern = [
            f"{os.path.basename(binary)}.*--profile=a2dp-sink",
            f"dbus-daemon.*{config_root}",
            f"obexd.*{config_root}",
        ]

    def desired(self, settings: dict) -> dict[str, tuple[tuple, object]]:
        instances = bluetooth_config.instances_from_settings(settings, config_root=self.config_root)
        return {
            inst.instance_id: ((inst.device_name, inst.adapter, inst.auto_pair, inst.discoverable), inst)
            for inst in instances
        }

    def render(self, settings: dict) -> None:
        bluetooth_config.render_configs(settings, config_root=self.config_root)

    def fifos(self, instance) -> list[str]:
        # The feeder creates this too, but arecord is spawned from the D-Bus connect handler and
        # would race an absent pipe on a phone that connects the instant the source comes up.
        return [instance.fifo_path]

    def daemons(self, instance) -> list[DaemonSpec]:
        # A stale socket from an unclean exit makes dbus-daemon refuse to bind (same as AirPlay).
        if os.path.exists(instance.obex_socket):
            try:
                os.unlink(instance.obex_socket)
            except OSError as e:
                logger.warning("[%s] could not clear stale obex socket: %s", instance.source_id, e)
        return [
            DaemonSpec(
                argv=[self.binary, "--profile=a2dp-sink", "-i", instance.adapter],
                log_path=os.path.join(instance.config_dir, "bluealsa.log"),
            ),
            # AVRCP cover art only. obexd is a SESSION-bus service, so each endpoint gets its own
            # private bus rather than us adding system-bus policy for org.bluez.obex — the same
            # arrangement multi-endpoint AirPlay uses for shairport's fixed MPRIS name.
            DaemonSpec(
                argv=[self.dbus_binary, "--session", f"--address={instance.obex_bus_address}",
                      "--nofork", "--nopidfile"],
                log_path=os.path.join(instance.config_dir, "obex-dbus.log"),
                settle_s=BUS_SETTLE_S,
            ),
            DaemonSpec(
                argv=[self.obexd_binary, "-n"],
                log_path=os.path.join(instance.config_dir, "obexd.log"),
                env={"DBUS_SESSION_BUS_ADDRESS": instance.obex_bus_address},
            ),
        ]

    async def start_source(self, instance) -> None:
        await self.server.start_bluetooth_source(instance)

    async def stop_source(self, instance) -> None:
        await self.server.stop_bluetooth_source(instance.source_id)

    async def update_source(self, instance) -> None:
        """Rename the source AND push the edited name/discoverable/pairable onto the live adapter."""
        await super().update_source(instance)
        await self.server.update_bluetooth_source(instance)
