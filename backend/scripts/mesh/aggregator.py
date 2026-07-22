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
import socket
from collections.abc import Awaitable, Callable

from mesh.discovery import MeshDiscovery, Peer
from mesh.model import MeshView, UnitSnapshot
from sync_engine.base import SyncEngine

logger = logging.getLogger("plum.mesh.aggregator")


def _detect_local_host() -> str | None:
    """This unit's own IP on the default-route interface.

    A unit ignores its own beacon, so it never learns the address peers reach it on — yet the
    local half of the view must still carry a host, or a GUI bootstrapped against this unit can't
    open its controller WS / address its REST. The connect-to-TEST-NET trick sends no packets; it
    just asks the kernel which source IP the default route would use (the wlan0 address peers see).
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))  # RFC 5737 TEST-NET-1 — unroutable, never actually contacted
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


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
        self._local_player_state: dict | None = None  # our speaker's self-report (see model)
        self._engine = engine
        self._discovery = discovery
        self._fetch = fetch_snapshot
        self.interval_s = interval_s
        self._local_host = _detect_local_host()
        self._view = MeshView(units=[self.local_snapshot()])
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

    def set_local_player_state(self, state: dict) -> None:
        """Record our own speaker's self-report (pushed by the player process)."""
        self._local_player_state = state

    def local_snapshot(self) -> UnitSnapshot:
        """The engine's snapshot, with this unit's own host stamped in (the engine can't know it)."""
        snap = self._engine.snapshot()
        if snap.host is None:
            snap.host = self._local_host
        # The engine only knows clients attached to ITS server; our speaker may be attached to
        # someone else's (a peer, Music Assistant, any Sendspin server). Its self-report travels
        # with the snapshot so every unit's GUI can still show where it went and what it's playing.
        snap.local_player = self._local_player_state
        return snap

    async def refresh(self) -> MeshView:
        """Rebuild the view now: local snapshot + every reachable peer's snapshot."""
        units: list[UnitSnapshot] = [self.local_snapshot()]
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
