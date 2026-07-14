"""Sync-engine ABC — the seam between Plum-Audio's app model and the sync library.

Only one implementation ships (SendspinEngine, over PlumSendspinServer), but routing the mesh
through this interface keeps the Router/aggregator from hard-coding aiosendspin calls, so an
aiosendspin pin bump is contained to the engine impl. The contract is exactly the set of
operations the mesh Router needs against the *local* unit; cross-unit coordination (asking a
peer to pull a player) is HTTP the Router does itself, not an engine concern.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from mesh.model import UnitSnapshot


class SyncEngine(ABC):
    """The local unit's sync backbone: sources/groups, player routing, snapshot."""

    @abstractmethod
    def start_source(self, source_id: str, fifo_path: str) -> None:
        """A local source became active — create its anchor group + start its feeder."""

    @abstractmethod
    async def stop_source(self, source_id: str) -> None:
        """A local source went away — tear down its group/feeder."""

    @abstractmethod
    async def attach_local_player(self, source_id: str, player_id: str) -> None:
        """Intra-server route: group an already-connected local player onto a source (live)."""

    @abstractmethod
    async def detach_player(self, source_id: str, player_id: str) -> None:
        """Remove a player from a source group (back to solo)."""

    @abstractmethod
    async def reclaim_remote_player(self, source_id: str, player_id: str, player_url: str) -> bool:
        """Cross-server roam: pull a player off its peer server onto a local source group.

        No DISCOVERY pre-connect counterpart exists: a client holds one websocket, so a playing
        player cannot be warmed on a second server — and the roam is already inaudible (the
        player's jitter buffer covers the reconnect). See PlumSendspinServer.reclaim_remote_player.
        """

    @abstractmethod
    async def set_player_volume(self, player_id: str, volume: int, muted: bool) -> None:
        """Set a player's volume/mute (per-client)."""

    @abstractmethod
    def snapshot(self) -> UnitSnapshot:
        """This unit's local structural state for the aggregator / REST snapshot."""
