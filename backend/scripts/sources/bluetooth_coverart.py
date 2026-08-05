#!/usr/bin/env python3
"""
Plum-Audio — Bluetooth album art via AVRCP cover art (BIP over OBEX) → the Sendspin artwork role.

When a phone supports AVRCP cover art, BlueZ exposes two extra things: `MediaPlayer1.ObexPort`
(the L2CAP PSM to reach its image server) and `Track.ImgHandle` (an id for the current track's
art). Fetching the image means opening an OBEX session to that port and asking for the handle.

THREE THINGS MAKE THIS WORK HERE AND NOT ON PLUM-SNAPCAST:
  * BlueZ >= 5.81. The predecessor ran 5.70, where ObexPort simply does not exist — its cover-art
    code was written but could never have run. Ours is 5.82.
  * `Experimental = true` in /etc/bluetooth/main.conf, or ObexPort stays hidden. Host provisioning.
  * obexd, on a PRIVATE session bus per endpoint (bluetooth_manager spawns it). obexd is a
    session-bus service; a private bus avoids adding system-bus policy for org.bluez.obex and
    keeps it scoped to the endpoint, reusing multi-endpoint AirPlay's dbus-daemon trick.

THE SESSION INTERFACE IS DISCOVERED, NOT ASSUMED. The predecessor's code called
`org.bluez.obex.Image1.GetThumbnail(handle)`, but that code never successfully ran even once, so
it is not evidence of anything. BlueZ has shipped BIP client methods under more than one
name/interface across versions, and guessing wrong fails at runtime on a path that is hard to
reach in the first place. So we introspect the session object, pick the first method that matches
a known cover-art shape, and log exactly what we found — a wrong guess becomes one clear log line
instead of silent absence of artwork.

Everything here is best-effort: a phone that doesn't support cover art, an obexd that won't start,
or a fetch that fails must never disturb audio, metadata, or transport. Failures log once and stop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import tempfile

from dbus_next import Variant
from dbus_next.aio import MessageBus
from PIL import Image

from .artwork import decode_image

logger = logging.getLogger("plum.bluetooth_coverart")


def _read_bytes(path: str) -> bytes:
    """Read a settled transfer off the loop — obexd writes these to /tmp and they can be large."""
    with open(path, "rb") as f:
        return f.read()


OBEX_BUS = "org.bluez.obex"
OBEX_ROOT = "/org/bluez/obex"
CLIENT_IFACE = "org.bluez.obex.Client1"

# Session methods that mean "give me the image for this handle", best first. GetThumbnail returns a
# small (typically 200x200) JPEG, which is all the GUI needs and by far the fastest to transfer.
# Verified present on obexd 5.82: Image1 offers Properties/Get/GetThumbnail. Thumbnail
# first — it is a small JPEG, which is all the GUI needs and far faster over BIP.
FETCH_METHODS = ("call_get_thumbnail", "call_get")
# Interfaces BlueZ has used for the BIP image-pull client.
IMAGE_IFACES = ("org.bluez.obex.Image1", "org.bluez.obex.ImagePull1", "org.bluez.obex.BipImagePull1")

CONNECT_TIMEOUT_S = 10.0
FETCH_TIMEOUT_S = 15.0
# The BIP transfer fills its destination file asynchronously (see _load_image).
TRANSFER_POLL_S = 0.2
TRANSFER_POLL_ATTEMPTS = 25
MIN_IMAGE_BYTES = 100
# How long a new track may go without art before the previous cover is cleared (see note_track).
# Long enough for a late ImgHandle plus its fetch, short enough that nobody reads the old cover
# as belonging to the new track.
ART_CLEAR_GRACE_S = 2.5


class BluetoothCoverArt:
    """Fetches AVRCP cover art for one Bluetooth source and publishes it to the artwork role."""

    def __init__(self, group, instance_id: str, bus_address: str) -> None:
        self.group = group
        self.instance_id = instance_id
        self.bus_address = bus_address
        self._bus: MessageBus | None = None
        self._session_path: str | None = None
        self._session_device: str | None = None
        self._image_iface = None
        self._fetch_method: str | None = None
        # (ImgHandle, track key). The HANDLE ALONE IS NOT A TRACK IDENTITY — iOS reuses one value
        # (e.g. 1000097) for whatever is currently playing, so deduping on it alone means a new
        # track carrying the same handle is skipped and the previous cover stays on screen.
        self._last_key: tuple[str, str] | None = None
        self._fetching = False
        self._shown_key: str | None = None   # the track whose art the role is currently serving
        self._clear_task: asyncio.Task | None = None
        self._clearing_key: str | None = None  # which track the pending clear is for
        self._warned_unavailable = False
        self._warned_skipped = False

    # -- lifecycle -----------------------------------------------------------

    async def stop(self) -> None:
        await self._close_session()
        if self._bus is not None:
            with contextlib.suppress(Exception):
                self._bus.disconnect()
            self._bus = None

    async def _connect(self) -> bool:
        """Connect to this endpoint's private obex bus. False if obexd isn't up yet.

        The liveness check is load-bearing, not defensive. Our obex daemons are a SET that the
        source manager respools as a unit, so the private dbus-daemon behind `bus_address` gets
        replaced — and when it does, a cached MessageBus is left pointing at a socket nobody is
        serving. dbus-next does not raise on that; every subsequent introspect simply hangs until
        CONNECT_TIMEOUT_S, so album art dies permanently while the log shows only "the device may
        not support AVRCP cover art". Observed on hardware 2026-07-29: obexd lost a startup race for
        its bus name ("Name already in use", because the previous generation's setsid'd children
        outlived the server), the manager restarted the set, and art never came back for the life of
        the process.
        """
        if self._bus is not None:
            if self._bus.connected:
                return True
            logger.info(
                "[bluetooth-%s] obex bus went away (daemon respool); reconnecting", self.instance_id
            )
            with contextlib.suppress(Exception):
                self._bus.disconnect()
            self._bus = None
            self._session_path = None  # the session lived on the old daemon
            self._session_device = None
            # `self._image` was a typo for these two: it created a stray attribute and left the
            # interface proxy pointing at the dead bus. Harmless only because rebuilding the session
            # rebinds it — but say what is meant.
            self._image_iface = None
            self._fetch_method = None
            self._warned_unavailable = False  # a new generation deserves a fresh complaint
        try:
            self._bus = await asyncio.wait_for(
                MessageBus(bus_address=self.bus_address).connect(), timeout=CONNECT_TIMEOUT_S
            )
            return True
        except Exception:  # noqa: BLE001 - obexd/dbus-daemon still starting, or absent entirely
            self._bus = None
            if not self._warned_unavailable:
                self._warned_unavailable = True
                logger.info(
                    "[bluetooth-%s] obex bus not available (%s); album art disabled for now",
                    self.instance_id, self.bus_address,
                )
            return False

    # -- artwork role --------------------------------------------------------

    def _artwork_role(self):
        return self.group.group_role("artwork")

    # -- OBEX session --------------------------------------------------------

    async def reset_for_new_session(self) -> None:
        """Drop everything tied to the PREVIOUS AVRCP session, so the next prepare() opens a fresh one.

        Called on every player bind, and it has to be, because the event we would rather key off does
        not exist: when bluetoothd is REPLACED (an upgrade, or installing our own patched build) its
        objects do not depart with InterfacesRemoved — the service simply vanishes, so the relay sees
        no "player gone" and nothing here is invalidated. Verified on hardware 2026-07-30: `.201.113`
        had a session opened the previous afternoon, was restarted underneath, rebound its player
        cleanly the next morning — and prepare() returned early on the dead `_session_path`, so no
        session was ever reopened. The phone then withheld ImgHandle (it only publishes one while a
        BIP session exists — see prepare), no fetch was ever attempted, and the artwork role kept
        serving the previous day's last image against every new track. Entirely silent: no failure to
        log, because nothing was tried.

        Cheap enough to do unconditionally: one CreateSession round trip per bind. We cannot tell a
        live session from a zombie by looking at it, so do not try — just rebuild it.
        """
        await self._close_session()
        self._warned_skipped = False

    async def _ensure_session(self, address: str, obex_port: int) -> bool:
        # _connect() FIRST: it is what notices a replaced obex daemon, and the early-return below
        # used to skip past it, handing a cached session to a bus nobody was serving.
        if not await self._connect():
            return False
        if self._session_path is not None and self._session_device == address:
            return True
        await self._close_session()
        assert self._bus is not None
        try:
            intro = await self._bus.introspect(OBEX_BUS, OBEX_ROOT)
            client = self._bus.get_proxy_object(OBEX_BUS, OBEX_ROOT, intro).get_interface(CLIENT_IFACE)
            # Target and port key are both fussy, and both were wrong on the first attempt.
            # Probed against obexd 5.82 on the rig:
            #   Target 'avrcp'                -> DBusError with no message at all
            #   Target 'bip-avrcp' + Channel  -> "Unable to find service record" (SDP lookup)
            #   Target 'bip-avrcp' + PSM      -> session created
            # PSM is the right key because MediaPlayer1.ObexPort is an L2CAP PSM, not an RFCOMM
            # channel; passing it as Channel sends obexd looking for a service record that the
            # phone does not publish.
            session_path = await asyncio.wait_for(
                client.call_create_session(
                    address, {"Target": Variant("s", "bip-avrcp"), "PSM": Variant("q", int(obex_port))}
                ),
                timeout=CONNECT_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001 - phone refused BIP, or obexd can't reach the port
            # Distinguish "the phone said no" from "we are talking to a corpse". The second used to
            # be reported as the first, which is how a dead bus masqueraded as an unsupported device.
            dead = self._bus is None or not self._bus.connected
            # And distinguish BOTH from "someone else already holds the channel". ECONNREFUSED on the
            # BIP PSM is not a device that lacks cover art: a phone serves one BIP session at a time,
            # so a SECOND obexd on the host takes the channel and ours is refused. Seen on hardware
            # 2026-07-29 — a D-Bus-activated user-session obexd (systemd --user, not ours: no `-n`)
            # appeared the instant the phone connected and art failed with exactly this, on a port we
            # had correctly discovered. Killing it made art work on the next track. Mask the distro's
            # user obex.service on a unit, the same way we disable bluealsa-aplay.service.
            refused = "refused" in str(exc).lower()
            logger.info(
                "[bluetooth-%s] could not open a cover-art session to %s (port %s); %s",
                self.instance_id, address, obex_port,
                "the obex bus is gone — will reconnect on the next track" if dead
                else "the device REFUSED the BIP port — check for a second obexd on this host "
                     "(pgrep -af obexd; ours is the one started with -n)" if refused
                else "the device may not support AVRCP cover art",
                exc_info=True,
            )
            if dead and self._bus is not None:
                with contextlib.suppress(Exception):
                    self._bus.disconnect()
                self._bus = None
            return False

        self._session_path = str(session_path)
        self._session_device = address
        return await self._bind_image_interface()

    async def _bind_image_interface(self) -> bool:
        """Introspect the session and find whichever BIP fetch method this BlueZ actually offers."""
        assert self._bus is not None and self._session_path is not None
        try:
            intro = await self._bus.introspect(OBEX_BUS, self._session_path)
        except Exception:  # noqa: BLE001
            logger.debug("[bluetooth-%s] could not introspect obex session", self.instance_id, exc_info=True)
            return False

        available = {i.name: [m.name for m in i.methods] for i in intro.interfaces}
        obj = self._bus.get_proxy_object(OBEX_BUS, self._session_path, intro)
        for iface_name in IMAGE_IFACES:
            if iface_name not in available:
                continue
            iface = obj.get_interface(iface_name)
            for method in FETCH_METHODS:
                if hasattr(iface, method):
                    self._image_iface, self._fetch_method = iface, method
                    logger.info(
                        "[bluetooth-%s] cover art ready via %s.%s",
                        self.instance_id, iface_name, method,
                    )
                    return True
        logger.info(
            "[bluetooth-%s] no known cover-art fetch method on this obex session; interfaces=%s",
            self.instance_id, available,
        )
        return False

    async def _close_session(self) -> None:
        # Clear the dedup key FIRST, on every path. It used to be cleared only where a live session
        # was torn down, so resetting with no session left yesterday's key in place and the next
        # fetch was deduped away — stale art on a fresh connection.
        self._last_key = None
        if self._session_path is None or self._bus is None:
            self._session_path = self._session_device = self._image_iface = self._fetch_method = None
            return
        with contextlib.suppress(Exception):
            intro = await self._bus.introspect(OBEX_BUS, OBEX_ROOT)
            client = self._bus.get_proxy_object(OBEX_BUS, OBEX_ROOT, intro).get_interface(CLIENT_IFACE)
            await client.call_remove_session(self._session_path)
        self._session_path = self._session_device = self._image_iface = self._fetch_method = None

    # -- stale art -----------------------------------------------------------

    def note_track(self, track_key: str) -> None:
        """Start the clock on clearing the previous cover, for a track we may have no art for.

        Stale art is worse than none: the previous album's cover beside the new title reads as a bug,
        where an empty slot reads as "this track has no art". But do NOT clear on the spot. BlueZ
        re-sends Track for reasons that are not a new track, and ImgHandle legitimately arrives a
        moment AFTER the title — clearing immediately would blink the cover on every normal track
        change. So arm a grace period instead, and let a successful fetch cancel it.
        """
        if not track_key or track_key == self._shown_key:
            return
        if self._clearing_key == track_key and self._clear_task is not None and not self._clear_task.done():
            return  # already counting down for this track; restarting would postpone the clear
        if self._clear_task is not None and not self._clear_task.done():
            self._clear_task.cancel()
        self._clearing_key = track_key
        with contextlib.suppress(RuntimeError):  # no running loop (sync unit test)
            self._clear_task = asyncio.ensure_future(self._clear_after_grace(track_key))

    async def _clear_after_grace(self, track_key: str) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(ART_CLEAR_GRACE_S)
            if track_key == self._shown_key:
                return  # the fetch won the race; nothing stale on screen
            role = self._artwork_role()
            if role is None:
                return
            with contextlib.suppress(Exception):
                await role.set_album_artwork(None)
                self._shown_key = track_key
                logger.info(
                    "[bluetooth-%s] no art for this track; cleared the previous cover",
                    self.instance_id,
                )

    # -- fetch ---------------------------------------------------------------

    async def prepare(self, address: str | None, obex_port: int | None) -> bool:
        """Open the BIP session as soon as we know the device supports cover art.

        THIS IS NOT AN OPTIMISATION — it is what makes cover art work at all. Phones commonly
        withhold `Track.ImgHandle` until a BIP session exists, so waiting for a handle before
        opening the session deadlocks: no session, therefore no handle, therefore no session.
        Observed exactly that on the rig (ObexPort=4105 present, ImgHandle absent). Plum-Snapcast's
        control script had already found this and opened the session proactively on track change;
        its comment ("some devices may only provide ImgHandle after session is established") is the
        only surviving record of it, since that code could never actually run on BlueZ 5.70.

        Returns True once a session exists, so the caller can retry: our own obexd is started by the
        source manager and may not be up yet when the player binds (10 s apart on `.201.113`), and a
        prepare that loses that race is fatal rather than transient — nothing else calls prepare, the
        phone therefore never publishes ImgHandle, and handle_track never runs to retry the session.
        """
        if not address or not obex_port:
            return False
        if self._session_path is not None:
            return True
        if await self._ensure_session(address, int(obex_port)):
            logger.info(
                "[bluetooth-%s] cover-art session open; the device may now publish ImgHandle",
                self.instance_id,
            )
            return True
        return False

    async def handle_track(self, address: str | None, obex_port: int | None, img_handle: str | None,
                           track_key: str = "") -> None:
        """Fetch and publish art for the current track, if this device offers any."""
        if not address or not obex_port or not img_handle:
            # Log the reason ONCE: a silent return here is indistinguishable from "no art exists",
            # which is exactly how the ImgHandle deadlock above hid for a whole test round.
            if not self._warned_skipped:
                self._warned_skipped = True
                logger.info(
                    "[bluetooth-%s] no art fetch yet (address=%s obex_port=%s img_handle=%s)",
                    self.instance_id, address, obex_port, img_handle,
                )
            return
        # Dedupe on the handle AND the track it arrived with: see _last_key. Repro that found it —
        # play, disconnect, skip several tracks on the phone, reconnect and play: metadata was
        # correct and the artwork was the one from before the disconnect.
        key = (img_handle, track_key)
        if key == self._last_key or self._fetching:
            return
        if self._artwork_role() is None:
            return
        self._fetching = True
        try:
            if not await self._ensure_session(address, int(obex_port)):
                return
            image = await self._fetch(img_handle)
            if image is None:
                return
            self._last_key = key
            self._shown_key = track_key
            if self._clear_task is not None and not self._clear_task.done():
                self._clear_task.cancel()  # art landed for this track; nothing to clear
            role = self._artwork_role()
            if role is not None:
                with contextlib.suppress(Exception):
                    await role.set_album_artwork(image)
                    logger.info(
                        "[bluetooth-%s] album art: %dx%d", self.instance_id, image.width, image.height
                    )
        finally:
            self._fetching = False

    async def _fetch(self, img_handle: str) -> Image.Image | None:
        """Ask for the thumbnail and load it once the transfer lands.

        Signature (obexd 5.82): GetThumbnail(in='s' destination, 's' handle) -> ('o' transfer, props).
        WE choose the destination file; the returned object path is the Transfer1, NOT the image —
        reading that as a path was silently wrong before it was ever exercised.
        """
        if self._image_iface is None or self._fetch_method is None:
            return None
        dest = self._destination_path()
        with contextlib.suppress(OSError):
            os.unlink(dest)  # a leftover from a previous track would look like an instant success
        try:
            call = getattr(self._image_iface, self._fetch_method)
            # Get() takes an extra filter dict; GetThumbnail() does not.
            args = (dest, img_handle, {}) if self._fetch_method == "call_get" else (dest, img_handle)
            await asyncio.wait_for(call(*args), timeout=FETCH_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - transfer failed/timed out; drop this handle
            logger.info("[bluetooth-%s] cover-art fetch failed", self.instance_id, exc_info=True)
            await self._close_session()  # session may be wedged; force a fresh one next time
            return None
        return await self._load_image(dest)

    def _destination_path(self) -> str:
        return os.path.join(tempfile.gettempdir(), f"plum-bt-art-{self.instance_id}.img")

    async def _load_image(self, path: str) -> Image.Image | None:
        """BIP transfers land as a FILE — the call returns its destination PATH, not the bytes.

        obexd returns that path immediately and fills the file in as the transfer proceeds, so
        reading straight away yields a truncated (or missing) image. Poll briefly for it to settle:
        a thumbnail is small, so this normally resolves on the first or second look.
        """
        last_size = -1
        for _ in range(TRANSFER_POLL_ATTEMPTS):
            size = os.path.getsize(path) if os.path.exists(path) else 0
            # Stable and plausibly an image => done. Comparing against the previous poll avoids
            # reading a file that is still growing.
            if size > MIN_IMAGE_BYTES and size == last_size:
                break
            last_size = size
            await asyncio.sleep(TRANSFER_POLL_S)
        else:
            logger.debug("[bluetooth-%s] cover art file never settled: %s", self.instance_id, path)
            return None

        try:
            data = await asyncio.to_thread(_read_bytes, path)
        except Exception:  # noqa: BLE001 - partial or corrupt transfer
            logger.debug("[bluetooth-%s] could not read cover art %s", self.instance_id, path, exc_info=True)
            return None
        # Read and decoded off the loop — this runs in the audio event loop. See sources/artwork.py.
        return await decode_image(data, what=f"[bluetooth-{self.instance_id}] cover art")
