#!/usr/bin/env python3
"""
Plum-Audio — the loudness-calibration REST surface (Flask, port 5002, at /api/audio/calibration).

PERSISTENCE ONLY, like its neighbour audio_api. Saving a curve writes settings.json and returns; it
does not move a speaker, and it cannot -- this process is not the audio loop. The matcher runs in
the audio process and picks the change up from settings.json on its next tick, exactly as the output
picker's device switch does. Playing the calibration TONE is the other half and lives on the mesh
API (:5001), because only that process can create a source and route a player.

Records are keyed by mesh player id -- the X25519 peer id, which is what `POST /api/mesh/volume`
takes and what the GUI's `Client.id` already is. Not the listener id, not the mDNS instance name,
and not the URL: a speaker has two names depending on whether it is attached or idle, and its URL
moves with DHCP. `name` and `url` are carried inside the record as denormalised display copies so
the GUI can still label a calibration for an endpoint that is currently offline.

Every write goes through SettingsManager.mutate, never get-then-post. The map is keyed by id, so a
blind patch would let two browsers calibrating two speakers each read the same map and the second
drop the first -- with a bumped version, so no poller would ever notice. See mutate's docstring.
"""

from __future__ import annotations

import logging
import math
import os
import sys
from datetime import UTC, datetime
from typing import Any

from flask import Blueprint, jsonify, request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # scripts/ on path
import calibration as cal_model  # noqa: E402
from settings_api import SettingsManager  # noqa: E402

logger = logging.getLogger(__name__)

# Bounds on what a user may type. A phone SPL meter reads roughly 30-120 dB; the wider band accepts
# meters reporting dBFS-style negatives without accepting a typo of several orders of magnitude.
MIN_MEASURED_DB = -60.0
MAX_MEASURED_DB = 160.0
MAX_TRIM_DB = 30.0
MAX_NAME_LEN = 120


class ValidationError(ValueError):
    """A bad request body. Carried to a 400 rather than a 500."""


def _clean_name(value: object) -> str:
    """Display-only, but still bounded: control characters and unbounded length are never wanted."""
    text = "" if value is None else str(value)
    text = "".join(ch for ch in text if ch.isprintable())
    return text.strip()[:MAX_NAME_LEN]


