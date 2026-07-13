#!/usr/bin/env python3
"""
SendspinEngine — the one shipping SyncEngine, a thin facade over PlumSendspinServer.

It adds no logic; it exists so the mesh Router/aggregator depend on the SyncEngine contract
rather than on PlumSendspinServer (and, transitively, aiosendspin) directly. Every method maps
1:1 onto a primitive the server already validated on hardware. If the sync library is ever
swapped or the pin bumps incompatibly, only this class and PlumSendspinServer change.
"""
from __future__ import annotations

import logging

from mesh.model import UnitSnapshot
from sendspin_server import PlumSendspinServer
from sync_engine.base import SyncEngine

logger = logging.getLogger("plum.sync_engine")


class SendspinEngine(SyncEngine):
    def __init__(self, server: PlumSendspinServer) -> None:
        self._server = server

    def start_source(self, source_id: str, fifo_path: str) -> None:
        self._server.start_source(source_id, fifo_path)

    async def stop_source(self, source_id: str) -> None:
        await self._server.stop_source(source_id)

    async def attach_local_player(self, source_id: str, player_id: str) -> None:
        await self._server.attach_player(source_id, player_id)

    async def detach_player(self, source_id: str, player_id: str) -> None:
        await self._server.detach_player(source_id, player_id)

    async def reclaim_remote_player(self, source_id: str, player_id: str, player_url: str) -> bool:
        return await self._server.reclaim_remote_player(source_id, player_id, player_url)

    def preconnect_player(self, player_id: str, player_url: str) -> None:
        self._server.preconnect_player(player_id, player_url)

    async def set_player_volume(self, player_id: str, volume: int, muted: bool) -> None:
        # TODO(Phase 2): drive the player's volume role. Render-side gain already lives in the
        # player's AlsaRenderer; this wires the control path once the volume API is in the mesh.
        logger.info("set_player_volume(%s, vol=%d, muted=%s) — not yet wired", player_id, volume, muted)

    def snapshot(self) -> UnitSnapshot:
        return self._server.snapshot()
