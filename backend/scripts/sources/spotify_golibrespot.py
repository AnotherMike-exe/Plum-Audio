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
clients extrapolate position between events. We re-anchor on metadata/seek AND on every play/pause
(asking the daemon where it actually is) — a bare speed flip would re-stamp the track's start anchor
and snap the client's timeline back to 0:00.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging

import aiohttp
from PIL import Image

from aiosendspin.models.types import RepeatMode

logger = logging.getLogger("plum.spotify_golibrespot")

ART_TIMEOUT = aiohttp.ClientTimeout(total=8)
CONTROL_TIMEOUT = aiohttp.ClientTimeout(total=5)


class SpotifyGoLibrespot:
    """Drives one go-librespot instance: events → roles, transport ← controller. Also the source remote."""

    # go-librespot natively exposes repeat/shuffle over its API, so this source advertises the full
    # controller command set. The server reads this to decide which MediaCommands to advertise on the
    # group's controller role; a source without it (e.g. AirPlay) stays play/pause/next/previous only,
    # and the GUI hides the repeat/shuffle controls for it. See sendspin_server._supported_commands_for.
    supports_repeat_shuffle = True

    # The Spotify Connect DEVICE volume — what the phone's own volume slider shows for this speaker,
    # and what go-librespot applies to the PCM it writes us. Not an endpoint output level.
    supports_source_volume = True

    def __init__(self, group, instance_id: str, api_base: str, *, on_source_volume=None) -> None:
        self.group = group
        self.instance_id = instance_id
        self.api_base = api_base.rstrip("/")
        self._on_source_volume = on_source_volume
        # go-librespot's volume scale is its own (`volume_steps`, default 65535); the events and
        # /status carry the current max, so we convert rather than assume.
        self._volume_max = 65535
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task | None = None
        self._stop_evt = asyncio.Event()
        self._warned_no_role = False
        self._playing = False
        self._last_art_url: str | None = None
        # go-librespot models repeat as two independent booleans; Sendspin as one RepeatMode
        # (off/one/all). We track both booleans and derive the mode (see _repeat_mode).
        self._repeat_context = False
        self._repeat_track = False
        self._shuffle_state = False

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

    def _controller_role(self):
        return self.group.group_role("controller")

    # -- repeat / shuffle ----------------------------------------------------

    def _repeat_mode(self) -> RepeatMode:
        """Map go-librespot's two booleans onto the Sendspin RepeatMode. repeat_track wins (a track
        on repeat is 'one' regardless of the context flag)."""
        if self._repeat_track:
            return RepeatMode.ONE
        if self._repeat_context:
            return RepeatMode.ALL
        return RepeatMode.OFF

    def push_modes(self) -> None:
        """Publish the current repeat/shuffle state onto the group's controller role, so every GUI on
        the group reflects it. Called on each go-librespot repeat/shuffle event, and by the server when
        a controller (re)joins — the controller role only exists once a controller client is present,
        so before then this is a harmless no-op and the state is (re)published on join."""
        controller = self._controller_role()
        if controller is None:
            return
        with contextlib.suppress(Exception):
            controller.set_repeat(self._repeat_mode())
            controller.set_shuffle(self._shuffle_state)

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
            self._apply_volume(status.get("volume"), status.get("volume_steps"))
            self._playing = not status.get("paused", True) and not status.get("stopped", True)
            self._repeat_context = bool(status.get("repeat_context"))
            self._repeat_track = bool(status.get("repeat_track"))
            self._shuffle_state = bool(status.get("shuffle_context"))
            self.push_modes()
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
                await self._apply_speed(True, data)
            elif etype in ("paused", "not_playing", "stopped"):
                await self._apply_speed(False, data)
            elif etype == "seek":
                self._anchor_progress(data.get("position"), data.get("duration"))
            elif etype == "repeat_context":
                self._repeat_context = bool(data.get("value"))
                self.push_modes()
            elif etype == "repeat_track":
                self._repeat_track = bool(data.get("value"))
                self.push_modes()
            elif etype == "shuffle_context":
                self._shuffle_state = bool(data.get("value"))
                self.push_modes()
            elif etype == "volume":
                self._apply_volume(data.get("value"), data.get("max"))

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
        # Optional richer metadata the Sendspin metadata role carries (a foreign controller may show
        # it). go-librespot exposes a track number on most tracks; the role validates track > 0.
        track_number = track.get("track_number")
        if isinstance(track_number, int) and track_number > 0:
            kwargs["track"] = track_number
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

    async def _apply_speed(self, playing: bool, data: dict | None = None) -> None:
        """Flip playback speed AND re-anchor progress to the daemon's true position.

        Re-anchoring is the whole point: the metadata role stores one progress anchor and clients
        extrapolate from its timestamp. go-librespot only reports position on metadata/seek, so the
        stored anchor is normally the track's start (position ~0). Emitting a bare speed change
        re-stamps that stale anchor with a fresh timestamp — the client then reads 0:00 on pause and
        resumes counting from zero. So ask the daemon where it actually is first (the event carries
        position on some builds; /status always does) and publish the full trio.
        """
        if playing == self._playing:
            return
        self._playing = playing
        role = self._metadata_role()
        if role is None or role.metadata is None:
            return
        position, duration = (data or {}).get("position"), (data or {}).get("duration")
        if position is None or duration is None:
            position, duration = await self._status_position()
        if position is not None and duration is not None:
            self._anchor_progress(position, duration)
        else:
            # No position available: freeze at the role's own extrapolated position rather than
            # re-stamping the stale anchor. Only correct for pause; a resume just resumes.
            if playing:
                role.update(playback_speed=1000)
            else:
                with contextlib.suppress(Exception):
                    role.freeze_progress()
        logger.info("[%s] %s (pos=%s)", self.instance_id, "playing" if playing else "paused", position)

    async def _status_position(self) -> tuple[int | None, int | None]:
        """(position_ms, duration_ms) for the current track from GET /status, or (None, None)."""
        await self.connect()
        assert self._session is not None
        with contextlib.suppress(Exception):
            async with self._session.get(f"{self.api_base}/status", timeout=CONTROL_TIMEOUT) as resp:
                if resp.status != 200:
                    return None, None
                status = await resp.json()
            track = status.get("track") or {}
            return track.get("position"), track.get("duration")
        return None, None

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

    async def set_repeat(self, mode: RepeatMode) -> None:
        """Drive go-librespot's two repeat flags from a Sendspin RepeatMode. Set the winning flag on
        and the other off so the daemon lands in exactly one of off/one/all (the event stream then
        echoes the confirmed state back through push_modes)."""
        if mode is RepeatMode.ONE:
            await self._post("/player/repeat_track", {"repeat_track": True})
            await self._post("/player/repeat_context", {"repeat_context": False})
        elif mode is RepeatMode.ALL:
            await self._post("/player/repeat_context", {"repeat_context": True})
            await self._post("/player/repeat_track", {"repeat_track": False})
        else:
            await self._post("/player/repeat_context", {"repeat_context": False})
            await self._post("/player/repeat_track", {"repeat_track": False})

    async def set_shuffle(self, shuffle: bool) -> None:
        await self._post("/player/shuffle_context", {"shuffle_context": bool(shuffle)})

    # -- source volume (the Spotify Connect device level) --------------------

    def _apply_volume(self, value, vmax) -> None:
        """Record go-librespot's volume as a percentage, learning its scale from the report."""
        try:
            if vmax:
                self._volume_max = int(vmax)
            if value is None or not self._volume_max:
                return
            percent = int(round(int(value) * 100 / self._volume_max))
        except (TypeError, ValueError, ZeroDivisionError):
            return
        logger.debug("[%s] source volume %d%% (%s/%s)", self.instance_id, percent, value, self._volume_max)
        if self._on_source_volume is not None:
            self._on_source_volume(percent)

    async def set_source_volume(self, percent: int) -> None:
        """Set the Spotify Connect device volume — this is what moves on the controlling phone."""
        raw = max(0, min(self._volume_max, round(percent * self._volume_max / 100)))
        await self._post("/player/volume", {"volume": raw})
        logger.info("[%s] source volume -> %d%% (%d/%d)", self.instance_id, percent, raw, self._volume_max)
