#!/usr/bin/env python3
"""
Plum-Audio mesh — the Router: turn "put player P on source S" into the right primitive.

The mesh rule is "servers stay, players roam": a source's audio never leaves the unit that
ingests it, so a route is always resolved *on the source's unit*. From the aggregated MeshView
the router picks one of three paths:

  1. source local, player local   → intra-server live re-group     (engine.attach_local_player)
  2. source local, player remote  → cross-server reclaim           (engine.reclaim_remote_player)
  3. source remote                → delegate to the source's unit   (HTTP → its mesh API)

Dependencies are injected as callables (view/peer providers, a delegate for path 3) so the
router is decoupled from the aggregator/discovery build order and unit-testable in isolation.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from mesh.discovery import Peer
from mesh.model import MeshView
from sync_engine.base import SyncEngine

logger = logging.getLogger("plum.mesh.router")

ViewProvider = Callable[[], MeshView]
PeerProvider = Callable[[str], "Peer | None"]
# delegate(peer, source_id, player_id) -> awaitable: ask a peer's mesh API to run this route.
RemoteDelegate = Callable[["Peer", str, str], Awaitable[bool]]


class RouteError(Exception):
    """A route could not be planned (unknown source/player/peer) or executed."""


class Router:
    def __init__(self, local_unit_id: str, engine: SyncEngine, *,
                 view_provider: ViewProvider, peer_provider: PeerProvider,
                 delegate: RemoteDelegate | None = None) -> None:
        self.local_unit_id = local_unit_id
        self._engine = engine
        self._view = view_provider
        self._peer = peer_provider
        self._delegate = delegate

    async def route_player(self, player_id: str, source_id: str) -> bool:
        """Route a player onto a source, choosing intra / cross-server / delegate."""
        view = self._view()
        found = view.find_source(source_id)
        if found is None:
            raise RouteError(f"no unit ingests source {source_id!r}")
        source_unit, _ = found

        # Path 3: the source lives on a peer — that unit owns the route.
        if source_unit.unit_id != self.local_unit_id:
            peer = self._require_peer(source_unit.unit_id)
            if self._delegate is None:
                raise RouteError(f"source {source_id!r} is on {peer.unit_id}; no remote delegate wired")
            logger.info("delegating route player=%s source=%s -> %s", player_id, source_id, peer.unit_id)
            return await self._delegate(peer, source_id, player_id)

        # Source is local. Where is the player?
        pfound = view.find_player(player_id)
        if pfound is not None and pfound[0].unit_id == self.local_unit_id:
            # Path 1: intra-server live re-group.
            await self._engine.attach_local_player(source_id, player_id)
            return True

        # Path 2: cross-server reclaim — pull the player from its home unit onto our source.
        if pfound is None:
            raise RouteError(f"unknown player {player_id!r} (not on any unit)")
        player_unit = pfound[0]
        peer = self._require_peer(player_unit.unit_id)
        return await self._engine.reclaim_remote_player(source_id, player_id, peer.player_url)

    def preconnect(self, player_id: str) -> None:
        """Park a remote player in a DISCOVERY connection for a cheap later route (fast-switch)."""
        pfound = self._view().find_player(player_id)
        if pfound is None:
            raise RouteError(f"unknown player {player_id!r}")
        player_unit = pfound[0]
        if player_unit.unit_id == self.local_unit_id:
            return  # already local; nothing to pre-connect
        peer = self._require_peer(player_unit.unit_id)
        self._engine.preconnect_player(player_id, peer.player_url)

    async def unroute_player(self, player_id: str, source_id: str) -> None:
        """Remove a player from a (local) source group."""
        await self._engine.detach_player(source_id, player_id)

    async def set_volume(self, player_id: str, volume: int, muted: bool) -> None:
        await self._engine.set_player_volume(player_id, volume, muted)

    def _require_peer(self, unit_id: str) -> Peer:
        peer = self._peer(unit_id)
        if peer is None:
            raise RouteError(f"unit {unit_id!r} not currently discoverable")
        return peer
