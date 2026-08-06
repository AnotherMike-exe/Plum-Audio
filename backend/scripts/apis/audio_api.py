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
    active = None if audio_devices.is_no_output(playing_on or spec) else (playing_on or spec)
    devices = audio_devices.list_output_devices(active_spec=active)
    device = audio_devices.find_device(spec, devices) if spec else None

    # Compare RESOLVED identities, never the raw strings. `spec` is whatever named the output —
    # PLUM_DAC_DEVICE's PortAudio name fragment ("bcm2835"), an `hw:C,D`, a bare card name, or a
    # `<card_name>:<device>` id — while the player's echo is ALWAYS the resolved id. So string
    # equality only ever held for a choice made in the GUI, which writes the id form.
    #
    # A unit still on its PLUM_DAC_DEVICE default therefore reported a switch pending FOREVER, to the
    # very device it was already playing on: `resolved: true`, `is_active: true`,
    # `playing_on == id == "Headphones:0"`, and `pending: true` beside it. The tab rendered "switching
    # to Built-in Headphones" next to that same device's "playing" tag. Seen on both mesh-pair units after
    # the greenfield alpha, 2026-08-06 — and invisible to every test here, because they all configure
    # an id rather than a fragment.
    #
    # `device` is already `find_device(spec, devices)`, i.e. the one thing that knows how to turn any
    # of those spellings into an id. When it resolves to nothing (a HAT removed, a spec from another
    # unit) there is no id to compare against, so fall back to the raw spec — it will differ, which is
    # the honest answer for a configuration that names a device this unit does not have.
    if audio_devices.is_no_output(spec):  # noqa: SIM108 - the ternary form nests two conditionals; less readable
        target = audio_devices.NO_OUTPUT
    else:
        target = device.id if device is not None else spec
    pending = playing_on is not None and playing_on != target
    # Crossing the none<->device boundary is the ONLY change that needs a restart: the player is a
    # process that either exists or does not, decided once by output_gate.py at container start. A
    # device->device switch still applies live through watch_output_device and must keep reading
    # exactly as it does today. Guarded on `playing_on is not None` like `pending`, so a missing echo
    # reads as "unknown" rather than shouting "restart" at a unit whose player simply has not
    # reported yet.
    restart_required = playing_on is not None and audio_devices.is_no_output(spec) != audio_devices.is_no_output(
        playing_on
    )

    if audio_devices.is_no_output(spec):
        # Deliberately nothing — not a device that went missing. Answered before the fallback below,
        # which would otherwise render this as "Not present ('none')".
        return {
            "configured": audio_devices.NO_OUTPUT,
            "resolved": True,
            "playing_on": playing_on,
            "pending": pending,
            "restart_required": restart_required,
            **audio_devices.no_output_device(is_active=True),
        }

    if device is not None:
        return {
            "configured": spec,
            "resolved": True,
            "playing_on": playing_on,
            "pending": pending,
            "restart_required": restart_required,
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
        "restart_required": restart_required,
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
            # "No output" is offered on EVERY unit, not only card-less ones — turning any unit into
            # an ingest/routing-only node is a deliberate choice, not just a fallback. Synthesised
            # here rather than in the GUI so the sentinel's spelling has one owner.
            rows = [d.to_dict() for d in devices]
            rows.append(audio_devices.no_output_device(is_active=audio_devices.is_no_output(spec)))
            return jsonify(rows)
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
            if audio_devices.is_no_output(device_id):
                # Short-circuit BEFORE the enumeration below, and therefore before the 404: there is
                # no AudioDevice to find, and asking for one is how this used to fail. Whether this
                # unit actually runs a player is decided at container start by output_gate.py, so
                # the honest answer is "saved, restart to apply".
                playing_on = load_active_output(state_file_path())
                settings_manager.update_settings(
                    {
                        "audio": {
                            "output": {
                                "device": audio_devices.NO_OUTPUT,
                                "device_type": audio_devices.DeviceType.NONE.value,
                            }
                        }
                    }
                )
                logger.info("output set to NO OUTPUT — this unit will run no player after a restart")
                restart_required = playing_on is None or not audio_devices.is_no_output(playing_on)
                return jsonify(
                    {
                        "success": True,
                        "pending": True,
                        "restart_required": restart_required,
                        "message": "Saved. This unit will start with no output after a restart.",
                        "device": audio_devices.no_output_device(is_active=True),
                    }
                )

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

            # Coming BACK from no-output needs a restart for the same reason leaving did: there is
            # no player process to hand the new device to. Read the echo before the write.
            restart_required = audio_devices.is_no_output(load_active_output(state_file_path()))

            # Store the STABLE id, never the hw address — card numbers move across reboots.
            settings_manager.update_settings(
                {"audio": {"output": {"device": device.id, "device_type": device.type.value}}}
            )
            logger.info("output device set to %s (%s)", device.id, device.friendly_name)

            return jsonify(
                {
                    "success": True,
                    "pending": True,  # saved; the player applies it on its next poll
                    "restart_required": restart_required,
                    "message": (
                        f"Saved. Restart this unit to start playing through {device.friendly_name}."
                        if restart_required
                        else f"Output set to {device.friendly_name}"
                    ),
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

        if audio_devices.is_no_output(device_id):
            return jsonify({"success": False, "message": "There is no output to test on this unit."}), 409

        try:
            ok, message = audio_devices.test_device(device_id, active_spec=audio_devices.configured_output_spec())
            return jsonify({"success": ok, "message": message}), (200 if ok else 409)
        except Exception as exc:  # noqa: BLE001
            logger.error("testing the output device failed: %s", exc, exc_info=True)
            return jsonify({"success": False, "message": str(exc)}), 500

    return bp
