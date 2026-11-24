#!/usr/bin/env python3
"""
Plum-Audio — the checks every source endpoint must pass before it reaches a path or a daemon.

An endpoint record comes from settings.json, and `POST /api/settings` writes arbitrary JSON into
that file. Three of its fields go somewhere a string must not be free text:

* `id` names a directory (`os.path.join(config_root, id)`, then `os.makedirs`), names a FIFO in
  /tmp, and lands UNESCAPED in the shairport and go-librespot templates.
* the ports land unescaped in those same templates.
* the Bluetooth `adapter` is an argument to bluealsa (`-i <adapter>`) and part of a D-Bus path.

The rule has one home, and two callers apply it. The settings API refuses a bad record with 400
and writes nothing. Each `<source>_config._instance()` applies it again, because the file can
already hold a bad record, and the renderers read the file and not the API. A renderer SKIPS a
bad endpoint with a warning. It does not repair the value: a repaired id would name a different
directory from the one the GUI shows.

No Flask import and no settings import: the audio process loads this module.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger("plum.endpoint_config")

# What real ids contain. `EndpointsManager._next_id` and DEFAULT_SETTINGS make decimal strings
# ("1", "2"). Letters, `_` and `-` are allowed for a hand-written file. The first character is
# alphanumeric, so an id is never an option to a daemon and never `.` or `..`.
INSTANCE_ID_ALLOWED = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")
ADAPTER_ALLOWED = re.compile(r"hci[0-9]{1,3}")

# Unprivileged ports only. Every default is above 1024 (RAOP 5050+, UDP 6001+, zeroconf 5354+).
PORT_MIN = 1024
PORT_MAX = 65535

SPOTIFY_BITRATES = (96, 160, 320)


class InvalidEndpoint(ValueError):
    """An endpoint record holds a value that must not reach a path, a template or an argv."""


def checked_id(value: object) -> str:
    """The endpoint id as the string every sink uses, or InvalidEndpoint.

    Returns the checked value, and the caller must use the RETURN value. Checking `endpoint["id"]`
    and then reading `endpoint["id"]` again proves nothing about what reaches the kernel.
    """
    # bool is an int subclass, and str(True) would pass the pattern.
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise InvalidEndpoint(f"endpoint id must be a string, not {type(value).__name__}")
    text = str(value)
    # fullmatch, not match with `$`: `$` also matches before a trailing newline.
    if not INSTANCE_ID_ALLOWED.fullmatch(text):
        raise InvalidEndpoint(f"endpoint id {text[:40]!r} is not valid (letters, digits, _ and -, 32 maximum)")
    return text


def checked_port(value: object, field: str) -> int:
    """A port as an int in the unprivileged range, or InvalidEndpoint. `field` names it in the error."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise InvalidEndpoint(f"{field} must be an integer")
    if isinstance(value, str) and not re.fullmatch(r"[0-9]{1,5}", value):
        raise InvalidEndpoint(f"{field} must be an integer")
    port = int(value)
    if not PORT_MIN <= port <= PORT_MAX:
        raise InvalidEndpoint(f"{field} must be between {PORT_MIN} and {PORT_MAX}")
    return port


def checked_adapter(value: object) -> str:
    """A BlueZ adapter name (`hci0`), or InvalidEndpoint."""
    if not isinstance(value, str) or not ADAPTER_ALLOWED.fullmatch(value):
        raise InvalidEndpoint(f"adapter {str(value)[:40]!r} is not valid (hci0, hci1, ...)")
    return value


def checked_name(value: object, fallback: str) -> str:
    """The device name if it is a string, else the fallback. The renderers escape it afterwards."""
    return value if isinstance(value, str) and value else fallback


def validate_section(source: str, section: object) -> None:
    """Check one `integrations.<source>` section as the API received it. Raises InvalidEndpoint.

    Checks SHAPE only. An absent optional field is valid, because the renderers derive it.
    """
    if not isinstance(section, dict):
        return  # `snapcast: false`, `visualizer: true`: not an endpoint section
    endpoints = section.get("endpoints")
    if endpoints is None:
        return
    if not isinstance(endpoints, list):
        raise InvalidEndpoint(f"integrations.{source}.endpoints must be a list")
    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            raise InvalidEndpoint(f"integrations.{source}.endpoints must hold objects")
        checked_id(endpoint.get("id"))
        if source == "airplay":
            for field in ("port", "udpPortBase"):
                if endpoint.get(field) is not None:
                    checked_port(endpoint[field], field)
        elif source == "spotify":
            if endpoint.get("zeroconfPort") is not None:
                checked_port(endpoint["zeroconfPort"], "zeroconfPort")
        elif source == "bluetooth" and endpoint.get("adapter") is not None:
            checked_adapter(endpoint["adapter"])
    if source == "spotify" and "bitrate" in section:
        bitrate = section["bitrate"]
        if isinstance(bitrate, bool) or bitrate not in SPOTIFY_BITRATES:
            raise InvalidEndpoint(f"spotify bitrate must be one of {SPOTIFY_BITRATES}")


# The managers resolve the endpoints on every poll. Without this set, one bad record writes the
# same warning every few seconds for as long as it stays in the file.
# The set is capped. Each distinct bad record adds a key, and a caller that writes a new bad record
# on every request would grow it without limit. At the cap it starts again: the cost is one more
# warning for a record that is still bad, and that is the smaller fault.
_warned: set[tuple[str, str]] = set()
_WARNED_MAX = 64


def resolve(source: str, endpoints: list, build) -> list:
    """Build an instance for each endpoint. Skip, with one warning, each one that is not valid."""
    instances = []
    for endpoint in endpoints:
        try:
            if not isinstance(endpoint, dict):
                raise InvalidEndpoint("endpoint is not an object")
            instances.append(build(endpoint))
        except InvalidEndpoint as exc:
            key = (source, repr(endpoint)[:200])
            if key not in _warned:
                if len(_warned) >= _WARNED_MAX:
                    _warned.clear()
                _warned.add(key)
                logger.warning("skipping %s endpoint: %s", source, exc)
    return instances
