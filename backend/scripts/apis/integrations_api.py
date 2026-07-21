#!/usr/bin/env python3
"""
Plum-Audio — integrations API (Flask blueprint).

Serves /api/integrations/* for the GUI's Integrations settings tab. Phase 3 ships the Spotify
slice first (it's the first ported source); airplay/dlna/bluetooth/plexamp slices are added here as
those sources land. Endpoint CRUD is PURE PERSISTENCE: it writes settings.json via the shared SettingsManager and
returns. Applying the change — rendering go-librespot configs, spawning/killing daemons, and
bringing the matching Sendspin sources up or down — belongs to sources/spotify_manager.py, which
reconciles from that same file inside the audio event loop (a separate process from this Flask app)
and picks edits up within a few seconds. That split is deliberate: this API cannot reach the audio
loop, and the Pi rig has no supervisord, so one reconciler makes rig and container behave alike.

Ported from Plum-Snapcast's spotify_endpoints_api.py, minus its config-regen + supervisorctl respool.
"""

from __future__ import annotations

import logging
import os
import sys

from flask import Blueprint, jsonify, request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # scripts/ on path
from settings_api import SettingsManager  # noqa: E402

logger = logging.getLogger(__name__)

VALID_BITRATES = (96, 160, 320)
MAX_ENDPOINTS = 10


class SpotifyEndpointsManager:
    """CRUD for Spotify Connect endpoints, persisted via SettingsManager."""

    def __init__(self, settings_manager: SettingsManager | None = None) -> None:
        self.settings_manager = settings_manager or SettingsManager()

    # -- helpers -------------------------------------------------------------

    def _spotify(self) -> dict:
        settings = self.settings_manager.get_settings()
        return settings.get("integrations", {}).get("spotify", {}) or {}

    def _save(self, endpoints: list[dict], bitrate: int) -> None:
        self.settings_manager.update_settings(
            {"integrations": {"spotify": {"bitrate": bitrate, "endpoints": endpoints}}}
        )

    @staticmethod
    def _next_id(endpoints: list[dict]) -> str:
        ids = [int(e["id"]) for e in endpoints if str(e.get("id", "")).isdigit()]
        return str(max(ids) + 1) if ids else "1"

    @staticmethod
    def _next_port(endpoints: list[dict]) -> int:
        return max((e.get("zeroconfPort", 5354) for e in endpoints), default=5353) + 1

    # -- CRUD ----------------------------------------------------------------

    def list_endpoints(self) -> dict:
        spotify = self._spotify()
        return {"success": True, "endpoints": spotify.get("endpoints", []) or [], "bitrate": spotify.get("bitrate", 320)}

    def add_endpoint(self, device_name: str, enabled: bool = True) -> dict:
        if not device_name or len(device_name) > 50:
            return {"success": False, "message": "Invalid device name (must be 1-50 characters)"}
        spotify = self._spotify()
        endpoints = spotify.get("endpoints", []) or []
        if len(endpoints) >= MAX_ENDPOINTS:
            return {"success": False, "message": f"Maximum of {MAX_ENDPOINTS} Spotify endpoints allowed"}
        endpoint = {
            "id": self._next_id(endpoints),
            "enabled": enabled,
            "deviceName": device_name,
            "zeroconfPort": self._next_port(endpoints),
        }
        endpoints.append(endpoint)
        self._save(endpoints, spotify.get("bitrate", 320))
        logger.info("added Spotify endpoint %s", endpoint)
        return {"success": True, "message": f"Added endpoint '{device_name}'", "endpoint": endpoint}

    def update_endpoint(self, endpoint_id: str, device_name: str | None = None, enabled: bool | None = None) -> dict:
        spotify = self._spotify()
        endpoints = spotify.get("endpoints", []) or []
        endpoint = next((e for e in endpoints if e.get("id") == endpoint_id), None)
        if endpoint is None:
            return {"success": False, "message": f"Endpoint '{endpoint_id}' not found"}
        if device_name is not None:
            if not device_name or len(device_name) > 50:
                return {"success": False, "message": "Invalid device name (must be 1-50 characters)"}
            endpoint["deviceName"] = device_name
        if enabled is not None:
            endpoint["enabled"] = enabled
        self._save(endpoints, spotify.get("bitrate", 320))
        logger.info("updated Spotify endpoint %s: %s", endpoint_id, endpoint)
        return {"success": True, "message": f"Updated endpoint '{endpoint_id}'", "endpoint": endpoint}

    def remove_endpoint(self, endpoint_id: str) -> dict:
        spotify = self._spotify()
        endpoints = spotify.get("endpoints", []) or []
        endpoint = next((e for e in endpoints if e.get("id") == endpoint_id), None)
        if endpoint is None:
            return {"success": False, "message": f"Endpoint '{endpoint_id}' not found"}
        endpoints.remove(endpoint)
        self._save(endpoints, spotify.get("bitrate", 320))
        logger.info("removed Spotify endpoint %s", endpoint_id)
        return {"success": True, "message": f"Removed endpoint '{endpoint.get('deviceName')}'"}

    def update_bitrate(self, bitrate: int) -> dict:
        if bitrate not in VALID_BITRATES:
            return {"success": False, "message": f"Bitrate must be one of {VALID_BITRATES}"}
        spotify = self._spotify()
        self._save(spotify.get("endpoints", []) or [], bitrate)
        logger.info("updated Spotify bitrate to %d", bitrate)
        return {"success": True, "message": f"Updated bitrate to {bitrate}",
                "warning": "Restarts Spotify endpoints; active playback is interrupted."}

    def status(self) -> dict:
        spotify = self._spotify()
        endpoints = spotify.get("endpoints", []) or []
        return {
            "success": True,
            "enabled": any(e.get("enabled") for e in endpoints),
            "endpoints": endpoints,
            "bitrate": spotify.get("bitrate", 320),
        }

    def set_all_enabled(self, enabled: bool) -> dict:
        spotify = self._spotify()
        endpoints = spotify.get("endpoints", []) or []
        for e in endpoints:
            e["enabled"] = enabled
        self._save(endpoints, spotify.get("bitrate", 320))
        return {"success": True, "message": f"All Spotify endpoints {'enabled' if enabled else 'disabled'}"}


