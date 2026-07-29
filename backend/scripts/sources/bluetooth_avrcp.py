#!/usr/bin/env python3
"""
Plum-Audio — Bluetooth AVRCP metadata/transport (BlueZ MediaPlayer1) → Sendspin roles.

BlueZ presents the phone's AVRCP player as `org.bluez.MediaPlayer1` (Track/Status/Position, plus
Play/Pause/Next/Previous and — sometimes — Repeat/Shuffle) and the audio link as
`org.bluez.MediaTransport1` (AVRCP Absolute Volume, 0-127). This module maps both onto the source
group's metadata/artwork/controller roles, OUT OF BAND from the audio stream, and doubles as the
source's transport remote — the same double duty SpotifyGoLibrespot does for go-librespot.

Ported from Plum-Snapcast's bluetooth-control-script.py, of which only the AVRCP extraction
survives. Dropped: the Snapcast JSON-RPC half, the notification-guard machinery (a metadata push is
not an audio event on Sendspin, so the resync storm cannot happen), the `MetadataStore` with its
hand-rolled position interpolation, and the `playback_api` POSTs — the Sendspin metadata role
stamps a timestamp and clients extrapolate, which is exactly what that API was invented to do.

REPEAT/SHUFFLE ARE PER-PLAYER, NOT PER-SOURCE. AVRCP player settings are optional — the phone
decides whether to expose them at all — so unlike Spotify (where `supports_repeat_shuffle` is a
constant) we discover them when a player binds and ask the server to re-advertise the source's
command set. Advertising a command we can't honour would put dead controls in every GUI on the
group. (Do NOT assume iOS omits them: the iPhone tested on 2026-07-28 exposed both Repeat and
Shuffle. Detect, never assume — in either direction.)

POSITION HANDLING — the fiddliest part of this file, all of it learned on hardware:
  * Re-anchor on every Track change, Status flip, and Position signal. Never emit a bare speed flip:
    the role re-stamps its existing anchor with a fresh timestamp, so clients resume extrapolating
    from the wrong place and, because the role CLAMPS at track_duration, park at 100%. Same trap as
    spotify_golibrespot._apply_speed.
  * Never assume position 0 on a Track update. BlueZ re-sends Track for things that aren't track
    changes (late ImgHandle, a re-read when a controller joins), so assuming 0 rewinds mid-song.
  * ALWAYS STAMP A FRESH TIMESTAMP — never use role.update() for progress. update() does
    replace(current, **kwargs), which copies the existing timestamp_us, and set_metadata only
    stamps anew when it is None. A current position paired with a stale timestamp makes the client
    add the already-elapsed time on top, i.e. double-count: right for a moment, then a jump
    forward, compounding after each pause. This was the actual cause of the timeline bug, and
    airplay_metadata._emit_progress had already found and documented the same library quirk.
  * TRUST BlueZ's Position; it is accurate to ~2 ms over 13 s (measured). An earlier version made
    the ticker reject readings that disagreed with our own anchor, on a theory about pause-inflated
    positions that measurement disproved — all it could ever do was discard the truth, including a
    real scrub. The single misreport actually observed is a paused device claiming 0, handled at
    the one place it occurs (_reanchor_now).
  * A remote scrub arrives BOTH as a Position SIGNAL and in the polled property; the signal is
    logged because it is the only way to tell a jump from normal advance.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import replace

from dbus_next import Variant

from aiosendspin.models.types import RepeatMode

logger = logging.getLogger("plum.bluetooth_avrcp")

BLUEZ = "org.bluez"
PLAYER_IFACE = "org.bluez.MediaPlayer1"
TRANSPORT_IFACE = "org.bluez.MediaTransport1"
PROPS_IFACE = "org.freedesktop.DBus.Properties"

# AVRCP absolute volume is 0-127; Sendspin speaks 0-100.
AVRCP_VOLUME_MAX = 127

# BlueZ Status -> is this playing? "forward-seek"/"reverse-seek" still count as playing; "error"
# and "stopped" do not.
PLAYING_STATES = {"playing", "forward-seek", "reverse-seek"}

# Seeding a just-appeared player can race BlueZ exporting it (see _seed_from_player).
SEED_ATTEMPTS = 5
SEED_RETRY_S = 0.5

# How often we re-anchor progress from BlueZ's Position (see _progress_ticker). Matches
# airplay_metadata.PROGRESS_TICK_S — same problem, same cadence.
PROGRESS_TICK_S = 1.0
# A paused track sitting past this offset cannot really be at 0 — that is the device lying (see
# _reanchor_now). Deliberately narrow: it is the only misreport actually observed on hardware.
POSITION_ZERO_LIE_MS = 2000

# BlueZ MediaPlayer1.Repeat <-> Sendspin RepeatMode. BlueZ also has "group", which has no Sendspin
# equivalent; we read it as ALL (closest meaning) and never write it.
REPEAT_FROM_BLUEZ = {
    "off": RepeatMode.OFF,
    "singletrack": RepeatMode.ONE,
    "alltracks": RepeatMode.ALL,
    "group": RepeatMode.ALL,
}
REPEAT_TO_BLUEZ = {
    RepeatMode.OFF: "off",
    RepeatMode.ONE: "singletrack",
    RepeatMode.ALL: "alltracks",
}


class BluetoothAvrcp:
    """Drives one Bluetooth source's metadata/transport. Also the source's transport remote."""

    def __init__(self, group, instance_id: str, adapter, *, on_commands_changed=None, cover_art=None) -> None:
        self.group = group
        self.instance_id = instance_id
        self.adapter = adapter  # BluetoothAdapter — owns the bus and the BlueZ signal fan-out
        self._on_commands_changed = on_commands_changed
        self.cover_art = cover_art  # BluetoothCoverArt | None — best-effort, never load-bearing
        self._obex_port: int | None = None
        self._player_path: str | None = None
        self._transport_path: str | None = None
        self._ticker_task: asyncio.Task | None = None
        self._warned_no_role = False
        self._playing = False
        self._duration_ms = 0
        self._last_title: str | None = None
        self._last_speed: int | None = None  # last published playback_speed, for transition logging
        # Our own anchor, so we can tell what a polled position SHOULD be (see _progress_ticker).
        self._anchor_pos_ms: int | None = None
        self._anchor_at: float = 0.0
        self._repeat = RepeatMode.OFF
        self._shuffle = False
        # Whether THIS player exposes Repeat/Shuffle at all (see module docstring).
        self.supports_repeat_shuffle = False

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        self.adapter.add_props_listener(self._on_props)
        self.adapter.add_object_listener(self._on_object)
        if self._ticker_task is None:
            self._ticker_task = asyncio.ensure_future(self._progress_ticker())

    async def stop(self) -> None:
        self._player_path = None
        self._transport_path = None
        if self._ticker_task is not None:
            self._ticker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ticker_task
            self._ticker_task = None

    async def _progress_ticker(self) -> None:
        """Re-anchor progress from BlueZ's own Position at a steady cadence while playing.

        BlueZ emits `Position` only sporadically — often just on track change and seek — so a single
        anchor has to carry the whole track. The metadata role extrapolates from the anchor's
        timestamp and CLAMPS at track_duration, so a stale anchor makes the GUI timeline drift and
        then park at 100%, and any client joining mid-track reads the wrong position entirely.
        airplay_metadata.py hit exactly this and solved it the same way (PROGRESS_TICK_S); Position
        is a readable property, so we simply ask.
        """
        while True:
            await asyncio.sleep(PROGRESS_TICK_S)
            if not self._playing or self._player_path is None or not self._duration_ms:
                continue
            props = await self._player_props()
            if props is None:
                continue
            try:
                position = int((await props.call_get(PLAYER_IFACE, "Position")).value)
            except Exception:  # noqa: BLE001 - player vanished mid-poll; next tick retries
                continue
            # Trust the poll. An earlier version rejected readings that disagreed with our own
            # anchor, on the theory that BlueZ inflates Position across pauses — MEASUREMENT
            # DISPROVED THAT (accurate to ~2 ms over 13 s), and the symptom it was meant to fix
            # turned out to be the stale-timestamp bug. Keeping the guard would only ever discard
            # the truth, including a legitimate scrub. The one lie we DID measure — a reported 0
            # while paused — is handled where it happens, in _reanchor_now.
            self._anchor_progress(position, self._duration_ms)

    # -- role access ---------------------------------------------------------

    def _metadata_role(self):
        role = self.group.group_role("metadata")
        if role is None and not self._warned_no_role:
            logger.warning("[bluetooth-%s] group has no metadata role; metadata dropped", self.instance_id)
            self._warned_no_role = True
        return role

    def _controller_role(self):
        return self.group.group_role("controller")

    def push_modes(self) -> None:
        """Publish repeat/shuffle onto the controller role. No-op until a controller joins (the role
        only exists then), which is why the server also calls this on controller (re)join."""
        controller = self._controller_role()
        if controller is None or not self.supports_repeat_shuffle:
            return
        with contextlib.suppress(Exception):
            controller.set_repeat(self._repeat)
            controller.set_shuffle(self._shuffle)

    # -- BlueZ object / signal handling --------------------------------------

    def _on_object(self, path: str, interfaces: list[str], present: bool) -> None:
        if PLAYER_IFACE in interfaces:
            if present:
                self._player_path = path
                logger.info("[bluetooth-%s] AVRCP player bound: %s", self.instance_id, path)
                asyncio.ensure_future(self._seed_from_player())
            elif self._player_path == path:
                logger.info("[bluetooth-%s] AVRCP player gone", self.instance_id)
                self._player_path = None
                self._set_command_support(False)
        if TRANSPORT_IFACE in interfaces:
            if present:
                self._transport_path = path
                asyncio.ensure_future(self._seed_from_transport())
            elif self._transport_path == path:
                self._transport_path = None

    def _on_props(self, path: str, interface: str, changed: dict) -> None:
        if interface == TRANSPORT_IFACE and path == self._transport_path:
            if "Volume" in changed:
                self._apply_volume(changed["Volume"])
            return
        if interface != PLAYER_IFACE:
            return
        # A player can appear via PropertiesChanged before InterfacesAdded is dispatched.
        if self._player_path is None:
            self._player_path = path
        if path != self._player_path:
            return
        if "Track" in changed:
            self._apply_track(changed["Track"])
        if "Status" in changed:
            self._apply_status(str(changed["Status"]))
        if "Position" in changed:
            # A Position SIGNAL is BlueZ telling us the phone jumped (scrub/seek) — the polled
            # property alone can't distinguish that from normal advance. Rare enough to log always.
            seek_to = int(changed["Position"])
            logger.info("[bluetooth-%s] seek signal -> %d ms", self.instance_id, seek_to)
            self._anchor_progress(seek_to, self._duration_ms)
        # Seeing either property at all proves the player exposes it. This is the backstop for a
        # phone that populates these later than the seed's retry window — without it, support is
        # decided once at bind time and a late arrival is never noticed.
        if "Repeat" in changed or "Shuffle" in changed:
            self._set_command_support(True)
        if "Repeat" in changed:
            self._repeat = REPEAT_FROM_BLUEZ.get(str(changed["Repeat"]).lower(), RepeatMode.OFF)
            self.push_modes()
        if "Shuffle" in changed:
            self._shuffle = str(changed["Shuffle"]).lower() != "off"
            self.push_modes()

    async def _player_props(self):
        bus = self.adapter.bus
        if bus is None or self._player_path is None:
            return None
        try:
            intro = await bus.introspect(BLUEZ, self._player_path)
            return bus.get_proxy_object(BLUEZ, self._player_path, intro).get_interface(PROPS_IFACE)
        except Exception:  # noqa: BLE001 - player vanished
            return None

    async def _seed_from_player(self) -> None:
        """Read the player on bind, so the roles reflect an in-progress track immediately.

        This is also where we learn whether the player exposes Repeat/Shuffle — GetAll rather than
        individual Gets, because a missing optional property raises and we want one round trip.

        RETRIED, because it is the ONLY place repeat/shuffle support is discovered. We are called
        the instant InterfacesAdded announces the player, and introspecting an object BlueZ has only
        just exported can fail; a one-shot seed then gives up forever. Track/Status still arrive
        later via PropertiesChanged, so the source looks completely healthy while the controller
        role quietly advertises no repeat/shuffle — observed on hardware 2026-07-28, where the
        phone exposed both and we never noticed.
        """
        path = self._player_path
        logger.info("[bluetooth-%s] seeding player %s", self.instance_id, path)
        values = None
        for attempt in range(SEED_ATTEMPTS):
            if self._player_path != path:
                # Silent-return trap: BlueZ churns player objects (player0 -> player1) as the phone
                # switches apps, so this fires more than you would expect. Log it — an unexplained
                # silent return here is exactly what hid a missing repeat/shuffle detection before.
                logger.info(
                    "[bluetooth-%s] seed for %s abandoned; player is now %s",
                    self.instance_id, path, self._player_path,
                )
                return
            props = await self._player_props()
            if props is not None:
                try:
                    all_props = await props.call_get_all(PLAYER_IFACE)
                    values = {k: (v.value if isinstance(v, Variant) else v) for k, v in all_props.items()}
                    # A SUCCESSFUL GetAll is not necessarily a COMPLETE one. BlueZ populates
                    # MediaPlayer1 progressively as AVRCP negotiation finishes, so ~250 ms after
                    # the player binds, Repeat/Shuffle are typically still absent even though the
                    # phone supports them — snapshot then and the source advertises no
                    # repeat/shuffle for the whole session. Keep asking until they show up.
                    if "Repeat" in values or "Shuffle" in values:
                        break
                    logger.debug(
                        "[bluetooth-%s] player properties incomplete (attempt %d); no Repeat/Shuffle yet",
                        self.instance_id, attempt + 1,
                    )
                except Exception:  # noqa: BLE001 - object still settling; retry below
                    logger.debug(
                        "[bluetooth-%s] player GetAll failed (attempt %d)",
                        self.instance_id, attempt + 1, exc_info=True,
                    )
            await asyncio.sleep(SEED_RETRY_S)
        if values is None:
            logger.warning(
                "[bluetooth-%s] could not read player properties after %d attempts; "
                "repeat/shuffle support unknown", self.instance_id, SEED_ATTEMPTS,
            )
            return
        self._set_command_support("Repeat" in values or "Shuffle" in values)
        # ObexPort is the phone's cover-art (BIP) L2CAP PSM. Present only on BlueZ >= 5.81 with
        # Experimental enabled AND a device that actually supports AVRCP cover art.
        port = values.get("ObexPort")
        if isinstance(port, int) and port > 0:
            self._obex_port = port
            # Open the BIP session NOW rather than waiting for an ImgHandle — many phones only
            # start publishing the handle once a session exists. See BluetoothCoverArt.prepare.
            if self.cover_art is not None:
                asyncio.ensure_future(
                    self.cover_art.prepare(self.adapter.active_address, self._obex_port)
                )
        if "Repeat" in values:
            self._repeat = REPEAT_FROM_BLUEZ.get(str(values["Repeat"]).lower(), RepeatMode.OFF)
        if "Shuffle" in values:
            self._shuffle = str(values["Shuffle"]).lower() != "off"
        if "Track" in values:
            self._apply_track(values["Track"])
        if "Status" in values:
            self._apply_status(str(values["Status"]))
        if "Position" in values:
            self._anchor_progress(int(values["Position"]), self._duration_ms)
        self.push_modes()

    async def _seed_from_transport(self) -> None:
        bus = self.adapter.bus
        if bus is None or self._transport_path is None:
            return
        with contextlib.suppress(Exception):
            intro = await bus.introspect(BLUEZ, self._transport_path)
            props = bus.get_proxy_object(BLUEZ, self._transport_path, intro).get_interface(PROPS_IFACE)
            volume = await props.call_get(TRANSPORT_IFACE, "Volume")
            self._apply_volume(volume.value if isinstance(volume, Variant) else volume)

    def _set_command_support(self, supported: bool) -> None:
        if supported == self.supports_repeat_shuffle:
            return
        self.supports_repeat_shuffle = supported
        logger.info(
            "[bluetooth-%s] player %s repeat/shuffle", self.instance_id,
            "supports" if supported else "does not support",
        )
        if self._on_commands_changed is not None:
            with contextlib.suppress(Exception):
                self._on_commands_changed()

    # -- metadata ------------------------------------------------------------

    def _apply_track(self, track) -> None:
        role = self._metadata_role()
        if role is None or not track:
            return
        values = {k: (v.value if isinstance(v, Variant) else v) for k, v in dict(track).items()}
        kwargs: dict[str, object] = {}
        if values.get("Title"):
            kwargs["title"] = str(values["Title"])
        artist = values.get("Artist")
        if artist:
            kwargs["artist"] = ", ".join(str(a) for a in artist) if isinstance(artist, (list, tuple)) else str(artist)
        if values.get("Album"):
            kwargs["album"] = str(values["Album"])
        number = values.get("TrackNumber")
        if isinstance(number, int) and number > 0:
            kwargs["track"] = number
        if kwargs:
            role.update(**kwargs)
            logger.info(
                "[bluetooth-%s] metadata: %s", self.instance_id,
                " · ".join(f"{k}={v}" for k, v in kwargs.items()),
            )
        duration = values.get("Duration")
        if isinstance(duration, int) and duration > 0:
            self._duration_ms = duration

        # Album art rides the Track dict as an opaque handle; fetching it is a separate OBEX
        # conversation, so hand it off and never await it here — art must not delay metadata.
        img_handle = values.get("ImgHandle")
        if self.cover_art is not None and img_handle:
            asyncio.ensure_future(
                self.cover_art.handle_track(
                    self.adapter.active_address, self._obex_port, str(img_handle)
                )
            )
        # Do NOT assume position 0 here. BlueZ re-sends Track for reasons that are not a new track
        # (ImgHandle arriving late, a re-read when a controller joins), so anchoring 0 on every
        # Track update rewinds the GUI to the start mid-song — visible as "switching to the
        # Bluetooth stream shows the wrong position". Ask the phone where it actually is; on a
        # genuine track change it answers ~0 anyway.
        if kwargs:
            # A real track change legitimately starts near 0; a re-sent Track for the SAME title
            # (late ImgHandle, a controller joining) does not, so only the former is trusted.
            title = kwargs.get("title")
            is_new_track = bool(title) and title != self._last_title
            if title:
                self._last_title = str(title)
            asyncio.ensure_future(
                self._reanchor_now(fallback_freeze=False, new_track=is_new_track)
            )

    def _apply_status(self, status: str) -> None:
        playing = status.lower() in PLAYING_STATES
        if playing == self._playing:
            return
        self._playing = playing
        logger.info("[bluetooth-%s] %s", self.instance_id, "playing" if playing else "paused")
        # Re-anchor from the phone's ACTUAL position rather than flipping speed on the spot. A bare
        # `role.update(playback_speed=1000)` re-stamps whatever anchor the role is holding with a
        # fresh timestamp; if that anchor is stale the client resumes extrapolating from the wrong
        # place and, because the role clamps at track_duration, parks at 100%. This is the identical
        # trap spotify_golibrespot._apply_speed documents — BlueZ just makes it easier to hit,
        # because it reports Position sporadically. Async because it needs a D-Bus round trip.
        asyncio.ensure_future(self._reanchor_now(fallback_freeze=not playing))

    async def _reanchor_now(self, *, fallback_freeze: bool, new_track: bool = False) -> None:
        """Publish progress using a freshly-read Position, so play/pause lands on the real spot.

        Guarded, because phones lie about Position WHILE PAUSED: the rig device reports 0 when
        paused and the true offset when playing, so a naive read anchors a part-way track at 0:00
        (seen as "wrong position on connect, correct after a play/pause"). A genuine track change
        legitimately IS ~0, so `new_track` is trusted outright. Everything except that one lie is
        believed — a broader rule would also throw away a real scrub.
        """
        role = self._metadata_role()
        if role is None or role.metadata is None:
            return
        position = None
        props = await self._player_props()
        if props is not None:
            with contextlib.suppress(Exception):
                position = int((await props.call_get(PLAYER_IFACE, "Position")).value)

        # Narrowly targeted at the ONE lie measured on hardware: a paused device reporting exactly
        # 0. Anything else is taken at face value — a broad "must agree with our anchor" rule would
        # also discard the truth after a scrub, which is the mistake the ticker used to make.
        if position == 0 and not new_track and not self._playing:
            expected = self._expected_position_ms()
            if expected is not None and expected > POSITION_ZERO_LIE_MS:
                logger.info(
                    "[bluetooth-%s] paused device reports position 0; keeping our anchor (~%d ms)",
                    self.instance_id, int(expected),
                )
                position = int(expected)
            elif expected is None:
                # Nothing to check against yet AND the classic paused-device lie. Publishing 0 here
                # is what showed 0:00 for a track already part-way through on first connect;
                # publishing nothing leaves the timeline blank until the first trustworthy reading,
                # which is honest rather than wrong.
                logger.info(
                    "[bluetooth-%s] paused device reports position 0 with no anchor yet; "
                    "holding off until a trustworthy reading", self.instance_id,
                )
                position = None

        if position is not None and self._duration_ms:
            self._anchor_progress(position, self._duration_ms)
            return
        # No position available. Freezing is only correct for pause; a resume with no known
        # position is better left alone than re-stamped onto a stale anchor.
        if fallback_freeze:
            with contextlib.suppress(Exception):
                role.freeze_progress()

    def _expected_position_ms(self) -> float | None:
        """Where playback should be right now, per our last anchor. None until we have one."""
        if self._anchor_pos_ms is None:
            return None
        if not self._playing:
            return float(self._anchor_pos_ms)
        return self._anchor_pos_ms + (time.monotonic() - self._anchor_at) * 1000.0

    def _anchor_progress(self, position_ms: int | None, duration_ms: int) -> None:
        role = self._metadata_role()
        if role is None or position_ms is None or not duration_ms:
            return
        # Log only on a speed TRANSITION: the ticker calls this every second, but what matters for
        # diagnosing timeline drift is whether clients were told to stop extrapolating. A paused
        # timeline that keeps advancing in the GUI means speed 0 never reached them.
        speed = 1000 if self._playing else 0
        log_transition = speed != self._last_speed
        if log_transition:
            self._last_speed = speed
            logger.info(
                "[bluetooth-%s] progress anchor: %d/%d ms speed=%d",
                self.instance_id, position_ms, duration_ms, speed,
            )
        # All three together or the role emits none; the role stamps a timestamp so clients
        # extrapolate from here (speed 0 freezes at this anchor).
        self._anchor_pos_ms = max(0, int(position_ms))
        self._anchor_at = time.monotonic()
        if role.metadata is None:
            return
        # Force a FRESH server timestamp — do NOT use role.update() here. update() does
        # replace(current_metadata, **kwargs), which COPIES the existing timestamp_us, and
        # set_metadata only stamps anew when it is None. So every anchor would carry a current
        # position paired with an ANCIENT timestamp, and the client adds the elapsed time since
        # that stale stamp on top of an already-current position — double-counting. Symptom:
        # correct for a moment, then a jump forward, compounding after every pause.
        # airplay_metadata._emit_progress hit this first and documents the same library quirk;
        # passing timestamp_us=None is what makes each emit a clean (position @ now) pair.
        role.set_metadata(
            replace(
                role.metadata,
                track_progress=self._anchor_pos_ms,
                track_duration=max(0, int(duration_ms)),
                playback_speed=speed,
                timestamp_us=None,
            )
        )

    def _apply_volume(self, raw) -> None:
        try:
            percent = int(round(int(raw) * 100 / AVRCP_VOLUME_MAX))
        except (TypeError, ValueError):
            return
        logger.debug("[bluetooth-%s] source volume %d%%", self.instance_id, percent)

    # -- transport control (controller → the phone, over AVRCP) --------------

    async def _invoke(self, member: str) -> None:
        bus = self.adapter.bus
        if bus is None or self._player_path is None:
            logger.warning("[bluetooth-%s] %s ignored — no AVRCP player", self.instance_id, member)
            return
        try:
            intro = await bus.introspect(BLUEZ, self._player_path)
            player = bus.get_proxy_object(BLUEZ, self._player_path, intro).get_interface(PLAYER_IFACE)
            await getattr(player, member)()
            logger.info("[bluetooth-%s] %s", self.instance_id, member)
        except Exception:  # noqa: BLE001 - phone dropped the AVRCP link, or refused the command
            logger.debug("[bluetooth-%s] %s failed", self.instance_id, member, exc_info=True)

    async def _set_player_prop(self, name: str, value: Variant) -> None:
        props = await self._player_props()
        if props is None:
            return
        with contextlib.suppress(Exception):
            await props.call_set(PLAYER_IFACE, name, value)

    async def play(self) -> None:
        await self._invoke("call_play")

    async def pause(self) -> None:
        await self._invoke("call_pause")

    async def next_track(self) -> None:
        await self._invoke("call_next")

    async def previous_track(self) -> None:
        await self._invoke("call_previous")

    async def set_repeat(self, mode: RepeatMode) -> None:
        await self._set_player_prop("Repeat", Variant("s", REPEAT_TO_BLUEZ.get(mode, "off")))

    async def set_shuffle(self, shuffle: bool) -> None:
        await self._set_player_prop("Shuffle", Variant("s", "alltracks" if shuffle else "off"))
