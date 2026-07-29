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
  * MediaPlayer1.Position is an INTERPOLATION, not a measurement. BlueZ advances it by wall clock
    between the AVRCP notifications the phone sends, and it drifts — observed reporting 454520 ms
    on a 400346 ms track, climbing, and NOT returning when the user scrubbed back. Do not build on
    it. The authoritative source is the Position SIGNAL (a relayed AVRCP notification), which is
    what carries a scrub; between signals we advance our OWN anchor, as airplay_metadata.py does.
  * Sanity-check any position against track_duration. That catches the drift and can never reject
    a legitimate scrub, which is always inside the track.
  * THE PROGRESS TICKER MUST NEVER BE ABLE TO DIE. Clients extrapolate locally between anchors, so
    a dead ticker does not look like a frozen timeline — the GUI keeps counting up perfectly
    smoothly and only the CORRECTIONS go missing. It presents as "my scrubs never show up", which
    sends you hunting through the relay and the GUI instead of the one loop that stopped. Worse,
    asyncio surfaces an unretrieved task exception only on garbage collection, and we hold a strong
    reference to the task forever, so it dies without a single line in the log. Hence the blanket
    except in _progress_ticker plus _ensure_ticker() on every player bind.
  * Session state (anchor, duration, title, playing) belongs to ONE AVRCP session. Reset it when the
    player object goes away, or a reconnect hours later resurrects the old position — the
    paused-device-lie guard will actively prefer a four-hour-old anchor over the device's honest 0.
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
# Slack before a reported position counts as past the end of the track (see _anchor_progress).
POSITION_OVERRUN_SLACK_MS = 2000
# How far a Position signal must diverge from our own anchor to count as a real seek rather than
# BlueZ re-announcing what we already know (see _apply_position_signal). Comfortably over one
# ticker period, so normal advance never reads as a jump.
POSITION_SEEK_EPSILON_MS = 1500

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
        self._ticks = 0
        self._playing = False
        self._duration_ms = 0
        self._last_title: str | None = None
        self._last_speed: int | None = None  # last published playback_speed, for transition logging
        # Our own anchor, so we can tell what a polled position SHOULD be (see _progress_ticker).
        self._anchor_pos_ms: int | None = None
        self._anchor_at: float = 0.0
        self._warned_overrun = False
        # A Position signal that arrived before any track length (see _apply_position_signal).
        self._pending_position_ms: int | None = None
        self._repeat = RepeatMode.OFF
        self._shuffle = False
        # Whether THIS player exposes Repeat/Shuffle at all (see module docstring).
        self.supports_repeat_shuffle = False

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        self.adapter.add_props_listener(self._on_props)
        self.adapter.add_object_listener(self._on_object)
        self._ensure_ticker()

    async def stop(self) -> None:
        self._player_path = None
        self._transport_path = None
        if self._ticker_task is not None:
            self._ticker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ticker_task
            self._ticker_task = None
        self._reset_session_state()

    def _ensure_ticker(self) -> None:
        """(Re)start the progress ticker unless it is already running.

        Belt and braces around the silent-death mode documented in _progress_ticker: called from
        start() AND from every AVRCP player bind, so a ticker lost to something the loop's own
        except clause cannot catch (an outside cancellation, say) is back within one reconnect
        instead of staying dead for the life of the process.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop yet (or under a sync unit test); start() will be called again in one
        if self._ticker_task is None or self._ticker_task.done():
            self._ticker_task = asyncio.ensure_future(self._progress_ticker())

    def _reset_session_state(self) -> None:
        """Forget everything that described the PREVIOUS AVRCP session.

        The anchor must not outlive the player object. Observed on hardware 2026-07-29: the phone
        detached and re-attached hours later correctly reporting Position 0, the paused-device-lie
        guard in _reanchor_now decided a FOUR-HOUR-OLD anchor (301297 ms, from a track that was no
        longer even loaded) was the more trustworthy of the two, and pinned every client to it.
        Within one session that guard is right — a part-way track really cannot be at 0 — but across
        a reconnect there is nothing worth keeping, so make sure there is nothing to keep.
        """
        self._playing = False
        self._duration_ms = 0
        self._last_title = None
        self._last_speed = None
        self._anchor_pos_ms = None
        self._anchor_at = 0.0
        self._ticks = 0
        self._warned_overrun = False
        self._pending_position_ms = None

    async def _progress_ticker(self) -> None:
        """Re-emit OUR OWN advancing anchor once a second. Never polls the device.

        BlueZ's MediaPlayer1.Position is an INTERPOLATION, not a measurement: it advances by wall
        clock between the AVRCP notifications the phone actually sends, and it drifts. Observed on
        the rig, mid-track and playing:

            Title: Never More   Duration: 400346   Position: 454520

        — 54 s past the end of its own track, climbing monotonically, and NOT coming back down when
        the user scrubbed backwards. Polling that was self-defeating: every reading was rejected as
        impossible, so nothing was published at all and clients extrapolated from a stale anchor
        forever ("the timeline just keeps counting up and ignores my scrubs").

        The authoritative source is the Position SIGNAL (_on_props), which is BlueZ relaying a real
        AVRCP notification — that is what carries a scrub. Between signals we advance our own anchor,
        exactly as airplay_metadata.py does; it never asks shairport where it is either.

        THIS LOOP MUST NEVER EXIT. See the except clause.
        """
        while True:
            await asyncio.sleep(PROGRESS_TICK_S)
            try:
                self._tick_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a bad tick must not end the ticker
                # asyncio reports an unretrieved task exception only when the task is GARBAGE
                # COLLECTED, and self._ticker_task holds a strong reference for the life of the
                # process — so one raise in here dies in complete silence, with nothing in the log.
                # Measured cost on hardware 2026-07-29: 16 minutes of playback across five tracks,
                # `playing` set, a valid anchor and duration the whole time, and not one periodic
                # update reaching a controller client (verified with a headless controller probe —
                # zero progress frames in 12 minutes). Clients extrapolate locally, so the GUI still
                # LOOKS like it is tracking; what silently stops is every CORRECTION, which is why
                # this surfaced as "scrubs on the phone never show up in the GUI" rather than as a
                # frozen timeline.
                logger.exception(
                    "[bluetooth-%s] progress tick failed; ticker continues", self.instance_id
                )

    def _tick_once(self) -> None:
        if not self._playing or not self._duration_ms:
            return
        expected = self._expected_position_ms()
        if expected is None:
            return
        if expected > self._duration_ms:
            # Extrapolated off the end with no AVRCP event to correct us. Hold here and wait for a
            # real one; log once, because going quiet is otherwise indistinguishable from the
            # silent-ticker-death above.
            if not self._warned_overrun:
                self._warned_overrun = True
                logger.info(
                    "[bluetooth-%s] extrapolated past end of track (%d/%d ms); holding until the "
                    "next AVRCP event", self.instance_id, int(expected), self._duration_ms,
                )
            return
        self._warned_overrun = False
        if self._ticks == 0:
            logger.info("[bluetooth-%s] progress ticker running (pos=%d)", self.instance_id, int(expected))
        self._ticks += 1
        self._anchor_progress(int(expected), self._duration_ms)

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
                self._ensure_ticker()
                asyncio.ensure_future(self._seed_from_player())
            elif self._player_path == path:
                logger.info("[bluetooth-%s] AVRCP player gone", self.instance_id)
                self._player_path = None
                self._set_command_support(False)
                self._reset_session_state()
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
            self._apply_position_signal(int(changed["Position"]))
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

    def _apply_position_signal(self, seek_to: int) -> None:
        """Handle a Position SIGNAL — BlueZ relaying a real AVRCP notification.

        This is the ONLY thing that can carry a within-track scrub, so it is never dropped on the
        grounds of being unexpected. It is dropped when it says nothing new: BlueZ re-emits the whole
        MediaPlayer1 property set every time it (re)creates the player object, which on a phone that
        reconnects often means the same Position arriving repeatedly — logged as a "seek" each time,
        and worse, re-publishing an anchor we already hold. Compare against where we think we are and
        only act on a genuine divergence.
        """
        expected = self._expected_position_ms()
        if expected is not None and abs(seek_to - expected) <= POSITION_SEEK_EPSILON_MS:
            logger.debug(
                "[bluetooth-%s] position signal %d ms matches our anchor (~%d ms); no seek",
                self.instance_id, seek_to, int(expected),
            )
            return
        logger.info(
            "[bluetooth-%s] seek signal -> %d ms (we had ~%s)", self.instance_id, seek_to,
            "nothing" if expected is None else "%d ms" % int(expected),
        )
        if not self._duration_ms:
            # A position means nothing without a track length, and _anchor_progress would drop it.
            # BlueZ has no ordering guarantee between Track and Position within the burst it emits
            # when it creates a player, and _reset_session_state now clears the duration on every
            # teardown — so hold the value and let _apply_track redeem it the moment a Duration
            # lands, rather than losing the first seek of a session.
            self._pending_position_ms = seek_to
            return
        self._anchor_progress(seek_to, self._duration_ms)

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
            if self._pending_position_ms is not None:
                pending, self._pending_position_ms = self._pending_position_ms, None
                logger.info(
                    "[bluetooth-%s] applying held seek %d ms now a duration is known",
                    self.instance_id, pending,
                )
                self._anchor_progress(pending, self._duration_ms)

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
        # Compute where we are with the OLD state still in effect: on a pause that means
        # anchor + elapsed (we were playing until this instant), which is the position to freeze at.
        position_now = self._expected_position_ms()
        self._playing = playing
        logger.info("[bluetooth-%s] %s", self.instance_id, "playing" if playing else "paused")
        # Re-anchor from the phone's ACTUAL position rather than flipping speed on the spot. A bare
        # `role.update(playback_speed=1000)` re-stamps whatever anchor the role is holding with a
        # fresh timestamp; if that anchor is stale the client resumes extrapolating from the wrong
        # place and, because the role clamps at track_duration, parks at 100%. This is the identical
        # trap spotify_golibrespot._apply_speed documents — BlueZ just makes it easier to hit,
        # because it reports Position sporadically. Async because it needs a D-Bus round trip.
        asyncio.ensure_future(
            self._reanchor_now(fallback_freeze=not playing, known_position=position_now)
        )

    async def _reanchor_now(
        self, *, fallback_freeze: bool, new_track: bool = False, known_position: float | None = None
    ) -> None:
        """Re-publish progress at a play/pause or track boundary.

        Prefers OUR OWN anchor. BlueZ's Position drifts (see the module docstring), and consulting
        it here is what briefly published 80632 ms when the truth was 18983 ms — corrected 8 ms
        later by the signal, but wrong on the wire in between. The device is only asked when we
        have nothing of our own: a fresh connection, or a genuine track change where our anchor
        belongs to the previous track.
        """
        role = self._metadata_role()
        if role is None or role.metadata is None:
            return

        position: int | None = None
        if known_position is not None and not new_track:
            position = int(known_position)
        else:
            position = await self._read_device_position()
            expected = self._expected_position_ms()
            if position is not None and self._duration_ms and position > self._duration_ms:
                position = None  # drifted past the end; nothing trustworthy to publish
            elif position == 0 and not self._playing and expected is not None and expected > POSITION_ZERO_LIE_MS:
                # The paused-device lie, kept from the earlier fix: a part-way track cannot be at 0.
                logger.info(
                    "[bluetooth-%s] paused device reports position 0; keeping our anchor (~%d ms)",
                    self.instance_id, int(expected),
                )
                position = int(expected)

        if position is not None and self._duration_ms:
            self._anchor_progress(position, self._duration_ms)
            return
        # Nothing trustworthy. Freezing is only right for a pause; a resume with no known position
        # is better left alone than re-stamped onto a stale anchor.
        if fallback_freeze:
            with contextlib.suppress(Exception):
                role.freeze_progress()

    async def _read_device_position(self) -> int | None:
        """One guarded read of BlueZ's Position. Only for when we have no anchor of our own."""
        props = await self._player_props()
        if props is None:
            return None
        try:
            return int((await props.call_get(PLAYER_IFACE, "Position")).value)
        except Exception:  # noqa: BLE001 - player vanished
            return None

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
        # A position past the end of the track is definitionally a lie, and BlueZ tells it: its
        # Position keeps advancing by WALL CLOCK across a pause/idle, so after a few minutes idle
        # it reports far beyond the track length (observed: 548733 ms on a 190066 ms track, which
        # is exactly the earlier anchor plus the elapsed idle time). The role clamps at duration,
        # so publishing it parks every client at 100%.
        #
        # This supersedes an earlier ticker guard that compared against our own extrapolation. That
        # one also rejected legitimate remote scrubs, and was removed after a measurement showing
        # Position accurate to ~2 ms — a measurement taken during CONTINUOUS PLAYBACK, which is the
        # one case where it never misbehaves. Checking against duration instead catches the real
        # lie and can never reject a scrub, since a scrub is always inside the track.
        if position_ms > duration_ms + POSITION_OVERRUN_SLACK_MS:
            expected = self._expected_position_ms()
            logger.info(
                "[bluetooth-%s] position %d ms exceeds track length %d ms; %s",
                self.instance_id, position_ms, duration_ms,
                "keeping our anchor" if expected is not None else "ignoring",
            )
            if expected is None or expected > duration_ms:
                return
            position_ms = int(expected)
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
