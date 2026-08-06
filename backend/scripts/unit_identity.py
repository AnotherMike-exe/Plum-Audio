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

WATCH_INTERVAL_S = 5.0

# Where the per-unit token comes from, in order. The Pi's SoC serial first because it survives a NIC
# swap, a reflash and every reboot — a MAC chosen by default route does not, and a token that moves
# would RENAME the unit, which is the whole thing this exists to prevent. Both are visible inside the
# container: /proc/cpuinfo comes from the host kernel and host networking exposes the host's
# interfaces. Overridable so a test can pin it.
_CPUINFO = "/proc/cpuinfo"
_NET_DIR = "/sys/class/net"
_TOKEN_LEN = 4


def host_token(length: int = _TOKEN_LEN) -> str:
    """A short, stable, per-unit hex token. Empty string when nothing identifying can be read.

    Never raises and never guesses: an unreadable host gets "", and callers fall back to the bare
    name rather than inventing an identity that would change on the next boot.
    """
    raw = ""
    try:
        with open(_CPUINFO, encoding="utf-8") as f:
            for line in f:
                if line.lower().startswith("serial"):
                    raw = line.split(":", 1)[1].strip()
    except (OSError, IndexError):
        pass

    if not raw:
        # Lowest PHYSICAL interface MAC — sorted, so enumeration order cannot change the answer.
        macs = []
        try:
            for iface in os.listdir(_NET_DIR):
                if iface == "lo" or iface.startswith(("docker", "veth", "br-", "dummy")):
                    continue
                if not os.path.exists(os.path.join(_NET_DIR, iface, "device")):
                    continue  # virtual
                try:
                    with open(os.path.join(_NET_DIR, iface, "address"), encoding="utf-8") as f:
                        macs.append(f.read().strip().replace(":", ""))
                except OSError:
                    continue
        except OSError:
            pass
        macs = sorted(m for m in macs if m and m != "000000000000")
        raw = macs[0] if macs else ""

    hexed = "".join(c for c in raw if c in "0123456789abcdefABCDEF")
    return hexed[-length:].upper() if hexed else ""


def default_device_name(base: str = "Plum Sendspin") -> str:
    """`base`, made unique to this unit when a token can be derived.

    An unnamed unit used to boot as the bare `base` on EVERY unit at once, so a rig brought up without
    PLUM_UNIT_NAME — a hand-run `docker compose up`, or any deploy path that is not docker/deploy.sh —
    presented several identical units to the mesh view, the GUI cards, mDNS and to an AirPlay sender.
    deploy.sh disambiguates what units.conf duplicates, but it cannot help a unit it never touched;
    this is the floor under that.
    """
    token = host_token()
    return f"{base} {token}" if token else base


# Kept as a module constant because sendspin_server/sendspin_player read it directly. Evaluated once
# at import, which is correct: neither the SoC serial nor a physical MAC changes while we run.
DEFAULT_DEVICE_NAME = default_device_name()


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
