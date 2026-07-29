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


class BluetoothManager(SourceManagerBase):
    def __init__(
        self,
        server,
        *,
        settings_file: str | None = None,
        config_root: str = bluetooth_config.DEFAULT_CONFIG_ROOT,
        binary: str = bluetooth_config.DEFAULT_BLUEALSA_BIN,
        **kwargs,
    ) -> None:
        super().__init__(server, name="bluetooth", settings_file=settings_file, **kwargs)
        self.config_root = config_root
        self.binary = binary
        # A survivor of a crashed run still owns org.bluealsa, so the fresh daemon can't acquire the
        # name and exits. Unlike AirPlay/Spotify there is no per-instance config path to scope the
        # pattern to (bluealsa is argv-configured), so we match our own distinctive profile flag —
        # safe because provisioning disables the distro bluealsa.service.
        self.stale_pattern = f"{os.path.basename(binary)}.*--profile=a2dp-sink"

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
        return [
            DaemonSpec(
                argv=[self.binary, "--profile=a2dp-sink", "-i", instance.adapter],
                log_path=os.path.join(instance.config_dir, "bluealsa.log"),
            )
        ]

    async def start_source(self, instance) -> None:
        await self.server.start_bluetooth_source(instance)

    async def stop_source(self, instance) -> None:
        await self.server.stop_bluetooth_source(instance.source_id)

    async def update_source(self, instance) -> None:
        """Rename the source AND push the edited name/discoverable/pairable onto the live adapter."""
        await super().update_source(instance)
        await self.server.update_bluetooth_source(instance)
