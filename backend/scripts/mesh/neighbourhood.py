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
from urllib.parse import urlparse

from mesh.avahi import CLIENT_SERVICE, DEFAULT_PATH, SERVER_SERVICE, AvahiClient, DiscoveredService

logger = logging.getLogger("plum.mesh.neighbourhood")


def _hostport(url: str | None) -> tuple[str, int] | None:
    """(host, port) from a ws:// URL, or None. The comparable part of a listener URL — the path and
    the scheme vary between what a device advertises and what we derived for ourselves."""
    if not url:
        return None
    parsed = urlparse(url)
    if not parsed.hostname:
        return None
    return (parsed.hostname, parsed.port or 0)


class Neighbourhood:
    """This unit's view of the Sendspin services on its network segment."""

    def __init__(
        self,
        unit_id: str,
        unit_name: str,
        *,
        server_port: int,
        own_client_ids: set[str] | None = None,
        own_player_url: str | None = None,
    ) -> None:
        self.unit_id = unit_id
        self.unit_name = unit_name
        self.server_port = server_port
        # Our own records come back to us from Avahi; knowing which are ours keeps the GUI from
        # offering "send this speaker to itself".
        #
        # Matched on the URL first, and only then on the id. A speaker has TWO names — the mDNS
        # instance name while idle, the handshake name while attached — and since aiosendspin 9.x it
        # also has two IDS: the mDNS record carries the listener id, while the id a server knows it
        # by is an X25519 public key. `own_client_ids` holds the latter, so name-matching alone
        # stopped recognising our own player and the GUI began offering to route it to itself.
        # The listener URL is the one identifier both views share; that is why it is the join.
        self.own_client_ids = own_client_ids or set()
        self.own_player_url = own_player_url
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

    async def rename(self, unit_name: str) -> None:
        """Re-advertise under a new friendly name (the user renamed the unit in Settings).

        The service INSTANCE stays keyed on unit_id — only the TXT `name` changes — so peers and
        third-party servers see a rename rather than the old unit vanishing and a new one appearing,
        which would drop routing that referenced it.
        """
        self.unit_name = unit_name
        await self._avahi.republish(
            self.unit_id, SERVER_SERVICE, self.server_port, {"path": DEFAULT_PATH, "name": unit_name}
        )

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

    def is_own_player(self, s: DiscoveredService) -> bool:
        """Whether a discovered player record is this unit's own speaker.

        Two signals, because neither is sufficient alone. The URL is the identifier the mDNS view
        and the handshake view actually share, so it is checked first — compared on (host, port)
        rather than the whole string, since a trailing path or a `127.0.0.1` vs LAN-IP difference
        would otherwise read as a different device. The id check remains as a fallback for a unit
        whose advertised URL we could not derive.
        """
        if self.own_player_url and _hostport(s.ws_url) == _hostport(self.own_player_url):
            return True
        return s.name in self.own_client_ids

    def foreign_players(self) -> list[DiscoveredService]:
        """Players that are not this unit's own — candidate render endpoints for our sources."""
        return [s for s in self._players.values() if not self.is_own_player(s)]

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
            "players": [_entry(s, self.is_own_player(s)) for s in self._players.values()],
            "servers": [_entry(s, s.name == self.unit_id) for s in self._servers.values()],
        }
