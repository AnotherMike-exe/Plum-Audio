#!/usr/bin/env python3
"""
Plum-Audio — the speaker's own persisted render state (volume + mute).

Kept out of ``settings.json`` deliberately: that file is owned by the Flask config API in a
different process, and this is runtime state the PLAYER is the source of truth for, not user
configuration. A separate file means no concurrent writers and no round-trip through an API just to
remember how loud the room was.

Why it has to persist at all: a Sendspin server cannot tell a player what its level is — it only
sends commands. The level a player reports in ``client/state`` at connect is what the whole mesh
(and any third-party server, e.g. Music Assistant) then believes. Without this, every restart would
silently return the room to 100%.

Its own module rather than living in ``sendspin_player`` so it is testable without numpy/PortAudio/
aiosendspin present (see tests/Unit/test_player_state.py).
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger("plum.player_state")

DEFAULT_PLAYER_STATE_FILE = "/data/player_state.json"
STATE_SAVE_DEBOUNCE_S = 2.0  # coalesce a slider drag into a single write


def state_file_path() -> str:
    return os.environ.get("PLUM_PLAYER_STATE_FILE", DEFAULT_PLAYER_STATE_FILE)


def _read(path: str) -> dict:
    """The persisted state, or {} for anything missing/unreadable/malformed."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:  # noqa: BLE001 - malformed/unreadable: fall back rather than fail to start
        logger.warning("player state %s unreadable — falling back to defaults", path)
        return {}


def _merge_write(path: str, changes: dict) -> None:
    """Atomically update SOME keys, leaving the rest alone.

    Read-modify-write rather than a plain dump: volume/mute and the active output are written on
    different schedules by different code paths, and a whole-file write from either would silently
    drop the other. A speaker that forgot its level on a device switch would look exactly like the
    missing-echo bug this file exists to prevent.
    """
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        data = _read(path)
        data.update(changes)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 - persistence is a convenience; never break rendering over it
        logger.debug("could not persist player state to %s", path, exc_info=True)


def load_render_state(path: str, *, default_volume: int, default_muted: bool = False) -> tuple[int, bool]:
    """Read the persisted volume/mute, falling back to the caller's defaults.

    Anything missing, unreadable or malformed is a fallback, not an error: a speaker that cannot read
    its last level must still come up and play, just at the default.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        volume = int(data.get("volume", default_volume))
        muted = bool(data.get("muted", default_muted))
        return max(0, min(100, volume)), muted
    except FileNotFoundError:
        return default_volume, default_muted
    except Exception:  # noqa: BLE001 - malformed/unreadable: fall back rather than fail to start
        logger.warning("player state %s unreadable — falling back to volume=%d", path, default_volume)
        return default_volume, default_muted


def save_render_state(path: str, volume: int, muted: bool) -> None:
    """Persist volume/mute atomically (tmp + rename), best-effort."""
    _merge_write(path, {"volume": int(volume), "muted": bool(muted)})


def load_active_output(path: str) -> str | None:
    """The output spec the player last reported having OPEN, or None if it never has."""
    output = _read(path).get("output_device")
    return str(output) if output else None


def save_active_output(path: str, spec: str | None) -> None:
    """Record which output the player actually has open.

    This is the echo, and it is here for the same reason the volume echo is: the config API writes
    the user's CHOICE into settings.json, but only the player knows whether that choice ever became
    audible. Without it the GUI would mark a switch applied the instant it was saved — including
    when the device turned out not to open — which is the failure this slice exists to prevent.
    """
    _merge_write(path, {"output_device": spec})
