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
    # Render level as the PLAYER last reported it (client/state), not as we last commanded it —
    # the player is the source of truth for its own gain, and it persists it across restarts.
    volume: int = 100
    muted: bool = False
    # The roles the server has ACTIVATED for this client, which is not the same as the roles it
    # negotiated. Under aiosendspin 9.x an encrypted-but-unpaired client negotiates its full role
    # set and is activated for none of it, so it appears here, in the group, at the right volume —
    # and renders nothing, with no error at either end. `negotiated` vs `active` is the only signal
    # that separates a working endpoint from a silent one, so it is published rather than left
    # inside the audio process. Empty on a client that is connected but not cleared to play.
    #
    # DEFAULTS to None (not []), so "a peer on an older image that never sends this" is
    # distinguishable from "a peer saying this client is activated for nothing" — the same reason
    # has_player defaults True.
    active_roles: list[str] | None = None
    # How this connection is secured, and therefore whether pairing is even a question for it.
    # `None` means **CLEARTEXT** — a legacy `client/hello` connection, which the server activates
    # straight from the negotiated role set with no pairing and no trust. That is every ESP32
    # speaker, Music Assistant, and our own web GUI, and it is why they are unaffected by any
    # pairing policy. `"sentinel"` is encrypted-but-unauthenticated (the published PSK);
    # `"long_term"` is a real pairing record.
    #
    # So: `security is None` -> never needs pairing. `security == "sentinel"` with empty
    # `active_roles` -> needs pairing. This pair is what the GUI gates its Pair button on, and it
    # is deliberately two fields rather than one enum, because "unknown" (an older peer sending
    # neither) must stay distinguishable from both.
    security: str | None = None
    paired: bool = False

    def to_dict(self) -> dict:
        return {
            "player_id": self.player_id,
            "name": self.name,
            "connected": self.connected,
            "group_id": self.group_id,
            "url": self.url,
            "volume": self.volume,
            "muted": self.muted,
            "active_roles": self.active_roles,
            "security": self.security,
            "paired": self.paired,
        }

    @classmethod
    def from_dict(cls, d: dict) -> PlayerState:
        return cls(
            player_id=d["player_id"],
            name=d.get("name", d["player_id"]),
            connected=bool(d.get("connected", False)),
            group_id=d.get("group_id"),
            url=d.get("url"),
            volume=int(d.get("volume", 100)),
            muted=bool(d.get("muted", False)),
            active_roles=d.get("active_roles"),
            security=d.get("security"),
            paired=bool(d.get("paired", False)),
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
    # The volume ON THE SENDING DEVICE (the phone's AirPlay/Bluetooth slider, the Spotify Connect
    # device volume) — NOT any endpoint's output level. Sendspin has no concept of it (the protocol's
    # controller volume is the group's player volume), so it rides our own snapshot instead.
    # None = this source cannot report/accept one right now; `supports_source_volume` is what the
    # GUI gates the second slider on, so a source with no live sender hides it rather than lying.
    source_volume: int | None = None
    source_muted: bool | None = None
    supports_source_volume: bool = False

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "group_id": self.group_id,
            "group_name": self.group_name,
            "streaming": self.streaming,
            "player_ids": list(self.player_ids),
            # Never emit an empty label: a source with no endpoint name falls back to its id, which
            # also keeps to_dict/from_dict a stable round trip.
            "name": self.name or self.source_id,
            "active": self.active,
            "source_volume": self.source_volume,
            "source_muted": self.source_muted,
            "supports_source_volume": self.supports_source_volume,
        }

    @classmethod
    def from_dict(cls, d: dict) -> SourceState:
        volume = d.get("source_volume")
        muted = d.get("source_muted")
        return cls(
            source_id=d["source_id"],
            group_id=d["group_id"],
            group_name=d.get("group_name", d["group_id"]),
            streaming=bool(d.get("streaming", False)),
            player_ids=list(d.get("player_ids", [])),
            name=d.get("name", "") or d["source_id"],
            active=bool(d.get("active", False)),
            source_volume=None if volume is None else int(volume),
            source_muted=None if muted is None else bool(muted),
            supports_source_volume=bool(d.get("supports_source_volume", False)),
        )


