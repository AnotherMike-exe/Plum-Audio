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

There is no pre-connect/fast-switch operation: a Sendspin client holds one websocket, so a
playing player cannot be warmed on a second server — and the cross-server roam is already
inaudible (the player's ~300 ms jitter buffer covers the ~25-55 ms reconnect; measured emitted
silence does not move across a roam). See PlumSendspinServer.reclaim_remote_player.
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


_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")


def _idle_player_url(view, player_id: str) -> str | None:
    """Reclaim URL for a player that is attached to nothing, from its unit's self-report.

    `UnitSnapshot.players` lists only clients attached to that server, so a player sitting idle is
    absent from every one of them. Its own unit still reports it under `local_player`, which is how
    the GUI keeps showing an unattached speaker — this is the routing counterpart of that.
    """
    for unit in view.units:
        lp = unit.local_player or {}
        if lp.get("player_id") == player_id:
            return _reachable_url(lp.get("url"), unit.host)
    return None


def _reachable_url(url: str | None, unit_host: str | None) -> str | None:
    """Rewrite a peer player's loopback reclaim URL onto the host we reach that unit at.

    A unit may register its own player as ws://127.0.0.1:8928 (correct for itself, and the
    natural single-unit default). Dialing that from here would hit OUR loopback — we'd reclaim
    our own player and then wait for a player that never arrives. The unit's host comes from the
    beacon source IP, so it is the one authority on how we reach that unit.
    """
    if not url or not unit_host:
        return url
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    hostport, slash, path = rest.partition("/")
    host, colon, port = hostport.rpartition(":")
    if not colon:  # no port in the URL
        host, port = hostport, ""
    if host.lower() not in _LOOPBACK_HOSTS:
        return url
    rewritten = f"{scheme}://{unit_host}{colon}{port}{slash}{path}"
    logger.info("rewrote loopback reclaim URL %s -> %s", url, rewritten)
    return rewritten


class Router:
    def __init__(
        self,
        local_unit_id: str,
        engine: SyncEngine,
        *,
        view_provider: ViewProvider,
        peer_provider: PeerProvider,
        delegate: RemoteDelegate | None = None,
    ) -> None:
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
        # Reclaim against the player's OWN listener URL (fixed to the host the player process runs
        # on), not the unit it's currently attached to — after a roam those differ.
        if pfound is None:
            # An IDLE player is attached to nothing, so it appears in no unit's `players` list —
            # only in its own unit's `local_player` self-report (see UnitSnapshot). Without this
            # fallback an idle speaker cannot be routed at all, and since routing is the only thing
            # that would attach it, "idle" was a state nothing could get it out of: sending a player
            # to none, or rebooting its unit, left auto-follow retrying a route that could never
            # succeed ("unknown player") while the speaker sat silent. The self-report carries the
            # player's own listener URL, which is exactly what the reclaim below needs.
            url = _idle_player_url(view, player_id)
            if not url:
                raise RouteError(f"unknown player {player_id!r} (not on any unit)")
            logger.info("reclaiming idle player=%s onto source=%s via %s", player_id, source_id, url)
            return await self._engine.reclaim_remote_player(source_id, player_id, url)
        url = _reachable_url(pfound[1].url, pfound[0].host)
        if not url:
            raise RouteError(f"no reclaim URL known for player {player_id!r}")
        return await self._engine.reclaim_remote_player(source_id, player_id, url)

    async def unroute_player(self, player_id: str, source_id: str) -> None:
        """Remove a player from a (local) source group."""
        await self._engine.detach_player(source_id, player_id)

    async def set_volume(self, player_id: str, volume: int, muted: bool) -> None:
        await self._engine.set_player_volume(player_id, volume, muted)

    async def set_source_volume(
        self, source_id: str, volume: int | None = None, muted: bool | None = None
    ) -> None:
        """Drive the SENDER's own volume for a local source (never a peer's — sources stay home)."""
        await self._engine.set_source_volume(source_id, volume, muted)

    def _require_peer(self, unit_id: str) -> Peer:
        peer = self._peer(unit_id)
        if peer is None:
            raise RouteError(f"unit {unit_id!r} not currently discoverable")
        return peer
