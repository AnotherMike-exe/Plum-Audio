"""Sync-engine ABC — the seam between Plum-Audio's app model and the sync library.

Only one implementation ships (SendspinEngine), but routing this through an interface keeps
mesh/lifecycle logic from hard-coding aiosendspin calls, so a pin bump is contained here.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class SyncEngine(ABC):
    """Abstracts the sync backbone: groups, streams, player routing, volume."""

    @abstractmethod
    async def on_source_active(self, source_id: str, fmt, fifo_path: str) -> None:
        """A local source started producing audio — create/attach its group + feeder."""

    @abstractmethod
    async def on_source_idle(self, source_id: str) -> None:
        """A local source went idle — tear down its stream."""

    @abstractmethod
    async def route_player(self, player_id: str, group_id: str) -> None:
        """Move a player into a group. Intra-server: live add_client. Cross-server: reclaim."""

    @abstractmethod
    async def set_player_volume(self, player_id: str, volume: int, muted: bool) -> None:
        ...

    @abstractmethod
    def get_status(self) -> dict:
        """Snapshot of servers/groups/players for the mesh aggregator + frontend."""
