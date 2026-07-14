#!/usr/bin/env python3
"""
Plum-Audio mesh — DataAggregator: one unified MeshView across all reachable units.

Successor to Plum-Snapcast's federation state merge. The local half comes straight from the
engine's snapshot; the remote half is each discovered peer's snapshot fetched over the mesh REST
API. Peers come from `discovery` (a beacon carries identity + the reachable IP); the actual
state is pulled on a poll so the view reflects live grouping/streaming, not just liveness.

The HTTP fetch is injected (`fetch_snapshot`) so the aggregator doesn't hard-depend on a
particular client and stays unit-testable. A peer that's discovered but momentarily unreachable
is simply omitted from this cycle's view — discovery's TTL remains the authority on liveness.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from mesh.discovery import MeshDiscovery, Peer
from mesh.model import MeshView, UnitSnapshot
from sync_engine.base import SyncEngine

logger = logging.getLogger("plum.mesh.aggregator")

# fetch_snapshot(peer) -> the peer's UnitSnapshot.to_dict(), or None if unreachable this cycle.
FetchSnapshot = Callable[[Peer], Awaitable["dict | None"]]

DEFAULT_INTERVAL_S = 2.0


class DataAggregator:
    def __init__(
        self,
        local_unit_id: str,
        engine: SyncEngine,
        discovery: MeshDiscovery,
        fetch_snapshot: FetchSnapshot,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
    ) -> None:
        self.local_unit_id = local_unit_id
        self._engine = engine
        self._discovery = discovery
        self._fetch = fetch_snapshot
        self.interval_s = interval_s
        self._view = MeshView(units=[engine.snapshot()])
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._task = asyncio.ensure_future(self._poll_loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def view(self) -> MeshView:
        """The most recently built mesh view (updated each poll cycle)."""
        return self._view

    async def refresh(self) -> MeshView:
        """Rebuild the view now: local snapshot + every reachable peer's snapshot."""
        units: list[UnitSnapshot] = [self._engine.snapshot()]
        peers = self._discovery.peers()
        results = await asyncio.gather(*(self._fetch_peer(p) for p in peers))
        units.extend(u for u in results if u is not None)
        self._view = MeshView(units=units)
        return self._view

    async def _fetch_peer(self, peer: Peer) -> UnitSnapshot | None:
        try:
            data = await self._fetch(peer)
        except Exception:  # noqa: BLE001 - a peer's HTTP hiccup must not drop the whole view
            logger.debug("snapshot fetch failed for %s", peer.unit_id, exc_info=True)
            return None
        if not data:
            return None
        snap = UnitSnapshot.from_dict(data)
        snap.host = peer.host  # the beacon source IP is the authority on how we reach this peer
        return snap

    async def _poll_loop(self) -> None:
        while not self._stop.is_set():
            with contextlib.suppress(Exception):
                await self.refresh()
            await asyncio.sleep(self.interval_s)
