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

import asyncio
import copy
import json
import logging
import os
import re
from typing import Dict, Any

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

# Default settings structure. `integrations` matches frontend/types.ts `Settings` (endpoints-array
# shape); `version` increments on every update so the GUI's poller detects changes.
DEFAULT_SETTINGS = {
    "version": 1,
    "deviceName": "Plum Sendspin",
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
        # Endpoints-array shape, keyed by adapter — see sources/bluetooth_config.py. autoPair and
        # discoverable stay section-level: they describe the integration's pairing behaviour, not
        # one radio. Files written before this shape are converted by _migrate_bluetooth().
        "bluetooth": {
            "autoPair": True,
            "discoverable": True,
            "endpoints": [
                {
                    "id": "1",
                    "enabled": False,
                    "deviceName": "Plum Audio",
                    "adapter": "hci0",
                }
            ],
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
    "autoSwitch": {
        # Ported from Plum-Snapcast, live here (unlike "federation" above): auto-route this unit's
        # player onto its own source when idle and that source goes active, and/or have it follow
        # another unit's player while idle. See mesh/follow.py.
        "localActivity": False,
        "slave": {"enabled": False, "masterUnitId": None},
    },
    "audio": {
        # Deliberately EMPTY. An empty device means "whatever PLUM_DAC_DEVICE says", which is how a
        # unit that has never been near the GUI keeps the output it booted with. A concrete-looking
        # default here outranks that env on every unit at once — see _migrate_audio_output, which
        # exists to undo exactly that on units already carrying the old placeholder.
        "output": {"device": None, "device_type": None},
        "input": {"devices": []},
        "calibration": {
            # Per-endpoint volume calibration; keys are mesh player IDs, values are calibration data.
        },
    },
}

SETTINGS_FILE = os.environ.get("PLUM_SETTINGS_FILE", "/data/settings.json")

# Output specs that mean "nobody has chosen one". `hw:Headphones` was the shipped default of a tab
# that was never reachable, so it can be cleared without losing a real user choice — see
# SettingsManager._migrate_audio_output.
LEGACY_OUTPUT_PLACEHOLDERS = {"hw:Headphones", "hw:headphones", ""}


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

    @staticmethod
    def _migrate_bluetooth(integrations: Dict[str, Any]) -> bool:
        """Convert a pre-endpoints Bluetooth section in place. Returns True if anything changed.

        Bluetooth used to be a single object ({enabled, deviceName, adapter, autoPair,
        discoverable}) because there is one radio per unit. It is now an endpoints array keyed by
        adapter, matching AirPlay/Spotify so the source manager, the endpoint CRUD, and the GUI
        card are all shared. This has to run for real: BluetoothManager reads `endpoints`, so an
        un-migrated file resolves to NO endpoints and Bluetooth silently never starts — no error,
        no log, just a source that never appears.
        """
        section = integrations.get("bluetooth")
        if not isinstance(section, dict) or "endpoints" in section:
            return False
        integrations["bluetooth"] = {
            "autoPair": bool(section.get("autoPair", True)),
            "discoverable": bool(section.get("discoverable", True)),
            "endpoints": [
                {
                    "id": "1",
                    # The old flag was the whole integration's on/off switch; it becomes this
                    # endpoint's, which is the same thing while there is one adapter.
                    "enabled": bool(section.get("enabled", False)),
                    "deviceName": section.get("deviceName", "Plum Audio"),
                    "adapter": section.get("adapter", "hci0"),
                }
            ],
        }
        logger.info("migrated bluetooth settings to the endpoints-array shape")
        return True

    @staticmethod
    def _migrate_audio_output(audio: Dict[str, Any]) -> bool:
        """Clear the pre-picker output placeholder in place. Returns True if anything changed.

        Every unit built before the output picker carries `audio.output.device = "hw:Headphones"`,
        a Plum-Snapcast leftover that no GUI could ever have set — the Audio tab was never wired in.
        It is not a valid spec here (the identity is `<card_name>:<device>`), and it is actively
        dangerous the moment the player starts preferring settings.json over PLUM_DAC_DEVICE: it
        would outrank the env on every existing unit simultaneously and resolve to nothing. Treat it
        as the "unset" it always was.

        `fallback_device` goes with it. A second device id is not what recovery looks like here —
        the player's fallback is to keep the stream it already has open rather than to switch
        somewhere else, because a failed switch must never end in silence.
        """
        output = audio.get("output")
        if not isinstance(output, dict):
            return False
        changed = False
        if (output.get("device") or "").strip() in LEGACY_OUTPUT_PLACEHOLDERS:
            output["device"] = None
            output["device_type"] = None
            changed = True
        if "fallback_device" in output:
            output.pop("fallback_device")
            changed = True
        if changed:
            logger.info("cleared the pre-picker audio output placeholder")
        return changed

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

            # The merge above is shallow, so an old-shape section from the file survives verbatim.
            # Persist the conversion rather than redoing it on every read — the source manager
            # reads settings.json directly, not through this class.
            migrated = self._migrate_bluetooth(merged["integrations"])
            migrated = self._migrate_audio_output(merged["audio"]) or migrated
            if migrated:
                self._save_settings(merged)

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
        """Set the mDNS hostname on the HOST's Avahi, over D-Bus. Returns (success, message).

        This used to rewrite /etc/avahi/avahi-daemon.conf and run `supervisorctl restart avahi`,
        which cannot work in this architecture and always reported "mDNS restart failed": Avahi runs
        on the HOST (we only reach it through the mounted system bus — see CLAUDE.md), and there is
        no `avahi` supervisord program to restart. The config it rewrote was the CONTAINER's, which
        nothing reads.

        `SetHostName` is the runtime equivalent and needs neither a config file nor a restart —
        Avahi re-announces its records itself. Two honest caveats, both reported to the caller:
          * It is RUNTIME state. A host reboot (or an Avahi restart) reverts to the host's own
            hostname. We deliberately do NOT re-apply it at container start: every unit ships with
            the same default hostname, so replaying it on boot would rename all three units to the
            same thing and leave Avahi resolving the collision with -2/-3 suffixes.
          * Avahi may hand back a de-conflicted name, so we report what it actually took, not what
            we asked for.
        """
        try:
            from dbus_next.aio import MessageBus
            from dbus_next.constants import BusType
        except ImportError:
            return False, "Hostname saved, but mDNS was not updated: dbus-next is unavailable"

        async def _connect():
            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            intro = await bus.introspect("org.freedesktop.Avahi", "/")
            server = bus.get_proxy_object("org.freedesktop.Avahi", "/", intro).get_interface(
                "org.freedesktop.Avahi.Server"
            )
            return bus, server

        async def _apply() -> tuple[bool, str]:
            """Returns (changed, hostname-now). Verified on the rig against a real Avahi.

            Two behaviours drive the shape of this, and both look like failures if taken at face
            value:
              * Setting the name it already has raises "invalid because redundant". That is a
                no-op, not an error — report success.
              * A genuine change makes Avahi RESET its server, which drops our bus connection
                mid-call: the SetHostName reply never arrives and dbus-next raises "Message
                recipient disconnected from message bus without replying" for a call that
                SUCCEEDED. So the set is never trusted — we reconnect on a fresh bus and read the
                name back to find out what actually happened.
            """
            bus, server = await _connect()
            try:
                if await server.call_get_host_name() == hostname:
                    return False, hostname
                try:
                    await server.call_set_host_name(hostname)
                except Exception as set_error:  # noqa: BLE001 - the read-back is the source of truth
                    logger.debug(f"SetHostName raised (verifying by read-back): {set_error}")
            finally:
                bus.disconnect()

            await asyncio.sleep(2)  # let Avahi finish restarting before we ask it anything
            bus2, server2 = await _connect()
            try:
                return True, await server2.call_get_host_name()
            finally:
                bus2.disconnect()

        try:
            changed, applied = asyncio.run(asyncio.wait_for(_apply(), 20))
        except Exception as e:  # noqa: BLE001 - any bus/policy/validation failure is the same answer here
            logger.error(f"Avahi SetHostName failed: {e}")
            return False, f"Hostname saved, but mDNS was not updated: {e}"

        if not changed:
            return True, f"Hostname already '{applied}.local'"
        if applied != hostname:
            # Either Avahi de-conflicted it (-2, -3 … when the name is claimed on the segment) or it
            # refused and kept the old one. Either way the user needs the name it actually answers to.
            logger.warning(f"Avahi reports '{applied}' after being asked for '{hostname}'")
            return True, f"mDNS name is now '{applied}.local' (asked for '{hostname}')"
        logger.info(f"Avahi hostname set to: {applied}")
        return True, f"Hostname updated to '{applied}.local'"


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
