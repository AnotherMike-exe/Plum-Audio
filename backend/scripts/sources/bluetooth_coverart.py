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
import io
import logging
import os

from dbus_next import Variant
from dbus_next.aio import MessageBus
from PIL import Image

logger = logging.getLogger("plum.bluetooth_coverart")

OBEX_BUS = "org.bluez.obex"
OBEX_ROOT = "/org/bluez/obex"
CLIENT_IFACE = "org.bluez.obex.Client1"

# Session methods that mean "give me the image for this handle", best first. GetThumbnail returns a
# small (typically 200x200) JPEG, which is all the GUI needs and by far the fastest to transfer.
FETCH_METHODS = ("call_get_thumbnail", "call_get_image_thumbnail", "call_get_image")
# Interfaces BlueZ has used for the BIP image-pull client.
IMAGE_IFACES = ("org.bluez.obex.Image1", "org.bluez.obex.ImagePull1", "org.bluez.obex.BipImagePull1")

CONNECT_TIMEOUT_S = 10.0
FETCH_TIMEOUT_S = 15.0
# The BIP transfer fills its destination file asynchronously (see _load_image).
TRANSFER_POLL_S = 0.2
TRANSFER_POLL_ATTEMPTS = 25
MIN_IMAGE_BYTES = 100


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
        self._last_handle: str | None = None
        self._fetching = False
        self._warned_unavailable = False

    # -- lifecycle -----------------------------------------------------------

    async def stop(self) -> None:
        await self._close_session()
        if self._bus is not None:
            with contextlib.suppress(Exception):
                self._bus.disconnect()
            self._bus = None

    async def _connect(self) -> bool:
        """Connect to this endpoint's private obex bus. False if obexd isn't up yet."""
        if self._bus is not None:
            return True
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

    async def _ensure_session(self, address: str, obex_port: int) -> bool:
        if self._session_path is not None and self._session_device == address:
            return True
        await self._close_session()
        if not await self._connect():
            return False
        assert self._bus is not None
        try:
            intro = await self._bus.introspect(OBEX_BUS, OBEX_ROOT)
            client = self._bus.get_proxy_object(OBEX_BUS, OBEX_ROOT, intro).get_interface(CLIENT_IFACE)
            # Target 'avrcp' is what selects the cover-art (BIP) service; Channel carries the PSM
            # BlueZ told us about via MediaPlayer1.ObexPort.
            session_path = await asyncio.wait_for(
                client.call_create_session(
                    address, {"Target": Variant("s", "avrcp"), "Channel": Variant("q", int(obex_port))}
                ),
                timeout=CONNECT_TIMEOUT_S,
            )
        except Exception:  # noqa: BLE001 - phone refused BIP, or obexd can't reach the port
            logger.info(
                "[bluetooth-%s] could not open a cover-art session to %s (port %s); "
                "the device may not support AVRCP cover art",
                self.instance_id, address, obex_port, exc_info=True,
            )
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
        if self._session_path is None or self._bus is None:
            self._session_path = self._session_device = self._image_iface = self._fetch_method = None
            return
        with contextlib.suppress(Exception):
            intro = await self._bus.introspect(OBEX_BUS, OBEX_ROOT)
            client = self._bus.get_proxy_object(OBEX_BUS, OBEX_ROOT, intro).get_interface(CLIENT_IFACE)
            await client.call_remove_session(self._session_path)
        self._session_path = self._session_device = self._image_iface = self._fetch_method = None
        self._last_handle = None

    # -- fetch ---------------------------------------------------------------

    async def handle_track(self, address: str | None, obex_port: int | None, img_handle: str | None) -> None:
        """Fetch and publish art for the current track, if this device offers any."""
        if not address or not obex_port or not img_handle:
            return  # device doesn't do cover art, or the handle hasn't arrived yet
        if img_handle == self._last_handle or self._fetching:
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
            self._last_handle = img_handle
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
        if self._image_iface is None or self._fetch_method is None:
            return None
        try:
            result = await asyncio.wait_for(
                getattr(self._image_iface, self._fetch_method)(img_handle), timeout=FETCH_TIMEOUT_S
            )
        except Exception:  # noqa: BLE001 - transfer failed/timed out; drop this handle
            logger.debug("[bluetooth-%s] cover-art fetch failed", self.instance_id, exc_info=True)
            # The session may be wedged; force a fresh one next time.
            await self._close_session()
            return None
        return await self._load_image(result)

    async def _load_image(self, result) -> Image.Image | None:
        """BIP transfers land as a FILE — the call returns its destination PATH, not the bytes.

        obexd returns that path immediately and fills the file in as the transfer proceeds, so
        reading straight away yields a truncated (or missing) image. Poll briefly for it to settle:
        a thumbnail is small, so this normally resolves on the first or second look.
        """
        path = result[0] if isinstance(result, (list, tuple)) and result else result
        if not isinstance(path, str) or not path:
            logger.debug("[bluetooth-%s] unexpected fetch result: %r", self.instance_id, result)
            return None

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
            with open(path, "rb") as f:
                data = f.read()
            img = Image.open(io.BytesIO(data))
            img.load()
            return img
        except Exception:  # noqa: BLE001 - partial or corrupt transfer
            logger.debug("[bluetooth-%s] could not read cover art %s", self.instance_id, path, exc_info=True)
            return None