def _parse_samples(raw: object) -> list[cal_model.Sample]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValidationError("samples must be a list")
    if len(raw) > cal_model.MAX_SAMPLES:
        raise ValidationError(f"at most {cal_model.MAX_SAMPLES} samples")

    samples: list[cal_model.Sample] = []
    for row in raw:
        if not isinstance(row, dict):
            raise ValidationError("each sample must be an object")
        try:
            volume = float(row["volume"])
            db = float(row["db"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError("each sample needs a numeric volume and db") from exc
        if not math.isfinite(volume) or not 0.0 <= volume <= 100.0:
            raise ValidationError("sample volume must be 0-100")
        if not math.isfinite(db) or not MIN_MEASURED_DB <= db <= MAX_MEASURED_DB:
            raise ValidationError(f"sample db must be {MIN_MEASURED_DB:.0f}-{MAX_MEASURED_DB:.0f}")
        samples.append(cal_model.Sample(volume=volume, db=db))
    return samples


def _parse_max_limit(raw: object) -> cal_model.MaxLimit:
    if raw is None:
        return cal_model.MaxLimit()
    if not isinstance(raw, dict):
        raise ValidationError("maxLimit must be an object")
    mode = raw.get("mode", "percentage")
    if mode not in ("percentage", "decibel"):
        raise ValidationError("maxLimit.mode must be 'percentage' or 'decibel'")
    try:
        value = float(raw.get("value", 100.0))
    except (TypeError, ValueError) as exc:
        raise ValidationError("maxLimit.value must be numeric") from exc
    if not math.isfinite(value):
        raise ValidationError("maxLimit.value must be numeric")
    if mode == "percentage" and not 0.0 <= value <= 100.0:
        raise ValidationError("a percentage ceiling must be 0-100")
    if mode == "decibel" and not MIN_MEASURED_DB <= value <= MAX_MEASURED_DB:
        raise ValidationError("a decibel ceiling must be a plausible SPL")
    return cal_model.MaxLimit(mode=mode, value=value)


def _parse_trim(raw: object) -> float:
    if raw is None:
        return 0.0
    try:
        trim = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError("trimDb must be numeric") from exc
    if not math.isfinite(trim) or abs(trim) > MAX_TRIM_DB:
        raise ValidationError(f"trimDb must be within +/-{MAX_TRIM_DB:.0f} dB")
    return trim


def _parse_record(body: dict) -> cal_model.EndpointCalibration:
    url = body.get("url")
    return cal_model.EndpointCalibration(
        name=_clean_name(body.get("name")),
        url=url if isinstance(url, str) and url else None,
        enabled=bool(body.get("enabled", True)),
        samples=_parse_samples(body.get("samples")),
        max_limit=_parse_max_limit(body.get("maxLimit")),
        trim_db=_parse_trim(body.get("trimDb")),
        last_calibrated=datetime.now(UTC).isoformat(timespec="seconds"),
    )


def _parse_policy(body: dict) -> cal_model.MatchPolicy:
    mode = body.get("mode", cal_model.DEFAULT_MATCH_MODE)
    if mode not in cal_model.MATCH_MODES:
        raise ValidationError(f"mode must be one of {', '.join(cal_model.MATCH_MODES)}")

    raw_sets = body.get("sets", [])
    if not isinstance(raw_sets, list):
        raise ValidationError("sets must be a list")

    sets: list[cal_model.MatchSet] = []
    seen: set[str] = set()
    for row in raw_sets:
        if not isinstance(row, dict):
            raise ValidationError("each set must be an object")
        set_id = row.get("id")
        if not isinstance(set_id, str) or not set_id.strip():
            raise ValidationError("each set needs a non-empty id")
        set_id = set_id.strip()
        if set_id in seen:
            raise ValidationError(f"duplicate set id {set_id!r}")
        seen.add(set_id)
        raw_members = row.get("members", [])
        if not isinstance(raw_members, list):
            raise ValidationError("set members must be a list")
        members = [m for m in raw_members if isinstance(m, str) and m]
        sets.append(cal_model.MatchSet(id=set_id, name=_clean_name(row.get("name")), members=members))

    return cal_model.MatchPolicy(mode=mode, sets=sets)


def _describe(player_id: str, cal: cal_model.EndpointCalibration) -> dict[str, Any]:
    """Delegates to calibration.describe so this surface and the mesh's merged view agree.

    They must: a record served without its derived half renders as "Not calibrated" in the GUI
    while the matcher is driving that very speaker.
    """
    return cal_model.describe(player_id, cal)


def _audio_section(settings: dict[str, Any]) -> dict[str, Any]:
    audio = settings.get("audio")
    if not isinstance(audio, dict):
        audio = {}
        settings["audio"] = audio
    return audio


def _snapshot(settings_manager: SettingsManager) -> dict[str, Any]:
    settings = settings_manager.get_settings()
    audio = settings.get("audio") if isinstance(settings.get("audio"), dict) else {}
    calibrations = cal_model.load_calibrations(audio.get("calibration"))
    policy = cal_model.MatchPolicy.from_dict(audio.get("loudnessMatch"))
    return {
        "calibrations": {pid: _describe(pid, c) for pid, c in calibrations.items()},
        "policy": policy.to_dict(),
        "modes": list(cal_model.MATCH_MODES),
        "suggestedVolumes": list(cal_model.SUGGESTED_SAMPLE_VOLUMES),
        "minSamples": cal_model.MIN_SAMPLES,
        "maxSamples": cal_model.MAX_SAMPLES,
    }


def create_calibration_blueprint(settings_manager: SettingsManager) -> Blueprint:
    bp = Blueprint("calibration", __name__, url_prefix="/api/audio/calibration")

    @bp.route("", methods=["GET"])
    @bp.route("/", methods=["GET"])
    def get_all():
        try:
            return jsonify(_snapshot(settings_manager))
        except Exception as e:  # noqa: BLE001 - a read failure must not 500 the whole tab
            logger.error(f"Failed to read calibrations: {e}")
            return jsonify({"error": str(e)}), 500

    @bp.route("/policy", methods=["PUT"])
    def put_policy():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "a JSON object is required"}), 400
        try:
            policy = _parse_policy(body)
        except ValidationError as e:
            return jsonify({"error": str(e)}), 400

        def apply(settings: dict[str, Any]) -> bool:
            _audio_section(settings)["loudnessMatch"] = policy.to_dict()
            return True

        try:
            settings_manager.mutate(apply)
        except Exception as e:  # noqa: BLE001 - surface the write failure, never a stack trace
            logger.error(f"Failed to save match policy: {e}")
            return jsonify({"error": str(e)}), 500
        logger.info(f"Loudness match policy set to {policy.mode} ({len(policy.sets)} set(s))")
        return jsonify(_snapshot(settings_manager))

    @bp.route("/<player_id>", methods=["PUT"])
    def put_one(player_id: str):
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "a JSON object is required"}), 400
        try:
            record = _parse_record(body)
        except ValidationError as e:
            return jsonify({"error": str(e)}), 400

        # Refuse measurements that cannot describe a speaker rather than storing them for the
        # matcher to reject silently later. The wizard needs to tell the user WHY at the moment
        # they press Save — a flat or inverted response means the tone was not coming from the
        # endpoint being measured, which is the predecessor's exact failure and is recoverable
        # only if it is reported.
        if record.samples and not record.calibrated:
            return (
                jsonify(
                    {
                        "error": (
                            "these measurements do not describe a speaker: loudness must rise with "
                            "volume. Check the tone was playing from this endpoint and that the "
                            "room was otherwise quiet."
                        ),
                        "fitRejected": True,
                    }
                ),
                400,
            )

        def apply(settings: dict[str, Any]) -> bool:
            audio = _audio_section(settings)
            existing = audio.get("calibration")
            table = dict(existing) if isinstance(existing, dict) else {}
            table[player_id] = record.to_dict()
            audio["calibration"] = table
            return True

        try:
            settings_manager.mutate(apply)
        except Exception as e:  # noqa: BLE001 - surface the write failure, never a stack trace
            logger.error(f"Failed to save calibration for {player_id}: {e}")
            return jsonify({"error": str(e)}), 500

        logger.info(f"Calibration saved for {player_id} ({len(record.samples)} sample(s))")
        return jsonify(_describe(player_id, record))

    @bp.route("/<player_id>", methods=["DELETE"])
    def delete_one(player_id: str):
        removed = False

        def apply(settings: dict[str, Any]) -> bool:
            nonlocal removed
            audio = _audio_section(settings)
            existing = audio.get("calibration")
            if not isinstance(existing, dict) or player_id not in existing:
                return False  # no-op: skip the save and the version bump
            table = dict(existing)
            table.pop(player_id)
            audio["calibration"] = table
            removed = True
            return True

        try:
            settings_manager.mutate(apply)
        except Exception as e:  # noqa: BLE001 - surface the write failure, never a stack trace
            logger.error(f"Failed to delete calibration for {player_id}: {e}")
            return jsonify({"error": str(e)}), 500

        if not removed:
            return jsonify({"error": "no calibration for that endpoint"}), 404
        logger.info(f"Calibration cleared for {player_id}")
        return jsonify(_snapshot(settings_manager))

    return bp
