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
    async def reclaim_remote_player(
        self, source_id: str, player_id: str, player_url: str, *, stage_pairing: bool = False
    ) -> bool:
        """Cross-server roam: pull a player off its peer server onto a local source group.

        No DISCOVERY pre-connect counterpart exists: a client holds one websocket, so a playing
        player cannot be warmed on a second server — and the roam is already inaudible (the
        player's jitter buffer covers the reconnect). See PlumSendspinServer.reclaim_remote_player.
        """

    @abstractmethod
    async def set_player_volume(self, player_id: str, volume: int, muted: bool) -> None:
        """Set a player's volume/mute (per-client)."""

    async def set_source_volume(self, source_id: str, volume: int | None = None, muted: bool | None = None) -> None:
        """Set the volume/mute ON THE SENDING DEVICE feeding a source (AirPlay/BT/Spotify).

        Distinct from `set_player_volume`, which is our own output gain. Optional: an engine whose
        sources have no back-channel to their sender simply doesn't implement it.
        """
        raise NotImplementedError

    @abstractmethod
    async def adopt_client(self, source_id: str, url: str, player_id: str | None = None) -> str | None:
        """Dial a foreign Sendspin speaker (discovered by mDNS) onto a source. Optional."""
        raise NotImplementedError

    async def release_client(self, source_id: str, player_id: str, url: str | None = None) -> None:
        """Hand a foreign speaker back to whatever server had it. Optional."""
        raise NotImplementedError

    # -- pairing. Optional: an engine whose protocol has no notion of it implements none of these.

    async def pair_client(self, client_id: str, method: str, token: str | None = None) -> None:
        """Begin an operator-initiated pairing attempt with a connected client.

        `token` carries the device's pairing token for the no-interaction `pairing_psk` method.
        """
        raise NotImplementedError

    def submit_pin(self, client_id: str, pin: str) -> bool:
        """Hand a waiting attempt the PIN the operator typed. False if nothing is waiting."""
        raise NotImplementedError

    async def cancel_pairing(self, client_id: str) -> None:
        """Abandon an attempt without finalising it."""
        raise NotImplementedError

    async def unpair_client(self, client_id: str) -> None:
        """Drop the pairing record both ends hold."""
        raise NotImplementedError

    async def open_pairing_window(self, client_id: str) -> bool:
        """Stand in for the operator's gesture on a client we are already paired with."""
        raise NotImplementedError

    def pairing_state(self, client_id: str | None = None) -> dict:
        """The last pairing outcome per client, for a GUI that is waiting on one."""
        raise NotImplementedError

    def snapshot(self) -> UnitSnapshot:  # noqa: B027 - optional hook; an engine with no structural view may leave it
        """This unit's local structural state for the aggregator / REST snapshot."""
