#!/usr/bin/env python3
"""
Plum-Audio — integrations API (Flask blueprint).

Serves /api/integrations/* for the GUI's Integrations settings tab. Phase 3 ships AirPlay,
Spotify and Bluetooth (the ported sources); dlna/plexamp slices are added here as those land.

Endpoint CRUD is PURE PERSISTENCE: it writes settings.json via the shared SettingsManager and
returns. Applying the change — rendering daemon configs, spawning/killing daemons, and bringing the
matching Sendspin sources up or down — belongs to the source managers (sources/*_manager.py), which
reconcile from that same file inside the audio event loop (a separate process from this Flask app)
and pick edits up within a few seconds. That split is deliberate: this API cannot reach the audio
loop, and the Pi rig has no supervisord, so one reconciler makes rig and container behave alike.

Ported from Plum-Snapcast's {airplay,spotify}_endpoints_api.py, minus their config-regen +
supervisorctl respool, and with the two sources' near-identical CRUD folded into one base class.
"""

from __future__ import annotations

import logging
import os
import sys

from flask import Blueprint, jsonify, request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # scripts/ on path
from sources import airplay_config, bluetooth_config  # noqa: E402

import bluetooth_bonds  # noqa: E402
from settings_api import DEVICE_NAME_ALLOWED, SettingsManager  # noqa: E402

logger = logging.getLogger(__name__)

VALID_BITRATES = (96, 160, 320)
MAX_ENDPOINTS = 10
MAX_NAME_LEN = 50


