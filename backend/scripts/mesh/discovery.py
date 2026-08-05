#!/usr/bin/env python3
"""
Plum-Audio mesh — peer discovery via a UDP broadcast beacon.

Why not mDNS: python-zeroconf binds UDP 5353, which collides with the host Avahi (the same
reason the SendspinServer runs with advertising off). Instead each unit periodically broadcasts
a small JSON beacon on a dedicated port and tracks peers with a TTL. This is the "our own
discovery, driven by URL" the architecture calls for, on the L2 network we already require.

A beacon carries only identity + ports; the peer's IP is taken from the datagram source address,
so no in-process IP detection is needed. From (host, ports) the orchestrator derives the peer's
server URL (ws://host:8927/sendspin) and player URL (ws://host:8928/sendspin) for routing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket
import time
from dataclasses import dataclass

logger = logging.getLogger("plum.mesh.discovery")

BEACON_PORT = 8929
BEACON_VERSION = 1
DEFAULT_INTERVAL_S = 2.0
DEFAULT_TTL_S = 8.0  # a peer unheard for this long is dropped (≈4 missed beacons)
BROADCAST_ADDR = "255.255.255.255"
# A ceiling on the peer table. Far above any real rig (four units today), but the beacon is
# unauthenticated broadcast — without it, anything on the segment can grow this without limit and
# each entry costs a concurrent HTTP fetch per aggregator tick, inside the audio event loop.
MAX_PEERS = 64


def _valid_port(value, fallback: int) -> int:
    """A port from the wire, or the fallback when absent. Raises on anything non-numeric."""
    if value is None:
        return fallback
    port = int(value)
    if not 1 <= port <= 65535:
        raise ValueError(f"port out of range: {port}")
    return port


@dataclass
class Peer:
    """A discovered peer unit."""

    unit_id: str
    name: str
    host: str
    server_port: int
    player_port: int
    last_seen: float

    @property
    def server_url(self) -> str:
        return f"ws://{self.host}:{self.server_port}/sendspin"

    @property
    def player_url(self) -> str:
        return f"ws://{self.host}:{self.player_port}/sendspin"


class _BeaconProtocol(asyncio.DatagramProtocol):
    def __init__(self, on_datagram) -> None:
        self._on_datagram = on_datagram

    def datagram_received(self, data: bytes, addr) -> None:  # noqa: ANN001
        self._on_datagram(data, addr)

    def error_received(self, exc: Exception) -> None:
        logger.debug("beacon socket error: %s", exc)


class MeshDiscovery:
    """Broadcasts this unit's beacon and maintains a TTL'd table of peer units."""

    def __init__(
        self,
        unit_id: str,
        unit_name: str,
        *,
        server_port: int = 8927,
        player_port: int = 8928,
        beacon_port: int = BEACON_PORT,
        interval_s: float = DEFAULT_INTERVAL_S,
        ttl_s: float = DEFAULT_TTL_S,
        broadcast_addr: str = BROADCAST_ADDR,
    ) -> None:
        self.unit_id = unit_id
        self.unit_name = unit_name
        self.server_port = server_port
        self.player_port = player_port
        self.beacon_port = beacon_port
        self.interval_s = interval_s
        self.ttl_s = ttl_s
        self.broadcast_addr = broadcast_addr
        self._peers: dict[str, Peer] = {}
        self._transport: asyncio.DatagramTransport | None = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        with contextlib.suppress(AttributeError, OSError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)  # not on all platforms
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", self.beacon_port))
        self._transport, _ = await loop.create_datagram_endpoint(lambda: _BeaconProtocol(self._on_datagram), sock=sock)
        self._task = asyncio.ensure_future(self._broadcast_loop())
        logger.info("mesh discovery up: unit=%s beacon=:%d", self.unit_id, self.beacon_port)

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    # -- peer table ----------------------------------------------------------

    def peers(self) -> list[Peer]:
        """Live peers (excludes self and expired entries)."""
        now = time.monotonic()
        self._prune(now)
        return list(self._peers.values())

    def get_peer(self, unit_id: str) -> Peer | None:
        peer = self._peers.get(unit_id)
        if peer is None or time.monotonic() - peer.last_seen > self.ttl_s:
            return None
        return peer

    # -- beacon encode / decode ---------------------------------------------

    def _make_beacon(self) -> bytes:
        return json.dumps(
            {
                "v": BEACON_VERSION,
                "unit_id": self.unit_id,
                "name": self.unit_name,
                "server_port": self.server_port,
                "player_port": self.player_port,
            }
        ).encode("utf-8")

    def _prune(self, now: float) -> None:
        """Drop expired entries. peers() only FILTERED by TTL, so the table itself never shrank.

        On a normal LAN that is bounded by the number of real units. It is not bounded at all
        against the wire: the beacon is unauthenticated UDP broadcast, so anything on the segment
        can mint peers faster than they expire, and each one becomes a concurrent HTTP fetch from
        the aggregator — which runs in the audio event loop.
        """
        for unit_id in [u for u, p in self._peers.items() if now - p.last_seen > self.ttl_s]:
            del self._peers[unit_id]

    def _on_datagram(self, data: bytes, addr) -> None:  # noqa: ANN001
        try:
            msg = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        if msg.get("v") != BEACON_VERSION:
            return
        unit_id = msg.get("unit_id")
        if not isinstance(unit_id, str) or not unit_id or unit_id == self.unit_id:
            return  # ignore malformed and our own beacon
        try:
            # int() on a non-numeric value raises ValueError/TypeError, which escaped into
            # datagram_received and reached the loop exception handler — one traceback per packet.
            server_port = _valid_port(msg.get("server_port"), self.server_port)
            player_port = _valid_port(msg.get("player_port"), self.player_port)
        except (TypeError, ValueError):
            logger.debug("ignoring beacon with bad ports from %s", addr[0])
            return

        now = time.monotonic()
        prev = self._peers.get(unit_id)
        if prev is None:
            self._prune(now)
            if len(self._peers) >= MAX_PEERS:
                logger.warning(
                    "peer table full (%d); ignoring new unit %s from %s", MAX_PEERS, unit_id, addr[0]
                )
                return

        name = msg.get("name")
        self._peers[unit_id] = Peer(
            unit_id=unit_id,
            name=name if isinstance(name, str) and name else unit_id,
            host=addr[0],
            server_port=server_port,
            player_port=player_port,
            last_seen=now,
        )
        if prev is None:
            logger.info("discovered peer %s (%s) at %s", unit_id, name, addr[0])

    async def _broadcast_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self._transport is not None:
                    self._transport.sendto(self._make_beacon(), (self.broadcast_addr, self.beacon_port))
            except Exception:  # noqa: BLE001 - a transient send error must not kill discovery
                logger.debug("beacon send failed", exc_info=True)
            await asyncio.sleep(self.interval_s)
