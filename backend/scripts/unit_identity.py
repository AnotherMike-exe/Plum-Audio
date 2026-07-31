#!/usr/bin/env python3
"""
Plum-Audio — this unit's display name, resolved from settings.json.

The name a user types into Settings ("Kitchen") is what must appear everywhere the unit shows up:
the mesh view, the GUI's unit card, and the mDNS records peers and third-party servers browse. That
was not true before — `sendspin_server.py` and `sendspin_player.py` each took their name from
PLUM_UNIT_NAME / PLUM_PLAYER_NAME and never looked at settings.json, so a rename in the GUI changed
the stored value and nothing else. Per-SOURCE names were always settings-driven (each endpoint's own
deviceName), which is why sources read correctly while the unit and its player did not.

Order of precedence: settings.json `deviceName` > the PLUM_* env var > DEFAULT_DEVICE_NAME. Env
stays meaningful as the value a fresh unit boots with before anyone has named it, and as the
override for a unit deliberately run outside the container.

The Sendspin-level names (the server's `server_name`, the player's client name) are fixed when the
connection is constructed, so a live rename moves the mesh/GUI/mDNS identity immediately and those
catch up on the next restart — deliberately, because restarting the audio process to apply a rename
would interrupt playback.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Awaitable, Callable

logger = logging.getLogger("plum.unit_identity")

DEFAULT_DEVICE_NAME = "Plum Sendspin"
WATCH_INTERVAL_S = 5.0


def settings_path() -> str:
    return os.environ.get("PLUM_SETTINGS_FILE", "/data/settings.json")


def device_name(fallback: str | None = None) -> str:
    """This unit's configured name, or `fallback` when settings.json has none yet.

    Never raises: the audio process must come up and play even if the settings file is missing,
    unreadable, or mid-write (the config API writes it atomically, but a torn read from some other
    writer must not take the unit down).
    """
    try:
        with open(settings_path(), encoding="utf-8") as f:
            configured = (json.load(f).get("deviceName") or "").strip()
        if configured:
            return configured
    except (OSError, ValueError):
        pass
    return fallback or DEFAULT_DEVICE_NAME


async def watch_device_name(
    on_change: Callable[[str], Awaitable[None]],
    *,
    fallback: str | None = None,
    interval: float = WATCH_INTERVAL_S,
) -> None:
    """Poll settings.json and invoke `on_change(new_name)` whenever the device name changes.

    Polling rather than inotify for the same reason the source managers poll it: settings.json is a
    cross-process contract rewritten by a different process, and a rename is not latency-critical.
    A failing callback is logged and retried on the next tick — never allowed to kill the watcher,
    or the first transient Avahi hiccup would freeze the unit's name until a restart.
    """
    current = device_name(fallback)
    while True:
        await asyncio.sleep(interval)
        latest = device_name(fallback)
        if latest == current:
            continue
        logger.info("device name changed: %r -> %r", current, latest)
        current = latest
        try:
            await on_change(latest)
        except Exception:  # noqa: BLE001
            logger.warning("applying the new device name failed; will retry on the next change", exc_info=True)
