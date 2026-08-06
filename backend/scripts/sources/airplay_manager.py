#!/usr/bin/env python3
"""
Plum-Audio — AirPlay endpoints: the reconciling manager.

Two processes per enabled endpoint, started in this order:
  1. `dbus-daemon --session` on a private socket — the endpoint's own message bus.
  2. `shairport-sync -c <rendered config>` with DBUS_SESSION_BUS_ADDRESS pointing at it.

The private bus is the whole trick behind multi-endpoint transport control: shairport's MPRIS bus
name (`org.mpris.MediaPlayer2.ShairportSync`) is FIXED, so N instances on the system bus fight over
it and only the first ever gets Play/Pause/Next/Previous. Giving each instance its own bus lets
every endpoint own the name unopposed; AirplayRemote then connects by bus address rather than to
the system bus. Verified on hardware (shairport 4.3.7): "MPRIS service started ... on the session
bus", and dbus-next binds the Player interface there.

Everything else (poll, reconcile, respawn, teardown) comes from SourceManagerBase.
Respool triggers (the signature): device name, RAOP port, UDP port base.
"""

from __future__ import annotations

import logging
import os

from sources import airplay_config
from sources.source_manager import DaemonSpec, SourceManagerBase

logger = logging.getLogger("plum.airplay_manager")

BUS_SETTLE_S = 0.5  # let dbus-daemon create its socket before shairport tries to connect


class AirplayManager(SourceManagerBase):
    def __init__(
        self,
        server,
        *,
        settings_file: str | None = None,
        template_path: str = airplay_config.DEFAULT_TEMPLATE,
        config_root: str = airplay_config.DEFAULT_CONFIG_ROOT,
        binary: str = airplay_config.DEFAULT_SHAIRPORT_BIN,
        dbus_binary: str = airplay_config.DEFAULT_DBUS_BIN,
        **kwargs,
    ) -> None:
        super().__init__(server, name="airplay", settings_file=settings_file, **kwargs)
        self.template_path = template_path
        self.config_root = config_root
        self.binary = binary
        self.dbus_binary = dbus_binary
        # A survivor of a crashed/previous run still holds the RAOP port (shairport) or the bus
        # socket path (dbus-daemon), which would block or orphan the fresh instance — the private
        # dbus-daemon especially, since it detaches (setsid) and would otherwise pile up one per
        # restart, leaving AirplayRemote resolving the wrong daemon. Sweep BOTH, scoped to our root.
        self.stale_pattern = [f"shairport-sync.*{config_root}", f"dbus-daemon.*{config_root}"]

    def desired(self, settings: dict) -> dict[str, tuple[tuple, object]]:
        endpoints = airplay_config.enabled_endpoints(settings)
        instances = airplay_config.instances_from_settings(settings, config_root=self.config_root)
        return {
            inst.instance_id: ((inst.device_name, inst.port, inst.udp_port_base), inst)
            # strict: instances_from_settings is one _instance per enabled_endpoints entry, from the
            # same settings dict — a length mismatch means that invariant broke and must not be silent.
            for _e, inst in zip(endpoints, instances, strict=True)
        }

    def render(self, settings: dict) -> None:
        airplay_config.render_configs(settings, template_path=self.template_path, config_root=self.config_root)

    def fifos(self, instance) -> list[str]:
        # The audio FIFO is the feeder's; the metadata FIFO has no other creator, and shairport
        # would otherwise create it itself with the wrong ownership under PUID/PGID.
        return [instance.fifo_path, instance.metadata_fifo]

    def daemons(self, instance) -> list[DaemonSpec]:
        if not os.path.exists(instance.config_path):
            logger.warning("[%s] no config at %s; not starting", instance.source_id, instance.config_path)
            return []
        # A stale socket file from an unclean exit makes dbus-daemon refuse to bind.
        if os.path.exists(instance.bus_socket):
            try:
                os.unlink(instance.bus_socket)
            except OSError as e:
                logger.warning("[%s] could not clear stale bus socket: %s", instance.source_id, e)
        log_dir = instance.config_dir
        return [
            DaemonSpec(
                argv=[self.dbus_binary, "--session", f"--address={instance.bus_address}", "--nofork", "--nopidfile"],
                log_path=os.path.join(log_dir, "dbus.log"),
                settle_s=BUS_SETTLE_S,
            ),
            DaemonSpec(
                argv=[self.binary, "-c", instance.config_path],
                log_path=os.path.join(log_dir, "shairport-sync.log"),
                env={"DBUS_SESSION_BUS_ADDRESS": instance.bus_address},
            ),
        ]

    async def start_source(self, instance) -> None:
        await self.server.start_airplay_source(instance)

    async def stop_source(self, instance) -> None:
        await self.server.stop_airplay_source(instance.source_id)
