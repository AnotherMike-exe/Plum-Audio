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

import contextlib
import json
import logging
import os
import shutil
import tempfile
import threading

logger = logging.getLogger("plum.player_state")

DEFAULT_PLAYER_STATE_FILE = "/data/player_state.json"
STATE_SAVE_DEBOUNCE_S = 2.0  # coalesce a slider drag into a single write


def state_file_path() -> str:
    return os.environ.get("PLUM_PLAYER_STATE_FILE", DEFAULT_PLAYER_STATE_FILE)


# Serialises the read-modify-write in _merge_write inside ONE process. The level is saved from the
# event loop and the health report from a worker thread; without the lock each reads the file, and
# the second rename drops the key the first one wrote. Writers in OTHER processes (output_gate.py)
# run before the player exists, so a thread lock is the whole of the contention.
_WRITE_LOCK = threading.Lock()
_write_failing = False  # one WARNING for a run of failed writes, one INFO when they stop


def _read_checked(path: str) -> tuple[dict, str | None]:
    """(state, problem). `problem` is None for a good or a missing file, else what is wrong with it.

    A missing file is normal: a unit that never changed its level has none. A zero-length or
    malformed file is NOT normal — it is what a power loss in the middle of a non-synced write
    leaves — and the caller must be able to tell the two apart, because the second one costs the
    room its level.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return {}, None
    except Exception as exc:  # noqa: BLE001 - permissions, I/O error: report, never fail to start
        return {}, f"unreadable ({exc})"
    if not raw.strip():
        return {}, "zero-length"
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return {}, f"not valid JSON ({exc})"
    if not isinstance(data, dict):
        return {}, f"not a JSON object ({type(data).__name__})"
    return data, None


def _read(path: str) -> dict:
    """The persisted state, or {} for anything missing/unreadable/malformed."""
    data, problem = _read_checked(path)
    if problem:
        logger.warning("player state %s is %s — falling back to defaults", path, problem)
    return data


def _write_atomic(path: str, data: dict) -> None:
    """Write `data` so that a power loss leaves either the old file or the new one, never neither.

    Three things, and each one closes a real hole:
      - mkstemp in the SAME directory: a fixed `<file>.tmp` is shared by every writer, so two of
        them truncate each other and the rename installs a splice. Same directory keeps the rename
        on one filesystem, which is what makes it atomic.
      - fsync BEFORE the rename: the rename is metadata and can reach the disk before the data
        does. A power cut in that window leaves a zero-length file under the real name.
      - fsync of the directory after it: makes the rename itself durable. Best-effort — some
        filesystems refuse it, and the file is already consistent either way.
    Same pattern as apis/settings_api.py `_save_settings`.
    """
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".player_state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    with contextlib.suppress(OSError):
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def _merge_write(path: str, changes: dict) -> None:
    """Atomically update SOME keys, leaving the rest alone.

    Read-modify-write rather than a plain dump: volume/mute and the active output are written on
    different schedules by different code paths, and a whole-file write from either would silently
    drop the other. A speaker that forgot its level on a device switch would look exactly like the
    missing-echo bug this file exists to prevent.

    A DAMAGED file is kept, not overwritten in silence. Merging onto `{}` and renaming over it would
    replace the room's level with whichever single key this call carries, and the only trace would
    be a unit that comes back at the default level after its next restart. So the damaged file moves
    to `<file>.corrupt` and the loss is logged at ERROR, once, at the moment it happens.
    """
    global _write_failing
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with _WRITE_LOCK:
            data, problem = _read_checked(path)
            if problem:
                kept = f"{path}.corrupt"
                with contextlib.suppress(OSError):
                    shutil.copyfile(path, kept)
                logger.error(
                    "player state %s is %s — every key it held is LOST (volume, mute, active output); "
                    "a copy is at %s and the file now restarts from %s",
                    path,
                    problem,
                    kept,
                    sorted(changes),
                )
            data.update(changes)
            _write_atomic(path, data)
        if _write_failing:
            _write_failing = False
            logger.info("player state %s is writable again", path)
    except Exception:  # noqa: BLE001 - persistence is a convenience; never break rendering over it
        # WARNING once per run of failures, not DEBUG: a state file that cannot be written means
        # the next restart loses the level, and that must be visible at the default log level. Not
        # once per call either — the health report writes every 30 s.
        if not _write_failing:
            _write_failing = True
            logger.warning("could not persist player state to %s", path, exc_info=True)
        else:
            logger.debug("could not persist player state to %s", path, exc_info=True)


def load_render_state(path: str, *, default_volume: int, default_muted: bool = False) -> tuple[int, bool]:
    """Read the persisted volume/mute, falling back to the caller's defaults.

    Anything missing, unreadable or malformed is a fallback, not an error: a speaker that cannot read
    its last level must still come up and play, just at the default. But a DAMAGED file is logged at
    ERROR, because the default is normally 100% and "the room came back loud" needs a cause in the
    log. A missing file stays quiet — that is a unit nobody adjusted yet.
    """
    data, problem = _read_checked(path)
    if problem:
        logger.error(
            "player state %s is %s — the saved level is lost; starting at volume=%d muted=%s",
            path,
            problem,
            default_volume,
            default_muted,
        )
        return default_volume, default_muted
    try:
        volume = int(data.get("volume", default_volume))
        muted = bool(data.get("muted", default_muted))
    except (TypeError, ValueError):
        logger.error("player state %s holds an unusable volume — starting at volume=%d", path, default_volume)
        return default_volume, default_muted
    return max(0, min(100, volume)), muted


def save_render_state(path: str, volume: int, muted: bool) -> None:
    """Persist volume/mute atomically (unique tmp + fsync + rename), best-effort."""
    _merge_write(path, {"volume": int(volume), "muted": bool(muted)})


def load_active_output(path: str) -> str | None:
    """The output spec the player last reported having OPEN, or None if it never has."""
    output = _read(path).get("output_device")
    return str(output) if output else None


def load_last_playing_server(path: str) -> str | None:
    """The server_id of the server that most recently had us playing, or None."""
    server_id = _read(path).get("last_playing_server_id")
    return str(server_id) if server_id else None


def save_last_playing_server(path: str, server_id: str | None) -> None:
    """Persist which server most recently had this player in playback.

    The spec is prescriptive here: a client "must persistently store the server_id of the server
    that most recently had playback_state: 'playing'", and use it to arbitrate when a second server
    connects — accept the incoming handshake first, then decide which server to keep.

    Plum implements none of that arbitration yet, and cannot fully: aiosendspin's attach_websocket
    blocks on server_hello inside the attach, so connection_reason is not available until after the
    client has already committed to the new server (see docs/UPSTREAM-AIOSENDSPIN.md). What IS
    implementable locally is the storage half, which is what this is — it makes the eventual
    arbitration a small delta rather than a design task, and it costs one key.

    Until then this is observability: it records which server won a contested speaker, which is the
    thing that was impossible to see during the Music Assistant tug-of-war.
    """
    _merge_write(path, {"last_playing_server_id": server_id})


def load_player_health(path: str) -> str | None:
    """The renderer's last reported health ("synchronized" / "error"), or None if never reported."""
    health = _read(path).get("player_health")
    return str(health) if health else None