class EndpointsManager:
    """CRUD for one source's endpoint list, persisted via SettingsManager.

    Subclasses supply the per-source bits: which `integrations.<key>` section to write, and which
    transport fields a new endpoint needs (ports and the like).
    """

    source_key = ""
    # Per-source, because the renderers cap independently and a mismatch is silent: Bluetooth
    # renders at most bluetooth_config.MAX_ENDPOINTS, so a flat cap here let the API accept an
    # endpoint that enabled_endpoints then truncated away — added in the GUI, never started, no error.
    max_endpoints = MAX_ENDPOINTS

    def __init__(self, settings_manager: SettingsManager | None = None) -> None:
        self.settings_manager = settings_manager or SettingsManager()

    # -- per-source hooks ----------------------------------------------------

    def _allocate(self, endpoints: list[dict], instance_id: str) -> dict:
        """Transport fields for a NEW endpoint (ports etc). Default: none."""
        return {}

    def _extras(self, section: dict) -> dict:
        """Section-level fields to preserve alongside `endpoints` (e.g. Spotify's bitrate)."""
        return {}

    # -- helpers -------------------------------------------------------------

    def _section(self) -> dict:
        settings = self.settings_manager.get_settings()
        return settings.get("integrations", {}).get(self.source_key, {}) or {}

    def _save(self, endpoints: list[dict], extras: dict | None = None) -> None:
        payload = {"endpoints": endpoints, **(extras if extras is not None else self._extras(self._section()))}
        self.settings_manager.update_settings({"integrations": {self.source_key: payload}})

    @staticmethod
    def _next_id(endpoints: list[dict]) -> str:
        ids = [int(e["id"]) for e in endpoints if str(e.get("id", "")).isdigit()]
        return str(max(ids) + 1) if ids else "1"

    @staticmethod
    def _valid_name(device_name: str | None) -> bool:
        """Length AND charset — the name reaches a daemon config template, not just a label.

        Rejecting here is what gives the GUI a usable error message; SettingsManager sanitizes the
        same field silently for callers that bypass this CRUD (POST /api/settings takes raw JSON),
        and the renderers escape on top of that.
        """
        return bool(device_name) and bool(DEVICE_NAME_ALLOWED.match(device_name))

    # -- CRUD ----------------------------------------------------------------

    def list_endpoints(self) -> dict:
        section = self._section()
        return {"success": True, "endpoints": section.get("endpoints", []) or [], **self._extras(section)}

    def add_endpoint(self, device_name: str, enabled: bool = True) -> dict:
        if not self._valid_name(device_name):
            return {"success": False, "message": (
                    f"Invalid device name (1-{MAX_NAME_LEN} characters; letters, digits, "
                    "space and . _ - ' ( ) & + only)"
                )}
        section = self._section()
        endpoints = section.get("endpoints", []) or []
        if len(endpoints) >= self.max_endpoints:
            return {
                "success": False,
                "message": f"Maximum of {self.max_endpoints} {self.source_key} endpoints allowed",
            }
        instance_id = self._next_id(endpoints)
        endpoint = {
            "id": instance_id,
            "enabled": enabled,
            "deviceName": device_name,
            **self._allocate(endpoints, instance_id),
        }
        endpoints.append(endpoint)
        self._save(endpoints, self._extras(section))
        logger.info("added %s endpoint %s", self.source_key, endpoint)
        return {"success": True, "message": f"Added endpoint '{device_name}'", "endpoint": endpoint}

    def update_endpoint(self, endpoint_id: str, device_name: str | None = None, enabled: bool | None = None) -> dict:
        section = self._section()
        endpoints = section.get("endpoints", []) or []
        endpoint = next((e for e in endpoints if e.get("id") == endpoint_id), None)
        if endpoint is None:
            return {"success": False, "message": f"Endpoint '{endpoint_id}' not found"}
        if device_name is not None:
            if not self._valid_name(device_name):
                return {"success": False, "message": (
                    f"Invalid device name (1-{MAX_NAME_LEN} characters; letters, digits, "
                    "space and . _ - ' ( ) & + only)"
                )}
            endpoint["deviceName"] = device_name
        if enabled is not None:
            endpoint["enabled"] = enabled
        self._save(endpoints, self._extras(section))
        logger.info("updated %s endpoint %s: %s", self.source_key, endpoint_id, endpoint)
        return {"success": True, "message": f"Updated endpoint '{endpoint_id}'", "endpoint": endpoint}

    def remove_endpoint(self, endpoint_id: str) -> dict:
        section = self._section()
        endpoints = section.get("endpoints", []) or []
        endpoint = next((e for e in endpoints if e.get("id") == endpoint_id), None)
        if endpoint is None:
            return {"success": False, "message": f"Endpoint '{endpoint_id}' not found"}
        endpoints.remove(endpoint)
        self._save(endpoints, self._extras(section))
        logger.info("removed %s endpoint %s", self.source_key, endpoint_id)
        return {"success": True, "message": f"Removed endpoint '{endpoint.get('deviceName')}'"}

    def status(self) -> dict:
        section = self._section()
        endpoints = section.get("endpoints", []) or []
        return {
            "success": True,
            "enabled": any(e.get("enabled") for e in endpoints),
            "endpoints": endpoints,
            **self._extras(section),
        }

    def set_all_enabled(self, enabled: bool) -> dict:
        section = self._section()
        endpoints = section.get("endpoints", []) or []
        for e in endpoints:
            e["enabled"] = enabled
        self._save(endpoints, self._extras(section))
        return {
            "success": True,
            "message": f"All {self.source_key} endpoints {'enabled' if enabled else 'disabled'}",
        }


class SpotifyEndpointsManager(EndpointsManager):
    """Spotify Connect endpoints. Extra section field: the shared bitrate."""

    source_key = "spotify"

    def _extras(self, section: dict) -> dict:
        return {"bitrate": section.get("bitrate", 320)}

    def _allocate(self, endpoints: list[dict], instance_id: str) -> dict:
        # Zeroconf port: next free above the highest in use (each instance must advertise its own).
        return {"zeroconfPort": max((e.get("zeroconfPort", 5354) for e in endpoints), default=5353) + 1}

    def update_bitrate(self, bitrate: int) -> dict:
        if bitrate not in VALID_BITRATES:
            return {"success": False, "message": f"Bitrate must be one of {VALID_BITRATES}"}
        section = self._section()
        self._save(section.get("endpoints", []) or [], {"bitrate": bitrate})
        logger.info("updated Spotify bitrate to %d", bitrate)
        return {"success": True, "message": f"Updated bitrate to {bitrate}",
                "warning": "Restarts Spotify endpoints; active playback is interrupted."}


