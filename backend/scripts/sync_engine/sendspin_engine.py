#!/usr/bin/env python3
"""
SendspinEngine — the one shipping SyncEngine, a thin facade over PlumSendspinServer.

It adds no logic; it exists so the mesh Router/aggregator depend on the SyncEngine contract
rather than on PlumSendspinServer (and, transitively, aiosendspin) directly. Every method maps
1:1 onto a primitive the server already validated on hardware. If the sync library is ever
swapped or the pin bumps incompatibly, only this class and PlumSendspinServer change.
"""

from __future__ import annotations

from mesh.model import UnitSnapshot
from sendspin_server import PlumSendspinServer
from sync_engine.base import SyncEngine


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

    async def set_player_volume(self, player_id: str, volume: int, muted: bool) -> None:
        self._server.set_player_volume(player_id, volume, muted)

    async def set_source_volume(
        self, source_id: str, volume: int | None = None, muted: bool | None = None
    ) -> None:
        await self._server.set_source_volume(source_id, volume, muted)

    async def adopt_client(self, source_id: str, url: str, player_id: str | None = None) -> bool:
        return await self._server.adopt_foreign_client(source_id, url, player_id=player_id)

    async def release_client(self, source_id: str, player_id: str, url: str | None = None) -> None:
        await self._server.release_foreign_client(source_id, player_id, url=url)

    def snapshot(self) -> UnitSnapshot:
        return self._server.snapshot()
