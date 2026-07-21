#!/usr/bin/env python3
"""
Settings API — server-side settings storage (GET/POST).

Ported from Plum-Snapcast `settings_api.py`. Settings persist to a JSON file (``/data/settings.json``
by default; override with ``PLUM_SETTINGS_FILE`` so the Pi test rig can persist locally). The Snapcast
bits are dropped: no snapserver cover-art proxy (Sendspin delivers artwork over the controller WS as
binary), no ``snapclientTarget`` (there is no snapclient), no ``snapcast`` integration flag.

Served by the standalone Flask host in ``apis/server.py`` on port 5002 (the mesh owns 5001 and must
stay aiohttp — it runs in the audio event loop).
"""

import copy
import json
import logging
import os
import re
import subprocess
from typing import Dict, Any

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

# Default settings structure. `integrations` matches frontend/types.ts `Settings` (endpoints-array
# shape); `version` increments on every update so the GUI's poller detects changes.
DEFAULT_SETTINGS = {
    "version": 1,
    "deviceName": "Plum Audio",
    "hostname": "plum-audio",
    "integrations": {
        "airplay": {
            "endpoints": [
                {
                    "id": "1",
                    "enabled": True,
                    "deviceName": "Plum Audio",
                    "port": 5050,
                    "udpPortBase": 6001,
                }
            ]
        },
        "bluetooth": {
            "enabled": False,
            "deviceName": "Plum Audio",
            "adapter": "hci0",
            "autoPair": True,
            "discoverable": True,
        },
        "spotify": {
            "bitrate": 320,
            "endpoints": [
                {
                    "id": "1",
                    "enabled": False,
                    "deviceName": "Plum Audio",
                    "zeroconfPort": 5354,
                }
            ],
        },
        "dlna": {"endpoints": []},  # user adds via the UI
        "plexamp": {
            "available": False,  # set from PLEXAMP_ENABLED env
            "enabled": False,
            "sourceName": "Plexamp",
        },
        "snapcast": False,  # inert; the mesh replaces Snapcast, but the frontend type still declares it
        "visualizer": False,  # boolean | VisualizerSettings; the VisualizerTab populates the object
    },
    "federation": {
        # Kept to satisfy the frontend Settings type; the mesh discovers peers automatically, so
        # this is inert (no federation service in Plum-Audio).
        "enabled": False,
        "autoDiscover": True,
    },
    "audio": {
        "output": {
            "device": "hw:Headphones",
            "device_type": "BUILTIN_HEADPHONES",
            "fallback_device": "hw:Headphones",
        },
        "input": {"devices": []},
        "calibration": {
            # Per-endpoint volume calibration; keys are mesh player IDs, values are calibration data.
        },
    },
}

SETTINGS_FILE = os.environ.get("PLUM_SETTINGS_FILE", "/data/settings.json")


