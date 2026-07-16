#!/usr/bin/env python3
"""
Plum-Audio — Spotify (go-librespot) metadata + transport via its HTTP+WebSocket API → Sendspin.

go-librespot exposes a loopback control API per instance (config `server:` block). This client, one
per Spotify source, connects to ``ws://127.0.0.1:<port>/events`` for player events and translates
them to the source group's Sendspin metadata/artwork roles (OUT OF BAND from the audio stream), and
POSTs ``/player/*`` to drive transport from the controller. No D-Bus involved.

Chosen over spotifyd's MPRIS: spotifyd 0.4.x dropped standard MPRIS, and go-librespot's event API is
richer and native to arm64. This is the same "consume whatever the daemon natively exposes" approach
the original Plum-Snapcast took with spotifyd's then-current MPRIS — see [[spotify-source]] and
spotify_config.py. As with AirPlay, metadata is off the audio path, so no Snapcast resync guards.

Events consumed (go-librespot /events, {"type", "data"}):
  metadata {name, artist_names[], album_name, album_cover_url, position, duration}  → title/artist/
      album + artwork + progress anchor
  playing / paused / not_playing / stopped  → playback_speed (1000 / 0)
  seek {position, duration}                  → re-anchor progress
  volume {value, max}                        → source volume (logged; role wiring later)
Position/duration are milliseconds (Sendspin roles use ms); the metadata role stamps a timestamp so
clients extrapolate position between events — we only re-anchor on metadata/seek, freeze on pause.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging

import aiohttp
from PIL import Image

logger = logging.getLogger("plum.spotify_golibrespot")

ART_TIMEOUT = aiohttp.ClientTimeout(total=8)
CONTROL_TIMEOUT = aiohttp.ClientTimeout(total=5)


class SpotifyGoLibrespot:
    """Drives one go-librespot instance: events → roles, transport ← controller. Also the source remote."""

    def __init__(self, group, instance_id: str, api_base: str) -> None:
        self.group = group
        self.instance_id = instance_id
        self.api_base = api_base.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task | None = None
        self._stop_evt = asyncio.Event()
        self._warned_no_role = False
        self._playing = False
        self._last_art_url: str | None = None

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
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def connect(self) -> None:
        """Create the shared HTTP/WS session (idempotent). No network I/O until run()/control."""
        if self._session is None:
            self._session = aiohttp.ClientSession()

    # -- role access ---------------------------------------------------------

    def _metadata_role(self):
        role = self.group.group_role("metadata")
        if role is None and not self._warned_no_role:
            logger.warning("[%s] group has no metadata role; metadata will be dropped", self.instance_id)
            self._warned_no_role = True
        return role

    def _artwork_role(self):
        return self.group.group_role("artwork")

    # -- event pump ----------------------------------------------------------

    async def run(self) -> None:
        logger.info("[%s] go-librespot monitor up (%s)", self.instance_id, self.api_base)
        await self.connect()
        assert self._session is not None
        ws_url = f"{self.api_base}/events"
        while not self._stop_evt.is_set():
            try:
                await self._seed_from_status()
                async with self._session.ws_connect(ws_url, heartbeat=30) as ws:
                    logger.info("[%s] connected to %s", self.instance_id, ws_url)
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await self._on_event(msg.json())
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - go-librespot not up yet / restarted; retry
                logger.debug("[%s] events stream error; retrying", self.instance_id, exc_info=True)
            if not self._stop_evt.is_set():
                await asyncio.sleep(1.0)

    async def _seed_from_status(self) -> None:
        """GET /status once so the roles reflect any in-progress track before the first event."""
        assert self._session is not None
        with contextlib.suppress(Exception):
            async with self._session.get(f"{self.api_base}/status", timeout=CONTROL_TIMEOUT) as resp:
                if resp.status != 200:
                    return
                status = await resp.json()
            self._playing = not status.get("paused", True) and not status.get("stopped", True)
            track = status.get("track") or {}
            if track:
                await self._apply_track(track)

    async def _on_event(self, event: dict) -> None:
        etype = event.get("type")
        data = event.get("data") or {}
        with contextlib.suppress(Exception):
            if etype == "metadata":
                await self._apply_track(data)
            elif etype == "playing":
                self._set_speed(True)
            elif etype in ("paused", "not_playing", "stopped"):
                self._set_speed(False)
            elif etype == "seek":
                self._anchor_progress(data.get("position"), data.get("duration"))
            elif etype == "volume":
                value, vmax = data.get("value"), data.get("max") or 0
                if vmax:
                    logger.debug("[%s] volume %d/%d", self.instance_id, value, vmax)

    async def _apply_track(self, track: dict) -> None:
        role = self._metadata_role()
        if role is None or not track:
            return
        kwargs: dict[str, object] = {}
        if track.get("name"):
            kwargs["title"] = str(track["name"])
        artists = track.get("artist_names")
        if artists:
            kwargs["artist"] = ", ".join(artists) if isinstance(artists, (list, tuple)) else str(artists)
        if track.get("album_name"):
            kwargs["album"] = str(track["album_name"])
        if kwargs:
            role.update(**kwargs)
            logger.info("[%s] metadata: %s", self.instance_id, " · ".join(f"{k}={v}" for k, v in kwargs.items()))

        self._anchor_progress(track.get("position"), track.get("duration"))

        art_url = track.get("album_cover_url")
        if art_url and art_url != self._last_art_url:
            self._last_art_url = str(art_url)
            await self._set_artwork(str(art_url))

    def _anchor_progress(self, position, duration) -> None:
        role = self._metadata_role()
        if role is None or position is None or duration is None:
            return
        speed = 1000 if self._playing else 0
        # All three progress fields together or the role emits none; the role stamps a timestamp so
        # clients extrapolate from here (speed 0 freezes at this anchor).
        role.update(track_progress=max(0, int(position)), track_duration=max(0, int(duration)), playback_speed=speed)

    def _set_speed(self, playing: bool) -> None:
        if playing == self._playing:
            return
        self._playing = playing
        role = self._metadata_role()
        if role is None or role.metadata is None:
            return
        role.update(playback_speed=1000 if playing else 0)
        logger.info("[%s] %s", self.instance_id, "playing" if playing else "paused")

    async def _set_artwork(self, url: str) -> None:
        role = self._artwork_role()
        if role is None or self._session is None:
            return
        img = await self._download_image(url)
        if img is not None:
            with contextlib.suppress(Exception):
                await role.set_album_artwork(img)
                logger.info("[%s] artwork: %dx%d", self.instance_id, img.width, img.height)

    async def _download_image(self, url: str) -> Image.Image | None:
        assert self._session is not None
        try:
            async with self._session.get(url, timeout=ART_TIMEOUT) as resp:
                if resp.status != 200:
                    return None
                data = await resp.read()
            img = Image.open(io.BytesIO(data))
            img.load()
            return img
        except Exception:  # noqa: BLE001 - malformed/unreachable art shouldn't break the source
            logger.debug("[%s] failed to download artwork %s", self.instance_id, url, exc_info=True)
            return None

    # -- transport control (controller → go-librespot) -----------------------

    async def _post(self, path: str, payload: dict | None = None) -> None:
        await self.connect()
        assert self._session is not None
        try:
            async with self._session.post(f"{self.api_base}{path}", json=payload, timeout=CONTROL_TIMEOUT) as resp:
                if resp.status >= 400:
                    logger.debug("[%s] %s -> HTTP %d", self.instance_id, path, resp.status)
        except Exception:  # noqa: BLE001 - go-librespot may be down; drop the command
            logger.debug("[%s] control %s failed", self.instance_id, path, exc_info=True)

    async def play(self) -> None:
        await self._post("/player/resume")

    async def pause(self) -> None:
        await self._post("/player/pause")

    async def next_track(self) -> None:
        await self._post("/player/next")

    async def previous_track(self) -> None:
        await self._post("/player/prev")
