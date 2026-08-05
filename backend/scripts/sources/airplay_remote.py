#!/usr/bin/env python3
"""
Plum-Audio — AirPlay transport/volume remote via shairport-sync's MPRIS interface.

shairport-sync (built --with-mpris-interface) owns ``org.mpris.MediaPlayer2.ShairportSync`` and
relays MPRIS Player calls back to the AirPlay sender as DACP — so Play/Pause/Next/Previous actually
control the phone or Mac that's streaming.

The bus name is FIXED, so multi-endpoint AirPlay gives each shairport instance its OWN session bus
(airplay_manager.py) and this remote connects by `bus_address`; every endpoint then owns the name
unopposed. With no bus_address we fall back to the system bus (single-instance / legacy), which
requires a policy letting our user own+send the name (backend/config/shairport-sync-dbus.conf).

Ported from Plum-Snapcast's DBusControl, but async on ``dbus-next`` instead of dbus-python + a glib
main loop, so it lives natively in the audio event loop. The name is resolved lazily and dropped on
any call failure, so a shairport restart self-heals on the next command.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable

from dbus_next import BusType, Variant
from dbus_next.aio import MessageBus

logger = logging.getLogger("plum.airplay_remote")

MPRIS_NAME = "org.mpris.MediaPlayer2.ShairportSync"
MPRIS_PATH = "/org/mpris/MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
PROPS_IFACE = "org.freedesktop.DBus.Properties"


class AirplayRemote:
    """Drives shairport-sync's MPRIS Player interface (transport + the SENDER's volume)."""

    def __init__(
        self,
        on_source_volume: Callable[[int], None] | None = None,
        *,
        bus_address: str | None = None,
    ) -> None:
        self._bus_address = bus_address  # None = the system bus (single-instance fallback)
        self._bus: MessageBus | None = None
        self._player = None  # cached Player proxy interface; None = not yet bound / needs re-resolve
        self._on_source_volume = on_source_volume
        self._watch_task: asyncio.Task | None = None
        # Whether a sender is actually connected. Between sessions shairport reports
        # PlaybackStatus=Stopped and Volume=0.0 — a truthful read of nothing, which as a source
        # volume would show the user a 0% slider for an endpoint no phone is even using.
        self._has_session = False

    async def connect(self) -> None:
        """Connect to the bus (idempotent). Binding to shairport happens lazily per command."""
        if self._bus is None:
            if self._bus_address:
                self._bus = await MessageBus(bus_address=self._bus_address).connect()
                logger.info("airplay remote: connected to %s", self._bus_address)
            else:
                self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
                logger.info("airplay remote: connected to system D-Bus")

    def _drop(self) -> None:
        """Tear the whole connection down so the NEXT command reconnects from scratch.

        Dropping only the proxy is not enough: our per-endpoint bus lives on a fixed socket PATH
        that the manager unlinks-and-recreates on any shairport respool (and stale dbus-daemons can
        pile up on that path). A cached `self._bus` therefore keeps pointing at an ORPHANED daemon
        that no longer has shairport on it — every command then fails "MPRIS unavailable" forever.
        Dropping the bus too means the next `connect()` opens a fresh socket to the path, landing on
        whatever daemon currently owns it — i.e. the one shairport actually registered on.
        """
        self._player = None
        if self._bus is not None:
            with contextlib.suppress(Exception):
                self._bus.disconnect()
            self._bus = None

    async def _player_iface(self):
        if self._player is not None:
            return self._player
        try:
            await self.connect()
            intro = await self._bus.introspect(MPRIS_NAME, MPRIS_PATH)
            obj = self._bus.get_proxy_object(MPRIS_NAME, MPRIS_PATH, intro)
            self._player = obj.get_interface(PLAYER_IFACE)
            if self._on_source_volume is not None:
                obj.get_interface(PROPS_IFACE).on_properties_changed(self._on_props_changed)
            logger.info("airplay remote: bound to %s", MPRIS_NAME)
        except Exception:  # noqa: BLE001 - shairport down, wrong/orphaned daemon, or name not owned yet
            self._drop()  # force a fresh bus + introspect next time, not a stuck stale connection
            logger.debug("airplay remote: MPRIS not available yet", exc_info=True)
        return self._player

    async def _invoke(self, method: str) -> None:
        player = await self._player_iface()
        if player is None:
            logger.warning("airplay remote: %s ignored — MPRIS unavailable", method)
            return
        try:
            await getattr(player, method)()
            logger.info("airplay remote: %s", method)
        except Exception:  # noqa: BLE001 - name owner may have changed (shairport restart)
            self._drop()  # drop the stale bus+proxy; next command re-resolves the current socket
            logger.debug("airplay remote: %s failed", method, exc_info=True)

    async def play(self) -> None:
        await self._invoke("call_play")

    async def pause(self) -> None:
        await self._invoke("call_pause")

    async def next_track(self) -> None:
        await self._invoke("call_next")

    async def previous_track(self) -> None:
        await self._invoke("call_previous")

    @property
    def supports_source_volume(self) -> bool:
        """MPRIS Volume on shairport is the AirPlay sender's own level — it relays to and from the
        phone, and applies it to the PCM it hands us (`ignore_volume_control = "no"`). There is no
        mute. Claimed only while bound to the daemon AND a sender is connected: an idle endpoint
        reports 0.0, which would otherwise show as a 0% source slider for a phone that isn't
        there."""
        return self._player is not None and self._has_session

    async def set_source_volume(self, percent: int) -> None:
        """Push a volume back to the AirPlay sender.

        shairport-sync exposes MPRIS `Volume` as **READ-ONLY** and takes writes through a custom
        `SetVolume` METHOD on the Player interface (verified by introspection against 4.3.7). A
        Properties.Set is simply refused — which presents as the GUI slider moving happily while the
        phone never changes, since our own readback is a different call. So drive the method, and
        fall back to the property write for a standard MPRIS player that lacks it.
        """
        player = await self._player_iface()
        if player is None:
            return
        level = max(0.0, min(1.0, percent / 100))
        try:
            await player.call_set_volume(level)
            logger.info("airplay remote: SetVolume %d%%", percent)
            return
        except Exception:  # noqa: BLE001 - no SetVolume method (not shairport), or the call failed
            logger.debug("airplay remote: SetVolume method failed; trying the property", exc_info=True)
        try:
            await player.set_volume(level)
            logger.info("airplay remote: set Volume property %d%%", percent)
        except Exception:  # noqa: BLE001
            self._player = None
            logger.warning("airplay remote: could not set source volume to %d%%", percent)

    def start_volume_watch(self, interval: float = 5.0) -> None:
        """Keep the sender's volume flowing to `on_source_volume` without waiting for a command.

        Two reasons this can't be left to the PropertiesChanged signal alone: the proxy (and with it
        the signal subscription) is bound LAZILY on the first command, so a phone's slider would be
        invisible until someone pressed play in the GUI; and `_drop()` tears the subscription down on
        any failure or shairport respool. The loop binds as soon as the daemon appears, re-binds
        after a drop, and re-reads the property each pass so a missed signal self-corrects.
        """
        if self._watch_task is not None:
            return
        self._watch_task = asyncio.ensure_future(self._volume_watch(interval))

    async def _volume_watch(self, interval: float) -> None:
        while True:
            player = await self._player_iface()  # binds (and subscribes) as soon as shairport is up
            if player is not None:
                with contextlib.suppress(Exception):
                    status = await player.get_playback_status()
                    self._has_session = str(getattr(status, "value", status)) != "Stopped"
                    if self._has_session:
                        vol = await player.get_volume()
                        self._report_volume(getattr(vol, "value", vol))
            await asyncio.sleep(interval)

    def _on_props_changed(self, iface: str, changed: dict, _invalidated: list) -> None:
        if iface != PLAYER_IFACE:
            return
        if "PlaybackStatus" in changed:
            status = changed["PlaybackStatus"]
            self._has_session = str(getattr(status, "value", status)) != "Stopped"
        if "Volume" in changed:
            val = changed["Volume"]
            # A volume signal is itself proof of a sender: shairport only relays one from a session.
            self._has_session = True
            self._report_volume(val.value if isinstance(val, Variant) else val)

    def _report_volume(self, mpris_volume) -> None:
        if self._on_source_volume is None:
            return
        with contextlib.suppress(TypeError, ValueError):
            self._on_source_volume(int(round(float(mpris_volume) * 100)))

    async def close(self) -> None:
        if self._watch_task is not None:
            self._watch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._watch_task
            self._watch_task = None
        if self._bus is not None:
            self._bus.disconnect()
            self._bus = None
            self._player = None
