#!/usr/bin/env python3
"""
Plum-Audio — AirPlay multi-instance config generation (shairport-sync).

Renders one shairport-sync config per AirPlay endpoint (from settings.json
integrations.airplay.endpoints) and resolves them to AirplayInstances. One endpoint → one
shairport-sync process → one instance FIFO → one Sendspin source, plus a private D-Bus session for
that instance's MPRIS transport control (see airplay_manager.py).

Each instance owns a unique RAOP port and UDP port block; the daemon advertises itself through the
host's Avahi, so nothing of ours binds 5353.

Mirrors spotify_config.py deliberately — same shape, so the manager machinery is shared.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from .config_render import escape_libconfig, write_atomic

logger = logging.getLogger("plum.airplay_config")

# Defaults are container paths (Binhex /app layout); every path is overridable — by argument or the
# PLUM_AIRPLAY_* env vars — so the Pi rig and tests can point them at a scratch dir.
DEFAULT_TEMPLATE = os.environ.get("PLUM_AIRPLAY_TEMPLATE", "/app/config/shairport-sync.conf.template")
DEFAULT_CONFIG_ROOT = os.environ.get("PLUM_AIRPLAY_CONFIG_DIR", "/data/shairport")
DEFAULT_SHAIRPORT_BIN = os.environ.get("PLUM_SHAIRPORT_BIN", "shairport-sync")
DEFAULT_DBUS_BIN = os.environ.get("PLUM_DBUS_DAEMON_BIN", "dbus-daemon")

MAX_ENDPOINTS = 10
PORT_BASE = 5050  # RAOP control port for endpoint 1; +1 per endpoint (see CLAUDE.md port map)
UDP_PORT_BASE = 6001  # UDP block start for endpoint 1; +UDP_PORT_STRIDE per endpoint
UDP_PORT_STRIDE = 10  # must be >= the template's udp_port_range


@dataclass(frozen=True)
class AirplayInstance:
    """A single resolved AirPlay endpoint the server should bring up as a source."""

    instance_id: str
    device_name: str
    port: int
    udp_port_base: int
    fifo_path: str
    metadata_fifo: str
    config_dir: str

    @property
    def source_id(self) -> str:
        return f"airplay-{self.instance_id}"

    @property
    def config_path(self) -> str:
        return os.path.join(self.config_dir, "shairport-sync.conf")

    @property
    def bus_socket(self) -> str:
        return os.path.join(self.config_dir, "dbus.socket")

    @property
    def bus_address(self) -> str:
        """Private session-bus address for this instance's MPRIS interface."""
        return f"unix:path={self.bus_socket}"


def fifo_path_for(instance_id: str) -> str:
    return f"/tmp/airplay-{instance_id}-fifo"


def metadata_fifo_for(instance_id: str) -> str:
    return f"/tmp/airplay-{instance_id}-metadata-fifo"


def port_for(instance_id: str) -> int:
    try:
        return PORT_BASE + int(instance_id) - 1
    except (TypeError, ValueError):
        return PORT_BASE


def udp_port_base_for(instance_id: str) -> int:
    try:
        return UDP_PORT_BASE + (int(instance_id) - 1) * UDP_PORT_STRIDE
    except (TypeError, ValueError):
        return UDP_PORT_BASE


def enabled_endpoints(settings: dict) -> list[dict]:
    """Enabled AirPlay endpoints from a settings dict, capped at MAX_ENDPOINTS."""
    airplay = settings.get("integrations", {}).get("airplay", {}) or {}
    endpoints = airplay.get("endpoints", []) or []
    return [e for e in endpoints[:MAX_ENDPOINTS] if e.get("enabled")]


def _instance(endpoint: dict, config_root: str) -> AirplayInstance:
    instance_id = str(endpoint.get("id"))
    return AirplayInstance(
        instance_id=instance_id,
        device_name=endpoint.get("deviceName", f"Plum Audio {instance_id}"),
        # Ports are stored per endpoint (the GUI shows them), but fall back to the derived block so
        # an endpoint written by an older build still lands somewhere unique.
        port=int(endpoint.get("port") or port_for(instance_id)),
        udp_port_base=int(endpoint.get("udpPortBase") or udp_port_base_for(instance_id)),
        fifo_path=fifo_path_for(instance_id),
        metadata_fifo=metadata_fifo_for(instance_id),
        config_dir=os.path.join(config_root, instance_id),
    )


def instances_from_settings(settings: dict, *, config_root: str = DEFAULT_CONFIG_ROOT) -> list[AirplayInstance]:
    """Resolve enabled endpoints to AirplayInstances WITHOUT writing any files."""
    return [_instance(e, config_root) for e in enabled_endpoints(settings)]


def render_configs(
    settings: dict,
    *,
    template_path: str = DEFAULT_TEMPLATE,
    config_root: str = DEFAULT_CONFIG_ROOT,
) -> list[AirplayInstance]:
    """Write a shairport-sync.conf into each enabled endpoint's config dir; return the instances."""
    with open(template_path, encoding="utf-8") as f:
        template = f.read()

    instances: list[AirplayInstance] = []
    for endpoint in enabled_endpoints(settings):
        inst = _instance(endpoint, config_root)
        os.makedirs(inst.config_dir, exist_ok=True)

        # escape_libconfig, not the raw name: this lands inside `name = "...";`, and shairport's
        # sessioncontrol block runs shell commands, so an unescaped quote is RCE as root.
        rendered = (
            template.replace("PLUM_AP_NAME", escape_libconfig(inst.device_name))
            .replace("PLUM_AP_PORT", str(inst.port))
            .replace("PLUM_AP_UDP_BASE", str(inst.udp_port_base))
            .replace("PLUM_AP_ID", inst.instance_id)
        )
        write_atomic(inst.config_path, rendered)

        instances.append(inst)
        logger.info(
            "rendered shairport config %s (name=%r port=%d udp=%d)",
            inst.config_path, inst.device_name, inst.port, inst.udp_port_base,
        )

    return instances