def create_integrations_blueprint(settings_manager: SettingsManager | None = None) -> Blueprint:
    """Build the /api/integrations blueprint. Currently the Spotify slice; more sources slot in here."""
    bp = Blueprint("integrations", __name__, url_prefix="/api/integrations")
    spotify = SpotifyEndpointsManager(settings_manager)

    def _status_code(result: dict) -> int:
        return 200 if result.get("success") else 400

    @bp.get("/spotify/status")
    def spotify_status():
        return jsonify(spotify.status())

    @bp.post("/spotify/enable")
    def spotify_enable():
        return jsonify(spotify.set_all_enabled(True))

    @bp.post("/spotify/disable")
    def spotify_disable():
        return jsonify(spotify.set_all_enabled(False))

    @bp.post("/spotify/device-name")
    def spotify_device_name():
        data = request.get_json(silent=True) or {}
        # Single-name shim: update the first endpoint (multi-instance uses per-endpoint PUT).
        endpoints = spotify.list_endpoints()["endpoints"]
        if not endpoints:
            return jsonify({"success": False, "message": "No Spotify endpoints configured"}), 400
        result = spotify.update_endpoint(endpoints[0]["id"], device_name=data.get("deviceName"))
        return jsonify(result), _status_code(result)

    @bp.get("/spotify/endpoints")
    def spotify_list():
        return jsonify(spotify.list_endpoints())

    @bp.post("/spotify/endpoints")
    def spotify_add():
        data = request.get_json(silent=True) or {}
        result = spotify.add_endpoint(data.get("deviceName", ""), data.get("enabled", True))
        return jsonify(result), _status_code(result)

    @bp.put("/spotify/endpoints/<endpoint_id>")
    def spotify_update(endpoint_id: str):
        data = request.get_json(silent=True) or {}
        result = spotify.update_endpoint(endpoint_id, data.get("deviceName"), data.get("enabled"))
        return jsonify(result), _status_code(result)

    @bp.delete("/spotify/endpoints/<endpoint_id>")
    def spotify_remove(endpoint_id: str):
        result = spotify.remove_endpoint(endpoint_id)
        return jsonify(result), _status_code(result)

    @bp.post("/spotify/bitrate")
    def spotify_bitrate():
        data = request.get_json(silent=True) or {}
        try:
            bitrate = int(data.get("bitrate"))
        except (TypeError, ValueError):
            return jsonify({"success": False, "message": "bitrate must be an integer"}), 400
        result = spotify.update_bitrate(bitrate)
        return jsonify(result), _status_code(result)

    return bp
