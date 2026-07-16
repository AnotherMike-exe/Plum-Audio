#!/usr/bin/env python3
"""
Plum-Audio — AirPlay transport/volume remote via shairport-sync's MPRIS interface.

shairport-sync (built --with-mpris-interface) owns ``org.mpris.MediaPlayer2.ShairportSync`` on the
SYSTEM D-Bus and relays MPRIS Player calls back to the AirPlay sender as DACP — so Play/Pause/Next/
Previous actually control the phone or Mac that's streaming. Requires a system D-Bus policy letting
our user own+send the name (see backend/config/shairport-sync-dbus.conf).

Ported from Plum-Snapcast's DBusControl, but async on ``dbus-next`` instead of dbus-python + a glib
main loop, so it lives natively in the audio event loop. The name is resolved lazily and dropped on
any call failure, so a shairport restart self-heals on the next command.
"""

from __future__ import annotations

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
    """Drives shairport-sync's MPRIS Player interface (transport, and volume for later)."""

    def __init__(self, on_source_volume: Callable[[int], None] | None = None) -> None:
        self._bus: MessageBus | None = None
        self._player = None  # cached Player proxy interface; None = not yet bound / needs re-resolve
        self._on_source_volume = on_source_volume

    async def connect(self) -> None:
        """Connect to the system bus (idempotent). Binding to shairport happens lazily per command."""
        if self._bus is None:
            self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            logger.info("airplay remote: connected to system D-Bus")

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
        except Exception:  # noqa: BLE001 - shairport may be down or not own the name yet
            self._player = None
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
            self._player = None  # drop the stale proxy; next command re-resolves
            logger.debug("airplay remote: %s failed", method, exc_info=True)

    async def play(self) -> None:
        await self._invoke("call_play")

    async def pause(self) -> None:
        await self._invoke("call_pause")

    async def play_pause(self) -> None:
        await self._invoke("call_play_pause")

    async def next_track(self) -> None:
        await self._invoke("call_next")

    async def previous_track(self) -> None:
        await self._invoke("call_previous")

    async def set_source_volume(self, percent: int) -> None:
        player = await self._player_iface()
        if player is None:
            return
        try:
            await player.set_volume(max(0.0, min(1.0, percent / 100)))
            logger.info("airplay remote: set_volume %d%%", percent)
        except Exception:  # noqa: BLE001
            self._player = None
            logger.debug("airplay remote: set_volume failed", exc_info=True)

    def _on_props_changed(self, iface: str, changed: dict, _invalidated: list) -> None:
        if iface != PLAYER_IFACE or "Volume" not in changed or self._on_source_volume is None:
            return
        val = changed["Volume"]
        vol = val.value if isinstance(val, Variant) else val
        self._on_source_volume(int(round(float(vol) * 100)))

    async def close(self) -> None:
        if self._bus is not None:
            self._bus.disconnect()
            self._bus = None
            self._player = None
