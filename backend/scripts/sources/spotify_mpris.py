#!/usr/bin/env python3
"""
Plum-Audio — Spotify (spotifyd) metadata + transport via MPRIS → Sendspin roles/controller.

spotifyd (built --with-mpris) owns ``org.mpris.MediaPlayer2.spotifyd`` on the SYSTEM D-Bus and
exposes both track metadata (Metadata/PlaybackStatus/Position properties) AND transport control
(Play/Pause/Next/Previous methods) through the one MPRIS Player interface. So — unlike AirPlay,
which needed a separate metadata FIFO (airplay_metadata) plus a control remote (airplay_remote) —
a single class drives both directions for a Spotify source.

Multi-instance naming: each spotifyd instance registers an MPRIS name. The first to start claims
the base ``org.mpris.MediaPlayer2.spotifyd``; later instances get a PID-suffixed
``org.mpris.MediaPlayer2.spotifyd.instance<PID>``. We resolve our instance's name from its process
(pgrep on the instance config filename), preferring the PID-suffixed name and falling back to the
base. The name is re-resolved lazily on any failure, so a spotifyd restart self-heals.

Ported from Plum-Snapcast's spotify-control-script.py, but async on ``dbus-next`` (native to the
audio event loop) instead of dbus-python + a glib main loop, and emitting to Sendspin metadata/
artwork roles instead of writing cover art to a snapweb root. As with AirPlay, metadata is OUT OF
BAND from the audio stream, so none of the Snapcast onResync() guards are needed.

Requires a system D-Bus policy letting our user call the spotifyd MPRIS name (see
backend/config/spotifyd-dbus.conf).
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import subprocess
import urllib.request

from PIL import Image

from dbus_next import BusType, Variant
from dbus_next.aio import MessageBus

logger = logging.getLogger("plum.spotify_mpris")

MPRIS_BASE_NAME = "org.mpris.MediaPlayer2.spotifyd"
MPRIS_PATH = "/org/mpris/MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
PROPS_IFACE = "org.freedesktop.DBus.Properties"

ART_DOWNLOAD_TIMEOUT_S = 8


def _spotifyd_pid(instance_id: str) -> str | None:
    """PID of the spotifyd process for this instance (matched by its config filename), or None."""
    with contextlib.suppress(Exception):
        result = subprocess.run(
            ["pgrep", "-f", f"spotifyd.*spotifyd-{instance_id}.conf"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().split("\n")[0]
    return None


class SpotifyMpris:
    """Drives one spotifyd instance's MPRIS interface: metadata/artwork → roles, transport ← controller.

    Owns the metadata push loop (subscribe to PropertiesChanged, plus a poll fallback for Position)
    and exposes async play/pause/next/previous for the controller event wiring in the server.
    """

    def __init__(self, group, instance_id: str) -> None:
        self.group = group
        self.instance_id = instance_id
        self._bus: MessageBus | None = None
        self._player = None  # cached Player iface proxy; None = needs (re)resolve
        self._props = None  # cached Properties iface proxy for the same object
        self._bus_name: str | None = None
        self._task: asyncio.Task | None = None
        self._stop_evt = asyncio.Event()
        self._warned_no_role = False
        self._last_art_url: str | None = None
        self._last_status: str | None = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self.run())

    async def stop(self) -> None:
        self._stop_evt.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._bus is not None:
            with contextlib.suppress(Exception):
                self._bus.disconnect()
            self._bus = None

    async def connect(self) -> None:
        """Connect to the system bus (idempotent). Binding to spotifyd happens lazily per use."""
        if self._bus is None:
            self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    # -- role access ---------------------------------------------------------

    def _metadata_role(self):
        role = self.group.group_role("metadata")
        if role is None and not self._warned_no_role:
            logger.warning("[%s] group has no metadata role; metadata will be dropped", self.instance_id)
            self._warned_no_role = True
        return role

    def _artwork_role(self):
        return self.group.group_role("artwork")

    # -- MPRIS binding -------------------------------------------------------

    def _candidate_names(self) -> list[str]:
        """MPRIS names to try for this instance, most-specific first."""
        names: list[str] = []
        pid = _spotifyd_pid(self.instance_id)
        if pid:
            names.append(f"{MPRIS_BASE_NAME}.instance{pid}")
        names.append(MPRIS_BASE_NAME)  # base name (instance 1, or single-instance)
        return names

    async def _resolve_player(self):
        """(Re)bind the Player + Properties interfaces for this instance's spotifyd, or None."""
        if self._player is not None:
            return self._player
        await self.connect()
        assert self._bus is not None
        for name in self._candidate_names():
            try:
                introspection = await self._bus.introspect(name, MPRIS_PATH)
                obj = self._bus.get_proxy_object(name, MPRIS_PATH, introspection)
                player = obj.get_interface(PLAYER_IFACE)
                props = obj.get_interface(PROPS_IFACE)
                # Probe: confirm the name is actually serviced (spotifyd may have exited).
                await props.call_get(PLAYER_IFACE, "PlaybackStatus")
                self._player = player
                self._props = props
                self._bus_name = name
                props.on_properties_changed(self._on_properties_changed)
                logger.info("[%s] bound MPRIS name %s", self.instance_id, name)
                return player
            except Exception:  # noqa: BLE001 - name not present yet / not this instance; try next
                continue
        return None

    def _drop_binding(self) -> None:
        self._player = None
        self._props = None
        self._bus_name = None

    # -- metadata pump -------------------------------------------------------

    async def run(self) -> None:
        logger.info("[%s] spotify MPRIS monitor up", self.instance_id)
        while not self._stop_evt.is_set():
            try:
                player = await self._resolve_player()
                if player is None:
                    await asyncio.sleep(1.0)  # spotifyd not up yet / no active session — retry
                    continue
                await self._refresh_all()
                # Position isn't reliably signalled, so poll it while playing to keep progress fresh;
                # PropertiesChanged handles metadata/status transitions between polls.
                await asyncio.sleep(1.0)
                if self._last_status == "Playing":
                    await self._refresh_position()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - never let a D-Bus hiccup kill the monitor
                logger.debug("[%s] MPRIS monitor error; rebinding", self.instance_id, exc_info=True)
                self._drop_binding()
                await asyncio.sleep(0.5)

    def _on_properties_changed(self, iface: str, changed: dict, _invalidated: list) -> None:
        if iface != PLAYER_IFACE:
            return
        asyncio.ensure_future(self._apply_changed(changed))

    async def _apply_changed(self, changed: dict) -> None:
        with contextlib.suppress(Exception):
            if "Metadata" in changed:
                await self._apply_metadata(_unwrap(changed["Metadata"]))
            if "PlaybackStatus" in changed:
                self._apply_status(_unwrap(changed["PlaybackStatus"]))
                await self._refresh_position()

    async def _refresh_all(self) -> None:
        if self._props is None:
            return
        with contextlib.suppress(Exception):
            status = await self._props.call_get(PLAYER_IFACE, "PlaybackStatus")
            self._apply_status(_unwrap(status))
        with contextlib.suppress(Exception):
            meta = await self._props.call_get(PLAYER_IFACE, "Metadata")
            await self._apply_metadata(_unwrap(meta))
        await self._refresh_position()

    async def _refresh_position(self) -> None:
        role = self._metadata_role()
        if role is None or self._props is None:
            return
        with contextlib.suppress(Exception):
            pos = _unwrap(await self._props.call_get(PLAYER_IFACE, "Position"))
            meta = role.metadata
            duration_ms = meta.track_duration if meta is not None else None
            position_ms = max(0, int(pos) // 1000)
            speed = 1000 if self._last_status == "Playing" else 0
            # All three progress fields set together or the role emits none; speed 0 freezes.
            if duration_ms is not None:
                role.update(track_progress=position_ms, track_duration=duration_ms, playback_speed=speed)
            else:
                role.update(track_progress=position_ms, playback_speed=speed)

    async def _apply_metadata(self, meta: dict) -> None:
        role = self._metadata_role()
        if role is None or not meta:
            return
        kwargs: dict[str, object] = {}
        title = meta.get("xesam:title")
        if title:
            kwargs["title"] = str(title)
        artist = meta.get("xesam:artist")
        if artist:
            # MPRIS artist is a list; the metadata role takes a single string.
            kwargs["artist"] = ", ".join(artist) if isinstance(artist, (list, tuple)) else str(artist)
        album = meta.get("xesam:album")
        if album:
            kwargs["album"] = str(album)
        length_us = meta.get("mpris:length")
        if length_us:
            kwargs["track_duration"] = max(0, int(length_us) // 1000)
        if kwargs:
            role.update(**kwargs)
            logger.info("[%s] metadata: %s", self.instance_id, " · ".join(f"{k}={v}" for k, v in kwargs.items()))

        art_url = meta.get("mpris:artUrl")
        if art_url and art_url != self._last_art_url:
            self._last_art_url = str(art_url)
            await self._set_artwork(str(art_url))

    def _apply_status(self, status: str | None) -> None:
        if not status or status == self._last_status:
            return
        self._last_status = status
        role = self._metadata_role()
        if role is None or role.metadata is None:
            return
        speed = 1000 if status == "Playing" else 0
        role.update(playback_speed=speed)
        logger.info("[%s] status: %s (speed=%d)", self.instance_id, status, speed)

    async def _set_artwork(self, url: str) -> None:
        role = self._artwork_role()
        if role is None:
            return
        img = await asyncio.get_running_loop().run_in_executor(None, _download_image, url)
        if img is not None:
            with contextlib.suppress(Exception):
                await role.set_album_artwork(img)
                logger.info("[%s] artwork: %dx%d", self.instance_id, img.width, img.height)

    # -- transport control (controller → spotifyd) ---------------------------

    async def _call(self, method: str) -> None:
        player = await self._resolve_player()
        if player is None:
            logger.debug("[%s] no MPRIS player bound; dropping %s", self.instance_id, method)
            return
        try:
            await getattr(player, f"call_{method}")()
        except Exception:  # noqa: BLE001 - spotifyd may have restarted; drop binding to re-resolve
            logger.debug("[%s] MPRIS %s failed; rebinding", self.instance_id, method, exc_info=True)
            self._drop_binding()

    async def play(self) -> None:
        await self._call("play")

    async def pause(self) -> None:
        await self._call("pause")

    async def next_track(self) -> None:
        await self._call("next")

    async def previous_track(self) -> None:
        await self._call("previous")


def _unwrap(value):
    """Recursively strip dbus-next Variant wrappers from a value."""
    if isinstance(value, Variant):
        return _unwrap(value.value)
    if isinstance(value, dict):
        return {k: _unwrap(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_unwrap(v) for v in value]
    return value


def _download_image(url: str) -> Image.Image | None:
    try:
        with urllib.request.urlopen(url, timeout=ART_DOWNLOAD_TIMEOUT_S) as resp:  # noqa: S310 - Spotify CDN URL
            data = resp.read()
        img = Image.open(io.BytesIO(data))
        img.load()
        return img
    except Exception:  # noqa: BLE001 - malformed/unreachable art shouldn't break the source
        logger.debug("failed to download artwork %s", url, exc_info=True)
        return None
