#!/usr/bin/env python3
"""
Plum-Audio mesh — the Sendspin neighbourhood: who else is on this network segment.

Publishes this unit's Sendspin records and watches for everyone else's, through the system Avahi
(see mesh/avahi.py for why not python-zeroconf). Two directions, per the spec:

  we PUBLISH   _sendspin-server._tcp   so any Sendspin client — ours, an ESP32 speaker, whatever —
                                       can find and dial this unit's server
  we BROWSE    _sendspin._tcp          Sendspin players on the segment. Ours advertise here too, so
                                       this is also how we find peer units' speakers
  we BROWSE    _sendspin-server._tcp   other Sendspin SERVERS — Music Assistant and friends. The
                                       protocol has no server-to-server anything, so this is purely
                                       "what else could this speaker be sent to", for the GUI

(The player process publishes _sendspin._tcp itself — it owns that socket. See sendspin_player.py.)

Interop is the point of standing on a standard: a foreign speaker is just a player whose URL came
from mDNS instead of our beacon, and it routes into our groups through the same
connect_to_client + add_client path a peer unit's player does.

mDNS is LINK-LOCAL. This sees one L2 segment; units on separate VLANs will not find each other
here (that is what the unit's own configuration is for).
"""

from __future__ import annotations

import logging

from mesh.avahi import CLIENT_SERVICE, DEFAULT_PATH, SERVER_SERVICE, AvahiClient, DiscoveredService

logger = logging.getLogger("plum.mesh.neighbourhood")


class Neighbourhood:
    """This unit's view of the Sendspin services on its network segment."""

    def __init__(
        self,
        unit_id: str,
        unit_name: str,
        *,
        server_port: int,
        own_client_ids: set[str] | None = None,
    ) -> None:
        self.unit_id = unit_id
        self.unit_name = unit_name
        self.server_port = server_port
        # Our own records come back to us from Avahi; knowing which are ours keeps the GUI from
        # offering "send this speaker to itself".
        self.own_client_ids = own_client_ids or set()
        self._avahi = AvahiClient()
        self._players: dict[str, DiscoveredService] = {}  # key -> service
        self._servers: dict[str, DiscoveredService] = {}

    async def start(self) -> None:
        await self._avahi.publish(
            self.unit_id, SERVER_SERVICE, self.server_port, {"path": DEFAULT_PATH, "name": self.unit_name}
        )
        await self._avahi.browse(CLIENT_SERVICE, self._on_player, self._on_gone)
        await self._avahi.browse(SERVER_SERVICE, self._on_server, self._on_gone)
        logger.info("neighbourhood up: advertising %s as %r", SERVER_SERVICE, self.unit_name)

    async def stop(self) -> None:
        await self._avahi.close()
        self._players.clear()
        self._servers.clear()

    # -- callbacks -----------------------------------------------------------

    def _on_player(self, service: DiscoveredService) -> None:
        self._players[service.key] = service

    def _on_server(self, service: DiscoveredService) -> None:
        self._servers[service.key] = service

    def _on_gone(self, key: str) -> None:
        self._players.pop(key, None)
        self._servers.pop(key, None)

    # -- accessors -----------------------------------------------------------

    def players(self) -> list[DiscoveredService]:
        """Every Sendspin player on the segment, ours included."""
        return list(self._players.values())

    def foreign_players(self) -> list[DiscoveredService]:
        """Players that are not this unit's own — candidate render endpoints for our sources."""
        return [s for s in self._players.values() if s.name not in self.own_client_ids]

    def servers(self) -> list[DiscoveredService]:
        """Every Sendspin server on the segment, including us."""
        return list(self._servers.values())

    def foreign_servers(self) -> list[DiscoveredService]:
        """Servers that are not this unit — e.g. Music Assistant. Somewhere a speaker could go."""
        return [s for s in self._servers.values() if s.name != self.unit_id]

    def to_dict(self) -> dict:
        """Wire form for the mesh API, so the GUI can render the wider network."""

        def _entry(s: DiscoveredService, is_own: bool) -> dict:
            return {
                "name": s.name,
                "friendly_name": s.friendly_name,
                "url": s.ws_url,
                "host": s.host,
                "port": s.port,
                "is_own": is_own,
            }

        return {
            "players": [_entry(s, s.name in self.own_client_ids) for s in self._players.values()],
            "servers": [_entry(s, s.name == self.unit_id) for s in self._servers.values()],
        }