class BluetoothEndpointsManager(EndpointsManager):
    """Bluetooth endpoints — one per ADAPTER, so realistically one.

    Section-level extras are autoPair/discoverable: they describe how the integration pairs, not
    how one radio behaves, and bluetooth_config carries them onto every instance because the
    adapter is what applies them.
    """

    max_endpoints = bluetooth_config.MAX_ENDPOINTS

    source_key = "bluetooth"

    def _extras(self, section: dict) -> dict:
        return {
            "autoPair": bool(section.get("autoPair", True)),
            "discoverable": bool(section.get("discoverable", True)),
        }

    def _allocate(self, endpoints: list[dict], instance_id: str) -> dict:
        # One endpoint per adapter: take the first hciN not already claimed. Adding a second
        # endpoint without a second radio is allowed but will simply find no adapter at runtime,
        # which bluetooth_adapter logs rather than crashing on.
        taken = {e.get("adapter") for e in endpoints}
        for n in range(bluetooth_config.MAX_ENDPOINTS):
            candidate = f"hci{n}"
            if candidate not in taken:
                return {"adapter": candidate}
        return {"adapter": bluetooth_config.adapter_for(instance_id)}

    def update_pairing(self, auto_pair: bool | None = None, discoverable: bool | None = None) -> dict:
        """Update the section-level pairing toggles (GUI: Integrations → Bluetooth)."""
        section = self._section()
        extras = self._extras(section)
        if auto_pair is not None:
            extras["autoPair"] = bool(auto_pair)
        if discoverable is not None:
            extras["discoverable"] = bool(discoverable)
        self._save(section.get("endpoints", []) or [], extras)
        logger.info("updated bluetooth pairing settings: %s", extras)
        return {
            "success": True,
            "message": "Updated Bluetooth settings",
            **extras,
            # Both toggles are adapter properties applied by the running source, and the manager
            # respools the endpoint to pick them up — so an active A2DP session is interrupted.
            "warning": "Restarts Bluetooth; a connected device is disconnected.",
        }


class AirplayEndpointsManager(EndpointsManager):
    """AirPlay endpoints. Each needs its own RAOP port and UDP port block."""

    source_key = "airplay"

    def _allocate(self, endpoints: list[dict], instance_id: str) -> dict:
        # Derive from the id so the block matches airplay_config's fallback, then push past anything
        # already in use (endpoints can be removed and re-added, leaving gaps).
        port = max(
            airplay_config.port_for(instance_id),
            max((e.get("port", 0) for e in endpoints), default=0) + 1,
        )
        udp_base = max(
            airplay_config.udp_port_base_for(instance_id),
            max((e.get("udpPortBase", 0) for e in endpoints), default=0) + airplay_config.UDP_PORT_STRIDE,
        )
        return {"port": port, "udpPortBase": udp_base}


