#!/usr/bin/env python3
"""
Plum-Audio — Bluetooth A2DP sink config (bluez-alsa).

Resolves the Bluetooth endpoints in settings.json to BluetoothInstances. One endpoint → one
ADAPTER (hci0) → one bluealsa daemon → one instance FIFO → one Sendspin source. Unlike AirPlay and
Spotify there is no config FILE to render: bluealsa is configured entirely by argv, and the adapter
itself (name/discoverable/pairable) is configured over D-Bus at runtime by bluetooth_adapter.py.
`render()` therefore only makes the log directory.

Mirrors airplay_config.py / spotify_config.py deliberately — same shape, so the SourceManagerBase
machinery is shared. There is realistically one entry (a Pi has one radio), but keying on the
adapter means a USB dongle is a second endpoint for free rather than a refactor.

DAEMON NAMING (verified on the rigs, 2026-07-28): upstream bluez-alsa renamed its daemon
`bluealsa` → `bluealsad` in v4.0.0, but Debian trixie's 4.3.1-3 package still installs
/usr/bin/bluealsa (and bluealsa-cli, not bluealsactl). We default to the Debian name; if a future
release adopts the rename, PLUM_BLUEALSA_BIN is the one-line escape hatch.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger("plum.bluetooth_config")

# Defaults are container paths (Binhex /app layout); every path is overridable — by argument or the
# PLUM_BLUETOOTH_*/PLUM_BLUEALSA_* env vars — so the Pi rig and tests can point them at a scratch dir.
DEFAULT_CONFIG_ROOT = os.environ.get("PLUM_BLUETOOTH_CONFIG_DIR", "/data/bluetooth")
DEFAULT_BLUEALSA_BIN = os.environ.get("PLUM_BLUEALSA_BIN", "bluealsa")
DEFAULT_ARECORD_BIN = os.environ.get("PLUM_ARECORD_BIN", "arecord")
# obexd serves AVRCP cover art on a private session bus (see BluetoothInstance.obex_bus_address).
DEFAULT_OBEXD_BIN = os.environ.get("PLUM_OBEXD_BIN", "/usr/libexec/bluetooth/obexd")
DEFAULT_DBUS_BIN = os.environ.get("PLUM_DBUS_DAEMON_BIN", "dbus-daemon")

# One endpoint per adapter, and a host has one or two. The cap is a sanity bound, not a design limit.
MAX_ENDPOINTS = 4
DEFAULT_ADAPTER = "hci0"


@dataclass(frozen=True)
class BluetoothInstance:
    """A single resolved Bluetooth endpoint (one adapter) the server should bring up as a source."""

    instance_id: str
    device_name: str
    adapter: str
    fifo_path: str
    config_dir: str
    # Section-level toggles, carried per instance because the adapter is what applies them.
    auto_pair: bool = True
    discoverable: bool = True

    @property
    def source_id(self) -> str:
        return f"bluetooth-{self.instance_id}"

    @property
    def adapter_path(self) -> str:
        """BlueZ object path for this adapter, e.g. /org/bluez/hci0."""
        return f"/org/bluez/{self.adapter}"

    @property
    def agent_path(self) -> str:
        """Our pairing agent's object path. Per instance so two adapters don't collide on the bus."""
        return f"/plum/audio/agent/{self.instance_id}"

    @property
    def obex_socket(self) -> str:
        return os.path.join(self.config_dir, "obex.socket")

    @property
    def obex_bus_address(self) -> str:
        """Private session-bus address for this endpoint's obexd (AVRCP cover art).

        obexd is a SESSION-bus service (Debian ships it as a user unit owning org.bluez.obex), and
        putting it on the system bus would need its own policy. Giving each endpoint a private bus
        instead reuses the exact trick multi-endpoint AirPlay uses for shairport's fixed MPRIS name
        — see airplay_manager.py — and keeps obexd scoped to the endpoint that spawned it.
        """
        return f"unix:path={self.obex_socket}"


def fifo_path_for(instance_id: str) -> str:
    return f"/tmp/bluetooth-{instance_id}-fifo"


def adapter_for(instance_id: str) -> str:
    """Fallback adapter derived from the endpoint id: 1 -> hci0, 2 -> hci1, ..."""
    try:
        return f"hci{int(instance_id) - 1}"
    except (TypeError, ValueError):
        return DEFAULT_ADAPTER


def enabled_endpoints(settings: dict) -> list[dict]:
    """Enabled Bluetooth endpoints from a settings dict, capped at MAX_ENDPOINTS."""
    bluetooth = settings.get("integrations", {}).get("bluetooth", {}) or {}
    endpoints = bluetooth.get("endpoints", []) or []
    return [e for e in endpoints[:MAX_ENDPOINTS] if e.get("enabled")]


def _instance(endpoint: dict, section: dict, config_root: str) -> BluetoothInstance:
    instance_id = str(endpoint.get("id"))
    return BluetoothInstance(
        instance_id=instance_id,
        device_name=endpoint.get("deviceName", f"Plum Audio {instance_id}"),
        # Stored per endpoint (the GUI shows it), falling back to the derived adapter so an endpoint
        # written by an older build still lands on a real radio.
        adapter=endpoint.get("adapter") or adapter_for(instance_id),
        fifo_path=fifo_path_for(instance_id),
        config_dir=os.path.join(config_root, instance_id),
        # Section-level: these describe the pairing behaviour of the whole integration, not one radio.
        auto_pair=bool(section.get("autoPair", True)),
        discoverable=bool(section.get("discoverable", True)),
    )


def instances_from_settings(settings: dict, *, config_root: str = DEFAULT_CONFIG_ROOT) -> list[BluetoothInstance]:
    """Resolve enabled endpoints to BluetoothInstances WITHOUT writing any files."""
    section = settings.get("integrations", {}).get("bluetooth", {}) or {}
    return [_instance(e, section, config_root) for e in enabled_endpoints(settings)]


def render_configs(
    settings: dict,
    *,
    config_root: str = DEFAULT_CONFIG_ROOT,
) -> list[BluetoothInstance]:
    """Create each enabled endpoint's directory (daemon logs live there); return the instances.

    There is no config file to write — bluealsa is argv-configured and the adapter is configured
    over D-Bus. This exists so the manager's render/reconcile contract is the same for every source.
    """
    instances = instances_from_settings(settings, config_root=config_root)
    for inst in instances:
        os.makedirs(inst.config_dir, exist_ok=True)
        logger.info(
            "bluetooth endpoint ready %s (name=%r adapter=%s auto_pair=%s discoverable=%s)",
            inst.config_dir,
            inst.device_name,
            inst.adapter,
            inst.auto_pair,
            inst.discoverable,
        )
    return instances
