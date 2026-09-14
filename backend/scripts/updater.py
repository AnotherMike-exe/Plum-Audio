#!/usr/bin/env python3
"""
Plum-Audio unit updates — the CONTAINER's half of a two-part mechanism.

A container cannot replace itself. The process that would pull a new image runs inside the thing
being replaced, so the pull and the `up -d` have to happen on the HOST. Nothing here holds the
Docker socket, and that is deliberate rather than incidental: the APIs are unauthenticated and bound
to 0.0.0.0 (CLAUDE.md "Open"), so a socket mounted in here would be root on the host for anyone who
can reach the port. This module therefore never runs Docker and never restarts anything.

The shape:

    container            writes /config/update.request        (this module)
    host agent           consumes it, pulls, recreates        (scripts/host-setup/plum-updater.sh)
    host agent           writes  /config/update.state         (read back by this module)

Both files live on the /config bind mount, which is how they cross the boundary at all — and the
STATE file living there is what makes the result survive the container recreate that ends the
request. Every call here returns at once, because by the time a pull finishes this process is gone
and a new one is answering the poll.

A unit whose host has no agent installed still answers every read: `agent.installed` is False and
the GUI says so, rather than offering a button that would write a file nothing ever reads. That is
the state of every unit provisioned before the agent existed.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("plum.updater")

# The bind mount both halves share. PLUM_CONFIG_DIR exists so the unit tests (and a dev run outside
# a container) can point this somewhere writable; in the image it is always /config.
DEFAULT_CONFIG_DIR = "/config"

REQUEST_NAME = "update.request"
STATE_NAME = "update.state"

# A request older than this is stale: the agent consumes one within seconds, so anything still
# sitting here later means the agent is not running (or died mid-run). We report it rather than
# silently leaving a button that appears to do nothing forever.
REQUEST_STALE_S = 600.0

# Channels a request may name. The agent resolves each to a concrete image reference, so the
# container never handles a registry path — which keeps "what may be installed" a host-side
# decision and stops an unauthenticated POST choosing an arbitrary image.
CHANNELS = ("dev", "latest")


class UpdateError(Exception):
    """A request could not be written. The message is safe to show a user."""


def config_dir() -> Path:
    return Path(os.environ.get("PLUM_CONFIG_DIR", DEFAULT_CONFIG_DIR))


def request_path() -> Path:
    return config_dir() / REQUEST_NAME


def state_path() -> Path:
    return config_dir() / STATE_NAME


def _read_json(path: Path) -> dict[str, Any] | None:
    """Read a JSON object, or None. Never raises.

    A half-written file is a real case here, not a hypothetical: the agent writes the state file
    while the container is being recreated, so a poll can land mid-write. Both sides write
    atomically (temp + rename), which makes that a near-miss rather than a guarantee, so a parse
    failure still reads as "no answer yet" instead of a 500 in the GUI.
    """
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.debug("could not read %s: %s", path, exc)
        return None
    return data if isinstance(data, dict) else None


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON via temp + rename, so a reader never sees a partial object."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def running_image() -> dict[str, Any]:
    """What this container believes it is, from the build-time environment.

    Everything here is baked by docker/build.sh and dev.yml at image build time. The container
    cannot read its own image DIGEST without the Docker socket, so that field comes from the agent's
    state file instead (see `status()`), and is absent on a unit with no agent. Reporting the tag
    alone would be worse than useless for `:dev`, which is mutable — two units on `:dev` can be
    weeks apart and both read "dev".
    """
    return {
        "version": os.environ.get("PLUM_APP_VERSION", "0.0.0-dev"),
        "buildType": os.environ.get("PLUM_BUILD_TYPE", "dev"),
        "gitDescribe": os.environ.get("PLUM_GIT_DESCRIBE"),
    }


def _agent_block(state: dict[str, Any] | None) -> dict[str, Any]:
    """Describe the host agent from its own state file."""
    if state is None:
        return {"installed": False, "version": None, "lastSeen": None}
    return {
        "installed": True,
        "version": state.get("agentVersion"),
        "lastSeen": state.get("writtenAt"),
    }


def _pending_block(now: float | None = None) -> dict[str, Any]:
    """Describe an outstanding request, and whether it has gone stale.

    `stale` is the only signal a GUI has that the agent is installed but not running. Without it a
    failed unit is indistinguishable from a slow one, and the operator waits forever.
    """
    request = _read_json(request_path())
    if request is None:
        return {"pending": False, "requestedAt": None, "stale": False}
    requested_at = request.get("requestedAt")
    age = None
    if isinstance(requested_at, (int, float)):
        age = (time.time() if now is None else now) - float(requested_at)
    return {
        "pending": True,
        "requestedAt": requested_at,
        "stale": age is not None and age > REQUEST_STALE_S,
    }


def status() -> dict[str, Any]:
    """Everything the Updates tab shows for THIS unit.

    Read-only and cheap: two small files off the bind mount. The GUI polls this per selected unit
    during an update, including across the window where the container is being recreated and the
    request simply fails — which is why nothing here is cached in memory.
    """
    state = _read_json(state_path())
    payload: dict[str, Any] = {
        "running": running_image(),
        "agent": _agent_block(state),
        "channels": list(CHANNELS),
    }
    payload.update(_pending_block())
    if state is not None:
        # The agent owns every field below: it is the only half that can talk to Docker or the
        # registry. Passed through rather than re-shaped so a newer agent can add fields without a
        # container change.
        payload["channel"] = state.get("channel")
        payload["image"] = state.get("image")
        payload["digest"] = state.get("digest")
        payload["available"] = state.get("available")
        payload["lastCheck"] = state.get("lastCheck")
        payload["lastUpdate"] = state.get("lastUpdate")
        payload["phase"] = state.get("phase")
    return payload


def request_update(channel: str | None = None, *, check_only: bool = False) -> dict[str, Any]:
    """Ask the host agent to update this unit. Returns as soon as the request is on disk.

    Refuses when no agent is installed. That is not defensive padding: writing the file anyway
    would leave the GUI polling a request nothing will ever consume, and the unit would look hung
    rather than unprovisioned. A unit provisioned before the agent existed is exactly this case, and
    the fix is one `provision.sh` run, which the error names.
    """
    if channel is None:
        channel = "dev"
    if channel not in CHANNELS:
        raise UpdateError(f"unknown channel {channel!r} — expected one of {', '.join(CHANNELS)}")

    state = _read_json(state_path())
    if state is None:
        raise UpdateError(
            "no update agent on this host — run scripts/host-setup/provision.sh for this unit, once per Pi image"
        )

    payload = {
        "channel": channel,
        "checkOnly": bool(check_only),
        "requestedAt": time.time(),
    }
    try:
        _write_json_atomic(request_path(), payload)
    except OSError as exc:
        raise UpdateError(f"could not write the update request: {exc}") from exc
    logger.info("update requested: channel=%s check_only=%s", channel, check_only)
    return payload