def _register_endpoint_routes(bp: Blueprint, source: str, manager: EndpointsManager) -> None:
    """Wire the shared /<source>/{status,enable,disable,device-name,endpoints[/<id>]} routes."""

    def _status_code(result: dict) -> int:
        return 200 if result.get("success") else 400

    @bp.get(f"/{source}/status", endpoint=f"{source}_status")
    def _status():
        return jsonify(manager.status())

    @bp.post(f"/{source}/enable", endpoint=f"{source}_enable")
    def _enable():
        return jsonify(manager.set_all_enabled(True))

    @bp.post(f"/{source}/disable", endpoint=f"{source}_disable")
    def _disable():
        return jsonify(manager.set_all_enabled(False))

    @bp.post(f"/{source}/device-name", endpoint=f"{source}_device_name")
    def _device_name():
        data = request.get_json(silent=True) or {}
        # Single-name shim: update the first endpoint (multi-instance uses the per-endpoint PUT).
        endpoints = manager.list_endpoints()["endpoints"]
        if not endpoints:
            return jsonify({"success": False, "message": f"No {source} endpoints configured"}), 400
        result = manager.update_endpoint(endpoints[0]["id"], device_name=data.get("deviceName"))
        return jsonify(result), _status_code(result)

    @bp.get(f"/{source}/endpoints", endpoint=f"{source}_list")
    def _list():
        return jsonify(manager.list_endpoints())

    @bp.post(f"/{source}/endpoints", endpoint=f"{source}_add")
    def _add():
        data = request.get_json(silent=True) or {}
        result = manager.add_endpoint(data.get("deviceName", ""), data.get("enabled", True))
        return jsonify(result), _status_code(result)

    @bp.put(f"/{source}/endpoints/<endpoint_id>", endpoint=f"{source}_update")
    def _update(endpoint_id: str):
        data = request.get_json(silent=True) or {}
        result = manager.update_endpoint(endpoint_id, data.get("deviceName"), data.get("enabled"))
        return jsonify(result), _status_code(result)

    @bp.delete(f"/{source}/endpoints/<endpoint_id>", endpoint=f"{source}_remove")
    def _remove(endpoint_id: str):
        result = manager.remove_endpoint(endpoint_id)
        return jsonify(result), _status_code(result)


def create_integrations_blueprint(settings_manager: SettingsManager | None = None) -> Blueprint:
    """Build the /api/integrations blueprint. AirPlay + Spotify today; more sources slot in here."""
    bp = Blueprint("integrations", __name__, url_prefix="/api/integrations")
    spotify = SpotifyEndpointsManager(settings_manager)
    airplay = AirplayEndpointsManager(settings_manager)
    bluetooth = BluetoothEndpointsManager(settings_manager)

    _register_endpoint_routes(bp, "airplay", airplay)
    _register_endpoint_routes(bp, "spotify", spotify)
    _register_endpoint_routes(bp, "bluetooth", bluetooth)

    @bp.get("/bluetooth/devices")
    def bluetooth_devices():
        """Paired devices, straight from BlueZ — see apis/bluetooth_bonds.py for why not settings.json."""
        try:
            return jsonify({"success": True, "devices": bluetooth_bonds.list_paired_devices()}), 200
        except bluetooth_bonds.BlueZUnavailable as e:
            # An empty list would read as "nothing is paired", which is a different and worse lie
            # than saying the radio could not be reached.
            return jsonify({"success": False, "devices": [], "message": f"Bluetooth unavailable: {e}"}), 503

    @bp.delete("/bluetooth/devices/<address>")
    def bluetooth_forget_device(address: str):
        """Forget one device: drops its link key so the phone can pair cleanly again."""
        try:
            ok, message = bluetooth_bonds.forget_device(address)
        except bluetooth_bonds.BlueZUnavailable as e:
            return jsonify({"success": False, "message": f"Bluetooth unavailable: {e}"}), 503
        return jsonify({"success": ok, "message": message, "address": address}), 200 if ok else 400

    @bp.post("/bluetooth/settings")
    def bluetooth_settings():
        """Section-level pairing toggles. The GUI has posted here since the Plum-Snapcast port."""
        data = request.get_json(silent=True) or {}
        result = bluetooth.update_pairing(data.get("autoPair"), data.get("discoverable"))
        return jsonify(result), 200 if result.get("success") else 400

    @bp.post("/spotify/bitrate")
    def spotify_bitrate():
        data = request.get_json(silent=True) or {}
        try:
            bitrate = int(data.get("bitrate"))
        except (TypeError, ValueError):
            return jsonify({"success": False, "message": "bitrate must be an integer"}), 400
        result = spotify.update_bitrate(bitrate)
        return jsonify(result), 200 if result.get("success") else 400

    return bp
