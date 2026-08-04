#!/usr/bin/env python3
"""
Plum-Audio — the audio output REST surface (Flask, port 5002, proxied at /api/audio/).

PERSISTENCE ONLY. Choosing an output writes settings.json and returns; nothing here restarts a
process or reaches into the audio loop. That is not a simplification, it is the rule this project
already paid for twice: the config API runs in a different process from the player, so it cannot
touch the PortAudio stream, and `supervisorctl` from an API is the render-config-then-respool
pattern that CLAUDE.md bans (the dev rig has no supervisord at all). The player watches
settings.json and applies the change itself — see sendspin_player, Phase 3.

The consequence worth stating plainly: a 200 from POST /output/device means SAVED, not YET PLAYING.
The GUI must show the switch as pending until the device reports back as active, or it will claim
success a second or two before the audio actually moves — and claim it too even if the new device
turns out not to open, which is the failure this whole slice is trying to avoid.

Snapcast's `/api/audio/input/*` endpoints are deliberately absent. A capture device is not a setting
here; it is a line-in SOURCE, which means a source manager, a FIFO and a PushStream feeder. That is
a Phase-3 source slice of its own, not a checkbox on this tab.
"""

from __future__ import annotations

import logging
import os
import sys

from flask import Blueprint, jsonify, request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # scripts/ on path
import audio_devices  # noqa: E402
from player_state import load_active_output, state_file_path  # noqa: E402

from settings_api import SettingsManager  # noqa: E402

logger = logging.getLogger(__name__)


def _current_output(settings_manager: SettingsManager) -> dict:
    """The configured output, resolved against what is actually present right now.

    `pending` compares the CHOICE (settings.json) against the player's ECHO of what it has open
    (player_state.json). They differ for the second or two before the player's next settings poll,
    and they stay different when the new device would not open — which is precisely the case the GUI
    must not render as success. Same reasoning as the volume echo: only the player knows what is
    actually audible, so the API reports its word, not its own assumption.
    """
    spec = audio_devices.configured_output_spec()
    playing_on = load_active_output(state_file_path())
    devices = audio_devices.list_output_devices(active_spec=playing_on or spec)
    device = audio_devices.find_device(spec, devices) if spec else None

    pending = playing_on is not None and playing_on != spec

    if device is not None:
        return {
            "configured": spec,
            "resolved": True,
            "playing_on": playing_on,
            "pending": pending,
            **device.to_dict(),
        }

    # Configured but missing: a HAT removed, a USB DAC unplugged, or a spec from another unit. Say
    # so rather than substituting a device that happens to work — the user needs to know the box is
    # not playing where they think it is.
    return {
        "configured": spec,
        "resolved": False,
        "playing_on": playing_on,
        "pending": pending,
        "id": None,
        "friendly_name": f"Not present ({spec})" if spec else "No output selected",
        "is_available": False,
        "is_active": False,
        "unavailable_reason": (f"The configured output {spec!r} is not attached to this unit." if spec else None),
    }


def create_audio_blueprint(settings_manager: SettingsManager = None) -> Blueprint:
    settings_manager = settings_manager or SettingsManager()
    bp = Blueprint("audio", __name__)

    @bp.route("/api/audio/devices/output", methods=["GET"])
    def get_output_devices():
        try:
            spec = audio_devices.configured_output_spec()
            devices = audio_devices.list_output_devices(active_spec=spec)
            return jsonify([d.to_dict() for d in devices])
        except Exception as exc:  # noqa: BLE001 - a discovery failure must not 500 the whole tab
            logger.error("listing output devices failed: %s", exc, exc_info=True)
            return jsonify({"error": str(exc)}), 500

    @bp.route("/api/audio/output/current", methods=["GET"])
    def get_current_output():
        try:
            return jsonify(_current_output(settings_manager))
        except Exception as exc:  # noqa: BLE001
            logger.error("reading the current output failed: %s", exc, exc_info=True)
            return jsonify({"error": str(exc)}), 500

    @bp.route("/api/audio/output/device", methods=["POST"])
    def set_output_device():
        """Persist a new output. The player picks it up on its next settings poll."""
        data = request.get_json(silent=True) or {}
        device_id = (data.get("id") or data.get("hw_id") or "").strip()
        if not device_id:
            return jsonify({"success": False, "error": "id is required"}), 400

        try:
            devices = audio_devices.list_output_devices(active_spec=audio_devices.configured_output_spec())
            device = audio_devices.find_device(device_id, devices)

            if device is None:
                return jsonify({"success": False, "error": f"Device {device_id!r} not found"}), 404
            if not device.is_available:
                # Refuse rather than persist: saving an unopenable device would leave the unit
                # silent after the player's next restart, with settings.json looking perfectly fine.
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": device.unavailable_reason or f"{device.friendly_name} cannot be opened",
                        }
                    ),
                    409,
                )

            # Store the STABLE id, never the hw address — card numbers move across reboots.
            settings_manager.update_settings(
                {"audio": {"output": {"device": device.id, "device_type": device.type.value}}}
            )
            logger.info("output device set to %s (%s)", device.id, device.friendly_name)

            return jsonify(
                {
                    "success": True,
                    "pending": True,  # saved; the player applies it on its next poll
                    "message": f"Output set to {device.friendly_name}",
                    "device": device.to_dict(),
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("setting the output device failed: %s", exc, exc_info=True)
            return jsonify({"success": False, "error": str(exc)}), 500

    @bp.route("/api/audio/output/test", methods=["POST"])
    def test_output_device():
        """Play a test tone, to confirm which speakers are wired to a device before switching."""
        data = request.get_json(silent=True) or {}
        device_id = (data.get("id") or data.get("hw_id") or "").strip()
        if not device_id:
            return jsonify({"success": False, "message": "id is required"}), 400

        try:
            ok, message = audio_devices.test_device(device_id, active_spec=audio_devices.configured_output_spec())
            return jsonify({"success": ok, "message": message}), (200 if ok else 409)
        except Exception as exc:  # noqa: BLE001
            logger.error("testing the output device failed: %s", exc, exc_info=True)
            return jsonify({"success": False, "message": str(exc)}), 500

    return bp