class SettingsManager:
    """Manages server-side settings persistence."""

    def __init__(self, settings_file: str = SETTINGS_FILE):
        self.settings_file = settings_file
        self._ensure_settings_file()

    def _ensure_settings_file(self):
        """Ensure settings file exists with default values."""
        if not os.path.exists(self.settings_file):
            logger.info(f"Creating settings file at {self.settings_file}")
            os.makedirs(os.path.dirname(self.settings_file), exist_ok=True)

            initial_settings = copy.deepcopy(DEFAULT_SETTINGS)
            plexamp_enabled = os.getenv("PLEXAMP_ENABLED", "0").strip() in ("1", "true", "True", "TRUE", "yes", "Yes", "YES")
            initial_settings["integrations"]["plexamp"]["available"] = plexamp_enabled
            initial_settings["integrations"]["plexamp"]["enabled"] = plexamp_enabled
            logger.info(f"Initializing Plexamp: available={plexamp_enabled}, enabled={plexamp_enabled}")

            self._save_settings(initial_settings)

    def _save_settings(self, settings: Dict[str, Any]):
        """Save settings to the JSON file, atomically.

        Write-to-temp + os.replace, because this file is a cross-process contract: the audio
        process's Spotify reconciler and the GUI both read it on a poll, and a truncated
        in-place rewrite would hand them a torn/empty JSON mid-save.
        """
        try:
            tmp_path = f"{self.settings_file}.tmp"
            with open(tmp_path, "w") as f:
                json.dump(settings, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.settings_file)

            # Best-effort ownership fix-up in the container (PUID/PGID). Skips cleanly when not root
            # (e.g. running as a plain user on the Pi test rig).
            try:
                puid = int(os.getenv("PUID", "99"))
                pgid = int(os.getenv("PGID", "100"))
                os.chown(self.settings_file, puid, pgid)
                os.chmod(self.settings_file, 0o644)
            except Exception as chown_error:
                logger.debug(f"Could not set file ownership (non-root is fine): {chown_error}")

            logger.info("Settings saved successfully")
        except Exception as e:
            logger.error(f"Failed to save settings: {e}")
            raise

    def get_settings(self) -> Dict[str, Any]:
        """Load settings from the JSON file, merged over defaults so new keys always exist."""
        try:
            with open(self.settings_file, "r") as f:
                settings = json.load(f)

            merged = copy.deepcopy(DEFAULT_SETTINGS)
            for key in merged:
                if key in settings:
                    if isinstance(merged[key], dict):
                        merged[key].update(settings[key])
                    else:
                        merged[key] = settings[key]

            # Keep Plexamp availability in sync with the environment (docker-compose configuration).
            plexamp_enabled = os.getenv("PLEXAMP_ENABLED", "0").strip() in ("1", "true", "True", "TRUE", "yes", "Yes", "YES")
            if merged["integrations"]["plexamp"]["available"] != plexamp_enabled:
                logger.info(f"Syncing Plexamp availability from environment: {plexamp_enabled}")
                merged["integrations"]["plexamp"]["available"] = plexamp_enabled
                if not plexamp_enabled:
                    merged["integrations"]["plexamp"]["enabled"] = False
                elif not settings.get("integrations", {}).get("plexamp", {}).get("available", False):
                    merged["integrations"]["plexamp"]["enabled"] = True
                self._save_settings(merged)

            return merged
        except FileNotFoundError:
            logger.warning("Settings file not found, using defaults")
            return copy.deepcopy(DEFAULT_SETTINGS)
        except Exception as e:
            logger.error(f"Failed to load settings: {e}")
            return copy.deepcopy(DEFAULT_SETTINGS)

    def update_settings(self, new_settings: Dict[str, Any]) -> Dict[str, Any]:
        """Update settings (partial or full), deep-merging one level and bumping the version."""
        current = self.get_settings()

        for key in new_settings:
            if key == "version":
                continue  # version is server-owned
            if key in current and isinstance(current[key], dict) and isinstance(new_settings[key], dict):
                current[key].update(new_settings[key])
            else:
                current[key] = new_settings[key]

        current["version"] = current.get("version", 0) + 1
        logger.info(f"Settings updated to version {current['version']}")

        self._save_settings(current)
        return current

    @staticmethod
    def validate_hostname(hostname: str) -> tuple[bool, str]:
        """Validate a hostname per DNS rules. Returns (is_valid, error_message)."""
        if not hostname:
            return False, "Hostname cannot be empty"
        if len(hostname) > 63:
            return False, "Hostname must be 63 characters or less"
        if not re.match(r"^[a-z0-9-]+$", hostname):
            return False, "Hostname must contain only lowercase letters, numbers, and hyphens"
        if hostname.startswith("-") or hostname.endswith("-"):
            return False, "Hostname cannot start or end with a hyphen"
        return True, ""

    @staticmethod
    def sanitize_hostname(device_name: str) -> str:
        """Convert a device name to a valid hostname."""
        hostname = device_name.lower()
        hostname = "".join(c if c.isalnum() or c == "-" else "-" for c in hostname)
        hostname = hostname.strip("-")
        hostname = hostname[:63]
        return hostname if hostname else "plum-audio"

    @staticmethod
    def update_avahi_hostname(hostname: str) -> tuple[bool, str]:
        """Update Avahi's host-name and restart the service. Returns (success, message).

        Container-only: writing /etc/avahi and restarting via supervisorctl both require the
        container environment. Degrades gracefully (returns False) on the Pi test rig / non-root.
        """
        try:
            avahi_conf = "/etc/avahi/avahi-daemon.conf"
            with open(avahi_conf, "r") as f:
                lines = f.readlines()

            in_server_section = False
            hostname_updated = False
            new_lines = []
            for line in lines:
                if line.strip() == "[server]":
                    in_server_section = True
                    new_lines.append(line)
                elif line.strip().startswith("[") and in_server_section:
                    if not hostname_updated:
                        new_lines.append(f"host-name={hostname}\n")
                        hostname_updated = True
                    in_server_section = False
                    new_lines.append(line)
                elif in_server_section and line.strip().startswith("host-name="):
                    new_lines.append(f"host-name={hostname}\n")
                    hostname_updated = True
                else:
                    new_lines.append(line)
            if in_server_section and not hostname_updated:
                new_lines.append(f"host-name={hostname}\n")

            with open(avahi_conf, "w") as f:
                f.writelines(new_lines)
            logger.info(f"Updated Avahi hostname to: {hostname}")

            result = subprocess.run(
                ["supervisorctl", "-c", "/app/supervisord/supervisord.conf", "restart", "avahi"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0:
                logger.info("Avahi service restarted successfully")
                return True, f"Hostname updated to '{hostname}.local' and mDNS restarted"
            logger.error(f"Failed to restart Avahi: {result.stderr}")
            return False, f"Hostname updated but mDNS restart failed: {result.stderr}"
        except Exception as e:
            logger.error(f"Failed to update Avahi hostname: {e}")
            return False, f"Failed to update hostname: {str(e)}"


def create_settings_blueprint(settings_manager: SettingsManager = None) -> Blueprint:
    """Create the Flask blueprint for the settings API."""
    if settings_manager is None:
        settings_manager = SettingsManager()

    bp = Blueprint("settings", __name__)

    @bp.route("/api/settings", methods=["GET"])
    def get_settings():
        try:
            return jsonify(settings_manager.get_settings())
        except Exception as e:
            logger.error(f"Get settings failed: {e}")
            return jsonify({"error": str(e)}), 500

    @bp.route("/api/settings", methods=["POST"])
    def update_settings():
        try:
            new_settings = request.get_json()
            if not new_settings:
                return jsonify({"error": "No settings provided"}), 400
            return jsonify(settings_manager.update_settings(new_settings))
        except Exception as e:
            logger.error(f"Update settings failed: {e}")
            return jsonify({"error": str(e)}), 500

    @bp.route("/api/settings/device", methods=["POST"])
    def update_device_settings():
        try:
            data = request.get_json()
            if not data:
                return jsonify({"error": "No data provided"}), 400

            device_name = data.get("deviceName")
            hostname = data.get("hostname")
            if not device_name and not hostname:
                return jsonify({"error": "Either deviceName or hostname must be provided"}), 400

            updates = {}
            messages = []

            if device_name:
                if not device_name.strip():
                    return jsonify({"error": "Device name cannot be empty"}), 400
                if len(device_name) > 100:
                    return jsonify({"error": "Device name must be 100 characters or less"}), 400
                updates["deviceName"] = device_name.strip()
                messages.append(f"Device name updated to '{device_name}'")
                logger.info(f"Device name updated to: {device_name}")

            if hostname:
                is_valid, error_msg = SettingsManager.validate_hostname(hostname)
                if not is_valid:
                    return jsonify({"error": error_msg}), 400
                updates["hostname"] = hostname
                success, avahi_msg = SettingsManager.update_avahi_hostname(hostname)
                messages.append(avahi_msg if success else f"Warning: {avahi_msg}")

            updated = settings_manager.update_settings(updates)
            return jsonify({"success": True, "message": "; ".join(messages), "settings": updated})
        except Exception as e:
            logger.error(f"Update device settings failed: {e}")
            return jsonify({"error": str(e)}), 500

    @bp.route("/api/settings/device/hostname/validate", methods=["POST"])
    def validate_hostname():
        try:
            data = request.get_json()
            hostname = data.get("hostname", "")
            is_valid, error_msg = SettingsManager.validate_hostname(hostname)
            return jsonify({"valid": is_valid, "error": error_msg if not is_valid else None})
        except Exception as e:
            logger.error(f"Hostname validation failed: {e}")
            return jsonify({"error": str(e)}), 500

    @bp.route("/api/settings/device/hostname/sanitize", methods=["POST"])
    def sanitize_hostname():
        try:
            data = request.get_json()
            device_name = data.get("deviceName", "")
            return jsonify({"hostname": SettingsManager.sanitize_hostname(device_name)})
        except Exception as e:
            logger.error(f"Hostname sanitization failed: {e}")
            return jsonify({"error": str(e)}), 500

    return bp


# Standalone testing (production is served by apis/server.py).
if __name__ == "__main__":
    from flask import Flask
    from flask_cors import CORS

    logging.basicConfig(level=logging.INFO)
    app = Flask(__name__)
    CORS(app)
    app.register_blueprint(create_settings_blueprint())
    print("Settings API running on http://localhost:5002")
    app.run(host="0.0.0.0", port=5002, debug=True)