def save_player_health(path: str, health: str) -> None:
    """Record whether the renderer is keeping up.

    This exists because aiosendspin 9.x deleted the wire field it used to ride on. The spec had
    `client/state.state: 'error'` for a client that cannot maintain sync — buffer underrun is its own
    example — and a server was meant to respond by granting more lead time. 9.x replaced that enum
    with `available: bool`, which cannot carry it: the server ends an active stream before honouring
    `available=False`, so reporting an xrun that way would turn a brief dropout into a real one.

    So the signal comes here instead. It is the only remaining place a struggling speaker is visible
    without reading logs, and unlike the log line it is queryable — the mesh API serves this file.
    See sendspin_player.PlayerHealth for the detection, which is unchanged and hardware-tuned.
    """
    _merge_write(path, {"player_health": health})


def load_health_report(path: str) -> dict | None:
    """The player's last periodic health report, or None if it never wrote one."""
    report = _read(path).get("health")
    return report if isinstance(report, dict) else None


def save_health_report(path: str, report: dict) -> None:
    """Record the player's periodic health report (see SendspinPlayer._health_report).

    `player_health` above is written only on a TRANSITION, so on its own it cannot tell a healthy
    player from one whose event loop stopped an hour ago: both read "synchronized". This block
    carries a wall-clock `ts` that the player refreshes on a fixed period, and that age is what the
    container healthcheck reads. It also carries the output-stream state, which has no other
    surface.
    """
    _merge_write(path, {"health": report})


def save_active_output(path: str, spec: str | None) -> None:
    """Record which output the player actually has open.

    This is the echo, and it is here for the same reason the volume echo is: the config API writes
    the user's CHOICE into settings.json, but only the player knows whether that choice ever became
    audible. Without it the GUI would mark a switch applied the instant it was saved — including
    when the device turned out not to open — which is the failure this slice exists to prevent.
    """
    _merge_write(path, {"output_device": spec})
