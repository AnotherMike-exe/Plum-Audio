#!/usr/bin/env python3
"""
Plum-Audio — integrations API (Flask blueprint).

Serves /api/integrations/* for the GUI's Integrations settings tab. Phase 3 ships the Spotify
slice first (it's the first ported source); airplay/dlna/bluetooth/plexamp slices are added here as
those sources land. Endpoint CRUD persists to the same settings.json the config API owns (via
SettingsManager), then regenerates the spotifyd configs + supervisord include and, in the container,
reloads supervisord. Off-container (the Pi rig, no supervisord) it degrades to persist + config
files only.

Ported from Plum-Snapcast's spotify_endpoints_api.py. Changed for the Sendspin model: the apply step
regenerates via sources.spotify_config (one spotifyd program per instance — no fifo-keeper or
lifecycle-manager) instead of the old setup shell script.

Live pick-up caveat: the running sendspin_server brings up a Sendspin source per enabled endpoint at
startup. Adding/removing an endpoint here persists + (re)starts spotifyd, but the running server
picks up the source set on its next start. Live add/remove is the "integration activity signaling"
work (deferred).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading

from flask import Blueprint, jsonify, request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # scripts/ on path
from sources import spotify_config  # noqa: E402

from settings_api import SettingsManager  # noqa: E402

logger = logging.getLogger(__name__)

VALID_BITRATES = (96, 160, 320)
MAX_ENDPOINTS = 10
SUPERVISORCTL = ["supervisorctl", "-c", "/app/supervisord/supervisord.conf"]


class SpotifyEndpointsManager:
    """CRUD for Spotify Connect endpoints, persisted via SettingsManager."""

    def __init__(self, settings_manager: SettingsManager | None = None) -> None:
        self.settings_manager = settings_manager or SettingsManager()
        self._lock = threading.Lock()  # serialize apply/regeneration

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

    def _apply(self) -> list[str]:
        """Regenerate spotifyd configs + supervisord include; reload supervisord if present.

        Returns a list of non-fatal warnings (e.g. off-container, where config paths/supervisord
        aren't available and we persist settings only).
        """
        warnings: list[str] = []
        with self._lock:
            settings = self.settings_manager.get_settings()
            instances: list[spotify_config.SpotifyInstance] = []
            try:
                instances = spotify_config.render_configs(settings)
                spotify_config.generate_supervisord(instances)
            except OSError as e:
                warnings.append(f"config regen skipped: {e}")
                logger.warning("spotify config regen skipped (off-container?): %s", e)
            warnings.extend(self._reload_supervisor([i.instance_id for i in instances]))
        return warnings

    def _reload_supervisor(self, enabled_ids: list[str]) -> list[str]:
        """reread + update to pick up added/removed programs, then RESTART each enabled instance.

        The restart matters: reread/update alone starts new programs and stops removed ones, but an
        *edited* endpoint's program already exists — so without an explicit restart the running
        spotifyd keeps its old config (old name + device_id). Restarting respools it onto the freshly
        rendered config. (This is the spool-down/up the container owns; on the rig, where supervisorctl
        is absent, this degrades to a warning and the instance is respooled by rerunning run_spotify.sh.)
        """
        warnings: list[str] = []
        for action in (["reread"], ["update"]):
            try:
                subprocess.run(SUPERVISORCTL + action, capture_output=True, text=True, timeout=15, check=False)
            except (OSError, subprocess.SubprocessError) as e:
                warnings.append(f"supervisorctl {action[0]} unavailable: {e}")
                return warnings  # no supervisord here — stop trying
        for instance_id in enabled_ids:
            try:
                subprocess.run(
                    SUPERVISORCTL + ["restart", f"spotifyd-{instance_id}"],
                    capture_output=True, text=True, timeout=20, check=False,
                )
            except (OSError, subprocess.SubprocessError) as e:
                warnings.append(f"restart spotifyd-{instance_id} failed: {e}")
        return warnings

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
        return {"success": True, "message": f"Added endpoint '{device_name}'", "endpoint": endpoint,
                "warnings": self._apply()}

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
        return {"success": True, "message": f"Updated endpoint '{endpoint_id}'", "endpoint": endpoint,
                "warnings": self._apply()}

    def remove_endpoint(self, endpoint_id: str) -> dict:
        spotify = self._spotify()
        endpoints = spotify.get("endpoints", []) or []
        endpoint = next((e for e in endpoints if e.get("id") == endpoint_id), None)
        if endpoint is None:
            return {"success": False, "message": f"Endpoint '{endpoint_id}' not found"}
        endpoints.remove(endpoint)
        self._save(endpoints, spotify.get("bitrate", 320))
        logger.info("removed Spotify endpoint %s", endpoint_id)
        return {"success": True, "message": f"Removed endpoint '{endpoint.get('deviceName')}'",
                "warnings": self._apply()}

    def update_bitrate(self, bitrate: int) -> dict:
        if bitrate not in VALID_BITRATES:
            return {"success": False, "message": f"Bitrate must be one of {VALID_BITRATES}"}
        spotify = self._spotify()
        self._save(spotify.get("endpoints", []) or [], bitrate)
        logger.info("updated Spotify bitrate to %d", bitrate)
        return {"success": True, "message": f"Updated bitrate to {bitrate}",
                "warning": "Restarts Spotify endpoints; active playback is interrupted.",
                "warnings": self._apply()}

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
        return {"success": True, "message": f"All Spotify endpoints {'enabled' if enabled else 'disabled'}",
                "warnings": self._apply()}


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