@dataclass
class UnitSnapshot:
    """One unit's complete local view. This is the mesh REST snapshot wire form."""

    unit_id: str
    name: str
    host: str | None  # filled in by the aggregator from the beacon source IP
    sources: list[SourceState] = field(default_factory=list)
    players: list[PlayerState] = field(default_factory=list)
    # This unit's OWN speaker, as reported by the player process itself (mesh/api player-state).
    # `players` above can only list clients attached to THIS server, so a speaker claimed by
    # another server — a peer unit, Music Assistant, any Sendspin server — would otherwise just
    # disappear. The self-report is how the GUI keeps seeing it, and learns what it is playing.
    local_player: dict | None = None
    # Does this unit have an audio output at all? False for an ingest/routing-only node — no player
    # process, no local speaker, nothing to route audio ONTO. It is not the same question as
    # `players == []` or `local_player is None`, both of which are also true for a moment at boot
    # and while a speaker is claimed by another server. Every consumer that has to tell "no speaker
    # here, ever" from "no speaker right now" keys off this.
    #
    # DEFAULTS TRUE, deliberately: a peer running an older image sends a snapshot without the field,
    # and reading that as "playerless" would make every existing unit look like an ingest node.
    has_player: bool = True
    # What this unit calls itself on the network (socket.gethostname()). Peers only ever learn each
    # other's IPs from the beacon, but a person reaches the GUI at `plum-amp100.local` — so without
    # this a peer cannot recognise a page served BY this unit as a legitimate origin. See
    # cors_policy.known_hosts.
    hostname: str | None = None
    # This unit's SENDSPIN server id, which under aiosendspin 9.x is its X25519 public key and is
    # NOT `unit_id`. They used to be the same string — we passed `server_id=unit_id` — and a good
    # deal of the mesh quietly relied on that, most importantly `follow`, which joins the server a
    # player reports itself attached to against this table. Publishing it is what makes that join
    # possible again; `MeshView.unit_by_server_id` is the lookup. None for a peer that has not
    # started its server yet.
    server_id: str | None = None
    # The unit this one is slaved to (`autoSwitch.slave.masterUnitId`), or None if it follows
    # nobody. Published because follow config lives on the FOLLOWER, so without it no other unit can
    # tell a room that is locked to this one from a room that merely joined the same stream by hand.
    # Loudness matching's default scope is exactly that distinction — see mesh/loudness.py. Cheap to
    # carry (one string) and read-only for every consumer but the follower itself.
    follows_unit_id: str | None = None
    # This unit's stored `audio.calibration` map, verbatim. Published because the GUI can only write
    # calibration to the unit serving the page, while matching runs on whichever unit owns the
    # GROUP — see calibration.merge_calibrations. Small (a few hundred bytes per endpoint) and
    # read-only for every consumer but the owning unit.
    calibration: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "unit_id": self.unit_id,
            "name": self.name,
            "host": self.host,
            "sources": [s.to_dict() for s in self.sources],
            "players": [p.to_dict() for p in self.players],
            "local_player": self.local_player,
            "has_player": self.has_player,
            "hostname": self.hostname,
            "server_id": self.server_id,
            "follows_unit_id": self.follows_unit_id,
            "calibration": self.calibration,
        }

    @classmethod
    def from_dict(cls, d: dict) -> UnitSnapshot:
        return cls(
            unit_id=d["unit_id"],
            name=d.get("name", d["unit_id"]),
            host=d.get("host"),
            sources=[SourceState.from_dict(s) for s in d.get("sources", [])],
            players=[PlayerState.from_dict(p) for p in d.get("players", [])],
            local_player=d.get("local_player"),
            has_player=bool(d.get("has_player", True)),
            hostname=d.get("hostname"),
            server_id=d.get("server_id"),
            follows_unit_id=d.get("follows_unit_id"),
            calibration=d.get("calibration") or {},
        )


@dataclass
class MeshView:
    """The aggregated view across all reachable units (self + live peers)."""

    units: list[UnitSnapshot] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"units": [u.to_dict() for u in self.units]}

    def unit(self, unit_id: str) -> UnitSnapshot | None:
        return next((u for u in self.units if u.unit_id == unit_id), None)

    def unit_by_server_id(self, server_id: str | None) -> UnitSnapshot | None:
        """The unit whose SENDSPIN server has this id, or None if it is not one of ours.

        Under 9.x a server id is an X25519 public key, so it is a different namespace from `unit_id`
        and this is the only way back. Deliberately strict — no fall-through to `unit()` — because
        the answer "not one of our units" is meaningful here rather than an error: it is how a player
        attached to Music Assistant or any other foreign Sendspin server is recognised as busy but
        unroutable. Matching a peer id against the unit table by accident would read a foreign server
        as one of ours and hand its speaker away.
        """
        if not server_id:
            return None
        return next((u for u in self.units if u.server_id == server_id), None)

    def unit_by_own_player(self, player_id: str | None) -> UnitSnapshot | None:
        """The unit whose OWN speaker this is, from its `local_player` self-report.

        The self-report is the only authoritative statement of "this speaker belongs to this unit".
        `players` cannot answer it: that list is every client attached to a unit's server, which
        after an adopt includes third-party speakers and after a roam includes other units'.

        This is the test for "is this one of ours", and it matters most on the pairing path — our
        own players are never cleartext (CLAUDE.md), so a hit here is positive evidence that a
        pairing handshake is safe against this id, where a miss is not evidence of anything.
        """
        if not player_id:
            return None
        for unit in self.units:
            local = unit.local_player or {}
            if local.get("player_id") == player_id:
                return unit
        return None

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
