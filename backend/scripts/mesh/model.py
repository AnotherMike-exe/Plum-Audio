#!/usr/bin/env python3
"""
Plum-Audio mesh — the normalized multi-unit state model.

One `UnitSnapshot` is a single unit's local view (its sources/groups + the players currently
connected to its server). The aggregator polls each peer's snapshot (over the mesh REST API,
peers found via `discovery`) and merges them into a `MeshView` — the unified
servers/streams/players model the frontend renders and the router plans against.

These are plain dataclasses with `to_dict`/`from_dict` so a snapshot travels over HTTP as JSON
unchanged. Keep this structural (who ingests what, which players are grouped where, is it
streaming) — now-playing metadata rides its own Sendspin metadata/artwork role to the GUI and is
deliberately *not* duplicated here.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PlayerState:
    """A render endpoint connected to a unit's server."""

    player_id: str
    name: str
    connected: bool
    group_id: str | None  # the group it currently renders, if any
    url: str | None = None  # the player's own LAN listener URL (how any server reclaims it)

    def to_dict(self) -> dict:
        return {
            "player_id": self.player_id,
            "name": self.name,
            "connected": self.connected,
            "group_id": self.group_id,
            "url": self.url,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PlayerState":
        return cls(
            player_id=d["player_id"],
            name=d.get("name", d["player_id"]),
            connected=bool(d.get("connected", False)),
            group_id=d.get("group_id"),
            url=d.get("url"),
        )


@dataclass
class SourceState:
    """A local audio source and the group it feeds (one source == one anchor group)."""

    source_id: str
    group_id: str
    group_name: str
    streaming: bool  # feeder is pushing audio right now
    player_ids: list[str] = field(default_factory=list)  # players attached to this group
    # Display name = the endpoint's device name ("Kitchen"), so the GUI shows what the user named
    # in Settings rather than the internal source_id. Follows a rename live.
    name: str = ""
    # A sender is actually using this source (audio arrived recently and the writer is still open).
    # Idle sources stay routable but drop out of the GUI's stream list — see SourceFeeder.is_active.
    active: bool = False

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "group_id": self.group_id,
            "group_name": self.group_name,
            "streaming": self.streaming,
            "player_ids": list(self.player_ids),
            "name": self.name,
            "active": self.active,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SourceState":
        return cls(
            source_id=d["source_id"],
            group_id=d["group_id"],
            group_name=d.get("group_name", d["group_id"]),
            streaming=bool(d.get("streaming", False)),
            player_ids=list(d.get("player_ids", [])),
            name=d.get("name", "") or d["source_id"],
            active=bool(d.get("active", False)),
        )


@dataclass
class UnitSnapshot:
    """One unit's complete local view. This is the mesh REST snapshot wire form."""

    unit_id: str
    name: str
    host: str | None  # filled in by the aggregator from the beacon source IP
    sources: list[SourceState] = field(default_factory=list)
    players: list[PlayerState] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "unit_id": self.unit_id,
            "name": self.name,
            "host": self.host,
            "sources": [s.to_dict() for s in self.sources],
            "players": [p.to_dict() for p in self.players],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "UnitSnapshot":
        return cls(
            unit_id=d["unit_id"],
            name=d.get("name", d["unit_id"]),
            host=d.get("host"),
            sources=[SourceState.from_dict(s) for s in d.get("sources", [])],
            players=[PlayerState.from_dict(p) for p in d.get("players", [])],
        )


@dataclass
class MeshView:
    """The aggregated view across all reachable units (self + live peers)."""

    units: list[UnitSnapshot] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"units": [u.to_dict() for u in self.units]}

    def unit(self, unit_id: str) -> UnitSnapshot | None:
        return next((u for u in self.units if u.unit_id == unit_id), None)

    def find_source(self, source_id: str) -> tuple[UnitSnapshot, SourceState] | None:
        """Locate which unit ingests a given source (audio stays on its ingesting unit)."""
        for u in self.units:
            for s in u.sources:
                if s.source_id == source_id:
                    return u, s
        return None

    def find_player(self, player_id: str) -> tuple[UnitSnapshot, PlayerState] | None:
        """Locate a player's home unit (the server it is currently connected to).

        Prefers a connected entry: during a roam a player can momentarily appear on both its old
        and new unit, so a connected match always wins over a stale/disconnected one.
        """
        fallback: tuple[UnitSnapshot, PlayerState] | None = None
        for u in self.units:
            for p in u.players:
                if p.player_id == player_id:
                    if p.connected:
                        return u, p
                    fallback = fallback or (u, p)
        return fallback
