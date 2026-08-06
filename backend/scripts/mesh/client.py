#!/usr/bin/env python3
"""
Plum-Audio mesh — HTTP client to peer units' mesh APIs.

Two calls the mesh core needs against a peer, both over the peer's aiohttp mesh API:
  - fetch_snapshot(peer): the aggregator's peer poll (GET /api/mesh/snapshot).
  - delegate_route(peer, ...): the router's path-3 handoff — ask the unit that ingests a source
    to run the route itself (POST /api/mesh/route), because audio never leaves its home unit.

All units run the same image, so the mesh API port is homogeneous (no need to advertise it in
the beacon). Kept deliberately thin — one shared ClientSession, short timeouts (a slow peer must
not stall the aggregator poll), failures surface as None/False for the caller to tolerate.
"""

from __future__ import annotations

import logging

import aiohttp

from mesh.discovery import Peer

logger = logging.getLogger("plum.mesh.client")

DEFAULT_API_PORT = 5001
DEFAULT_TIMEOUT_S = 3.0


class MeshClient:
    def __init__(self, *, api_port: int = DEFAULT_API_PORT, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self.api_port = api_port
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(timeout=self._timeout)

    async def stop(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _url(self, peer: Peer, path: str) -> str:
        return f"http://{peer.host}:{self.api_port}{path}"

    async def fetch_snapshot(self, peer: Peer) -> dict | None:
        """GET a peer's local snapshot for the aggregator. None on any failure/timeout."""
        assert self._session is not None
        try:
            async with self._session.get(self._url(peer, "/api/mesh/snapshot")) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
        except (TimeoutError, aiohttp.ClientError):
            return None

    async def delegate_route(self, peer: Peer, source_id: str, player_id: str) -> bool:
        """POST a route to the unit that owns the source; True on success."""
        assert self._session is not None
        payload = {"player_id": player_id, "source_id": source_id}
        try:
            async with self._session.post(self._url(peer, "/api/mesh/route"), json=payload) as resp:
                if resp.status != 200:
                    logger.warning("delegated route to %s failed: HTTP %d", peer.unit_id, resp.status)
                    return False
                body = await resp.json()
                return bool(body.get("ok"))
        except (TimeoutError, aiohttp.ClientError):
            logger.warning("delegated route to %s unreachable", peer.unit_id)
            return False

    async def delegate_unroute(self, peer: Peer, source_id: str, player_id: str) -> bool:
        """POST an unroute to the unit that owns the source (a player attached to its group is
        detached by its own server); True on success. Used to follow a leader into idle."""
        assert self._session is not None
        payload = {"player_id": player_id, "source_id": source_id}
        try:
            async with self._session.post(self._url(peer, "/api/mesh/unroute"), json=payload) as resp:
                if resp.status != 200:
                    logger.warning("delegated unroute to %s failed: HTTP %d", peer.unit_id, resp.status)
                    return False
                body = await resp.json()
                return bool(body.get("ok"))
        except (TimeoutError, aiohttp.ClientError):
            logger.warning("delegated unroute to %s unreachable", peer.unit_id)
            return False
