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
import sys
import tempfile
import threading
from typing import Dict, Any

from flask import Blueprint, jsonify, request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # scripts/ on path
import audio_devices  # noqa: E402  — the sentinel's spelling has exactly one owner

logger = logging.getLogger(__name__)

# Serialises the whole read-modify-write cycle, not just the write. Flask runs threaded=True
# (apis/server.py), so two concurrent endpoint edits both read version N, both write N+1, and the
# second silently drops the first's change — with a matching version, so the GUI's poller never
# reconciles it. Module-level rather than per-instance because settings_api's __main__ block builds
# a second SettingsManager over the same path.
_SETTINGS_LOCK = threading.RLock()

# Default settings structure. `integrations` matches frontend/types.ts `Settings` (endpoints-array
# shape); `version` increments on every update so the GUI's poller detects changes.
DEFAULT_SETTINGS = {
    "version": 1,
    # PLUM_UNIT_NAME first, for the same reason `audio.output` below is empty: this default is WRITTEN
    # to settings.json on the first read, so a concrete literal here outranks the env on every unit at
    # once and permanently. unit_identity.py documents the precedence as
    # `settings.json deviceName > PLUM_* env > DEFAULT_DEVICE_NAME`, and with a literal here the env
    # tier was unreachable — a freshly imaged unit came up as "Plum Sendspin" no matter what
    # units.conf said. Two greenfield units then appear under ONE name in the mesh view, the GUI and
    # mDNS, which is indistinguishable from a discovery bug. Found deploying the alpha to the .201
    # units, 2026-08-06. A GUI rename still wins, and still only needs writing once.
    "deviceName": os.environ.get("PLUM_UNIT_NAME", "").strip() or "Plum Sendspin",
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

# A device name is not free text: it is interpolated into shairport-sync's libconfig
# (`name = "<here>";`) and go-librespot's YAML (`device_name: "<here>"`), then a daemon is respooled
# to read it. Quotes, braces, semicolons and newlines all terminate the surrounding scalar, and
# shairport's sessioncontrol block can run shell commands — so an unfiltered name is remote code
# execution as root, reachable from any LAN page via the blanket-CORS POST /api/settings.
#
# This is the OUTER of two layers. The renderers escape as well (sources/*_config.py); neither is
# trusted alone. Permissive enough for real names ("Mike's Room", "Kitchen (Main)").
DEVICE_NAME_ALLOWED = re.compile(r"^[A-Za-z0-9 ._\-'()&+]{1,50}$")
_DEVICE_NAME_STRIP = re.compile(r"[^A-Za-z0-9 ._\-'()&+]")


def sanitize_device_name(name: Any) -> str | None:
    """Reduce a name to the safe charset, or None if nothing usable survives."""
    if not isinstance(name, str):
        return None
    cleaned = _DEVICE_NAME_STRIP.sub("", name).strip()[:50]
    return cleaned or None


def _sanitize_device_names(settings: Dict[str, Any]) -> None:
    """Scrub every device name in place, whatever route wrote it.

    POST /api/settings takes arbitrary JSON and never went through the endpoint CRUD's validation,
    so this is the only chokepoint both paths share.
    """
    top = sanitize_device_name(settings.get("deviceName"))
    if top:
        settings["deviceName"] = top

    integrations = settings.get("integrations")
    if not isinstance(integrations, dict):
        return
    for section in integrations.values():
        if not isinstance(section, dict):
            continue
        for endpoint in section.get("endpoints", []) or []:
            if not isinstance(endpoint, dict) or "deviceName" not in endpoint:
                continue
            safe = sanitize_device_name(endpoint.get("deviceName"))
            if safe != endpoint.get("deviceName"):
                logger.warning("sanitized endpoint deviceName %r -> %r", endpoint.get("deviceName"), safe)
            endpoint["deviceName"] = safe or "Plum Audio"


# An output spec is a `<card_name>:<device>` id, an `hw:C,D` address, a bare ALSA card name, or a
# PortAudio name fragment — all of which live in this charset. Deliberately permissive: this checks
# SHAPE, never existence. A spec that resolves nowhere on THIS unit is legitimate (PLUM_DAC_DEVICE
# values are fragments that only match on the unit they were written for, and the tier-2 test writes
# an unopenable device on purpose to prove the renderer falls back rather than going silent).
AUDIO_OUTPUT_ALLOWED = re.compile(r"^[A-Za-z0-9_.:,\- ]{1,64}$")


def _validate_audio_output(settings: Dict[str, Any]) -> None:
    """Normalise and validate `audio.output` in place; raise ValueError on a bad shape.

    POST /api/settings takes arbitrary JSON and merges ONE level deep, which means it REPLACES this
    whole sub-dict wholesale — and it never went through audio_api's find_device/404/409 checks. So
    an arbitrary string could become the spec the player hands to PortAudio, and now also the input
    the output gate reads to decide whether this unit runs a player at all. Same chokepoint argument
    as _sanitize_device_names: this is the only place both write paths share.
    """
    audio = settings.get("audio")
    if not isinstance(audio, dict):
        return
    output = audio.get("output")
    if not isinstance(output, dict):
        audio["output"] = {"device": None, "device_type": None}
        return

    device = output.get("device")
    if device is None:
        return
    if not isinstance(device, str):
        raise ValueError("audio.output.device must be a string or null")

    stripped = device.strip()
    if stripped in LEGACY_OUTPUT_PLACEHOLDERS:
        # Same meaning as _migrate_audio_output gives them: unset. Handled here too so a POST cannot
        # write a placeholder back in between migrations.
        output["device"] = None
        output["device_type"] = None
        return
    if audio_devices.is_no_output(stripped):
        # Normalise to the canonical spelling. NOTE: "none" must never join
        # LEGACY_OUTPUT_PLACEHOLDERS — _migrate_audio_output would then clear it on every read, and
        # a unit deliberately set to no output would silently grow a player back at its next start.
        output["device"] = audio_devices.NO_OUTPUT
        output["device_type"] = audio_devices.DeviceType.NONE.value
        return
    if stripped.lower().startswith("hw:") and stripped not in LEGACY_OUTPUT_PLACEHOLDERS:
        # Refused rather than silently accepted — see _migrate_audio_output. The picker stores
        # `<card_name>:<device>`; anything writing an ALSA address here is either a hand-edit or a
        # caller that has not been told card numbers move.
        raise ValueError(
            f"audio.output.device must be a stable '<card_name>:<device>' id, not an ALSA address: {device!r}"
        )
    if not AUDIO_OUTPUT_ALLOWED.match(stripped):
        raise ValueError(f"audio.output.device is not a valid device spec: {device!r}")
    output["device"] = stripped


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

        The temp name must be UNIQUE, not `<file>.tmp`: os.replace is atomic, but two threads
        sharing one temp path are not: B truncates A's half-written buffer, then both rename, and
        the survivor is a splice of two JSON documents. mkstemp in the same directory keeps the
        rename atomic (same filesystem) while giving each writer its own file.
        """
        fd, tmp_path = tempfile.mkstemp(
            dir=os.path.dirname(self.settings_file) or ".", prefix=".settings-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w") as f:
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
        finally:
            # A failure before the rename leaves the temp behind; mkstemp names are unique, so
            # without this every failed save leaks a file into /data.
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

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
        # `isinstance(device, str)` is load-bearing: the shipped default is device=None, and
        # `(None or "").strip()` is "", which is IN the placeholder set. Without the type guard this
        # migration reports "changed" on every call for every unit that has not picked an output —
        # so get_settings() persisted the file on every read, and the GUI polls it every 10s per tab.
        device = output.get("device")
        if isinstance(device, str) and device.strip() in LEGACY_OUTPUT_PLACEHOLDERS:
            output["device"] = None
            output["device_type"] = None
            changed = True
        elif isinstance(device, str) and device.strip().lower().startswith("hw:"):
            # An `hw:C,D` address is not an identity, it is a position — and card numbers move (the
            # HAT on .7.204 has been 2, 1, 2, 0 and 3). Left stored, find_device's address pass
            # resolves it against TODAY's numbering and returns whatever card holds that slot now:
            # measured, `hw:2,0` gave sndrpihifiberry:0 before a reboot and vc4hdmi1:0 after. Worse,
            # the next write through set_output_device canonicalises that wrong card's STABLE id
            # into this file permanently, at which point it is indistinguishable from a real choice.
            #
            # Clearing it falls back to PLUM_DAC_DEVICE, which is the documented "never chosen"
            # behaviour and is honest. Only reachable via a hand-edit or POST /api/settings — the
            # picker has always stored `<card_name>:<device>`.
            logger.warning(
                "clearing stored output %r: an ALSA address is not a stable identity (card numbers move)",
                device,
            )
            output["device"] = None
            output["device_type"] = None
            changed = True
        if "fallback_device" in output:
            output.pop("fallback_device")
            changed = True
        if changed:
            logger.info("cleared the pre-picker audio output placeholder")
        return changed

    def _load_merged(self) -> Dict[str, Any]:
        """Load + merge + migrate, RAISING if the file exists but cannot be read.

        Split out from get_settings() precisely so a write path can tell "no file yet" (defaults
        are correct) from "the file is damaged or unreadable" (defaults are catastrophic). See
        update_settings for what the difference costs.
        """
        try:
            with open(self.settings_file, "r") as f:
                settings = json.load(f)
        except FileNotFoundError:
            # Genuinely absent is not damage: _ensure_settings_file creates it from these defaults.
            logger.warning("Settings file not found, using defaults")
            return copy.deepcopy(DEFAULT_SETTINGS)

        try:
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
        except Exception as e:
            logger.error(f"Failed to load settings: {e}")
            raise

    def get_settings(self) -> Dict[str, Any]:
        """Load settings, merged over defaults so new keys always exist. Never raises.

        A damaged file reads as defaults so the GUI still renders something. Do NOT call this from
        a write path — use _load_merged, which raises. See update_settings.
        """
        with _SETTINGS_LOCK:
            try:
                return self._load_merged()
            except Exception:
                return copy.deepcopy(DEFAULT_SETTINGS)

    def update_settings(self, new_settings: Dict[str, Any]) -> Dict[str, Any]:
        """Update settings (partial or full), deep-merging one level and bumping the version.

        Reads through _load_merged, NOT get_settings: get_settings answers a damaged file with
        DEFAULT_SETTINGS, and building a write on that reply replaces the unit's entire
        configuration — every AirPlay/Spotify/Bluetooth endpoint, the device name, the chosen
        output — with defaults plus the caller's patch, then persists it with a bumped version so
        the GUI's poller sees nothing to reconcile. Failing the request is the only safe answer.
        """
        with _SETTINGS_LOCK:
            current = self._load_merged()

            for key in new_settings:
                if key == "version":
                    continue  # version is server-owned
                if key in current and isinstance(current[key], dict) and isinstance(new_settings[key], dict):
                    current[key].update(new_settings[key])
                else:
                    current[key] = new_settings[key]

            _sanitize_device_names(current)
            _validate_audio_output(current)

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
        except ValueError as e:
            # A rejected value is the caller's fault, not ours — _validate_audio_output raises this.
            # Must be caught BEFORE the generic handler, or a bad device spec reads as a 500.
            logger.warning(f"Rejected a settings update: {e}")
            return jsonify({"error": str(e)}), 400
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
