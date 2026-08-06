#!/usr/bin/env python3
"""
Plum-Audio mesh — FollowReconciler: auto-route-on-connect and auto-follow ("slave" mode).

Ported from Plum-Snapcast, which never carried these two behaviors over to the Sendspin mesh:

  localActivity  when this unit's OWN player is idle and one of its OWN local sources becomes
                 active — a NEW connection, an inactive->active edge, not a source that was already
                 streaming — route the player onto it. So "just AirPlay to the kitchen speaker"
                 needs no manual routing, but selecting None while AirPlay still streams sticks
                 (it is not re-grabbed, since that source is not a new connection).
  slave          mirror another unit's player (`masterUnitId`): follow whatever stream it is on,
                 and follow it into idle when it stops. A LOCAL override always wins — once the
                 user moves the follower off what follow put it on (to None or another source),
                 follow keeps hands off until the follower is manually back in sync with the master
                 (or slave mode is turned off).

Like the rest of the mesh, there is no event/callback for "a source went active" or "a player was
routed" — everything here is derived by polling `DataAggregator.view()` each tick, same shape as
`DataAggregator._poll_loop` and `SourceManagerBase._run` (settings.json is read fresh every tick
too, so a GUI settings change applies live, no restart).

Both the follower's own status and any leader's status are read from `UnitSnapshot.local_player`
— each unit's PLAYER self-reports where it is attached and what group it is in (see
`mesh/api.py`'s `_player_state`), which is the only way to learn this when the player has roamed
onto a different unit's server (or a leader's player has roamed onto a THIRD unit). If a leader's
speaker is attached to a server outside our mesh (Music Assistant, any foreign Sendspin server),
there is no `source_id` to route onto — follow is a no-op until it comes back, same graceful
degradation as when the leader unit is simply offline.

A `source_id` is only unique WITHIN a unit (every unit's own AirPlay endpoint is happily also
"airplay-1"), so a "target" is tracked as an `(owning_unit_id, source_id)` pair EVERYWHERE in this
file, never a bare source_id — comparing bare ids would silently treat our own idle home source as
"the same" as a same-named leader source on another unit. For the same reason, routing onto a
leader's source can never go through `Router.route_player` called in-process here: that resolves
the bare id against OUR OWN view, where OUR OWN same-named source sorts first, and would silently
attach the player locally instead of following the leader. The peer-delegate primitive `Router`'s
own Path 3 uses (`MeshClient.delegate_route`, via `MeshDiscovery.get_peer`) is used explicitly here
instead, keyed on the target's owning unit — never inferred from the ambiguous source_id.

A local override is tracked with an explicit `_overridden` flag: the follower's current target
differing from `_last_auto_target` (the target this reconciler itself last routed to) means the
user moved it — to None or another source. That flag suppresses all auto-follow. It is lifted when
(a) the follower is back on the master's current stream (a manual re-join), (b) slave mode is off,
or (c) the MASTER goes idle/none WHILE the follower is itself idle/none — the master session is a
natural reset point, so a follower left on None re-follows the master's next stream automatically.
A follower actively playing ANOTHER source keeps its override across a master reset.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os

from mesh.aggregator import DataAggregator
from mesh.model import MeshView
from mesh.router import PeerProvider, RemoteDelegate, RouteError, Router

logger = logging.getLogger("plum.mesh.follow")

POLL_INTERVAL_S = 2.0


class FollowReconciler:
    def __init__(
        self,
        aggregator: DataAggregator,
        router: Router,
        *,
        local_unit_id: str,
        local_player_id: str,
        peer_provider: PeerProvider,
        delegate: RemoteDelegate,
        unroute_delegate: RemoteDelegate | None = None,
        settings_file: str | None = None,
        poll_interval: float = POLL_INTERVAL_S,
    ) -> None:
        self._aggregator = aggregator
        self._router = router
        self._local_unit_id = local_unit_id
        self._local_player_id = local_player_id
        self._peer = peer_provider
        self._delegate = delegate
        self._unroute_delegate = unroute_delegate
        self.settings_file = settings_file or os.environ.get("PLUM_SETTINGS_FILE", "/data/settings.json")
        self.poll_interval = poll_interval
        # (owning_unit_id, source_id) this reconciler itself last routed to.
        self._last_auto_target: tuple[str, str] | None = None
        # Local sources that were active on the previous tick, so localActivity fires only on a
        # NEW connection (inactive->active edge) — not on a source that was already streaming when
        # the player went idle (which would re-grab it the instant the user selects None). None on
        # the very first tick means "no history yet" — treat everything already active as existing.
        self._prev_active: set[str] | None = None
        # A LOCAL override always wins over auto-follow: once the user moves the follower off what
        # follow put it on (to None or another source), follow keeps hands off until the follower is
        # back in sync with the master (a manual re-join) or slave mode is turned off. Without this,
        # selecting None on a slaved unit is re-followed on the very next tick.
        self._overridden = False
        self._task: asyncio.Task | None = None
        self._stop_evt = asyncio.Event()

    # -- lifecycle -------------------------------------------------------------------------------

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        self._stop_evt.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stop_evt.is_set():
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - a bad cycle must never kill the loop
                logger.exception("follow reconciler: cycle failed; retrying next tick")
            await asyncio.sleep(self.poll_interval)

    # -- reconcile ---------------------------------------------------------------------------------

    def _read_settings(self) -> dict | None:
        try:
            with open(self.settings_file, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    async def tick(self) -> None:
        """One reconcile cycle. Public (not `_tick`) so unit tests can drive it directly."""
        if self._local_player_id is None:
            # This unit has no speaker, so it has nothing to auto-route or auto-follow. Belt and
            # braces: sendspin_server does not construct a reconciler for such a unit at all, but
            # both paths below end in routing a player id that resolves to nothing — a RouteError
            # with a stack trace per source activation, or in slave mode a re-route every tick
            # forever. Leading is unaffected; that is passive, read from our snapshot by the peers.
            return
        settings = self._read_settings()
        if settings is None:
            return
        auto = settings.get("autoSwitch") or {}
        view = self._aggregator.view()
        idle, current_target = self._player_status(view, self._local_unit_id)

        # Rising-edge detection for localActivity: only sources that JUST became active count as a
        # "new connection". A source already active last tick is "existing" and must not be grabbed.
        my_unit = view.unit(self._local_unit_id)
        now_active = {s.source_id for s in my_unit.sources if s.active} if my_unit is not None else set()
        prev_active = self._prev_active if self._prev_active is not None else now_active
        self._prev_active = now_active

        if idle and auto.get("localActivity") and my_unit is not None:
            for src in my_unit.sources:
                if src.active and src.source_id not in prev_active and self._local_player_id not in src.player_ids:
                    await self._route(src.source_id, self._local_unit_id)
                    # A local grab counts as an override, or slave mode undoes it on the very next
                    # tick: the follower is then on a source that IS `_last_auto_target`, so the
                    # "did the user move us?" check below compares equal, `_overridden` never trips,
                    # and it routes straight back to the master. With both toggles on, the player
                    # ping-pongs — and every hop calls refresh_stream(), so listeners get a
                    # discontinuity and the visualizer drops to zero, twice, per local activation.
                    # Observed on .2.10 (2026-08-06), which has both enabled with master .11.
                    #
                    # Local winning is the DOCUMENTED intent — PlaybackTab's own copy says "Local
                    # connections always take priority". The override clears the usual way, when the
                    # master goes idle and this follower is idle too.
                    self._overridden = True
                    return  # one action per tick; re-derive fresh state next cycle

        slave = auto.get("slave") or {}
        master_unit_id = slave.get("masterUnitId")
        if not (slave.get("enabled") and master_unit_id):
            self._overridden = False  # follow off: nothing to override
            return

        leader_target = self._leader_status(view, master_unit_id)

        # Master idle/none is a natural reset point for the follow relationship — the master session
        # ended. Handle it BEFORE the override check so it can lift a stale opt-out.
        if leader_target is None:
            if idle:
                # Follower is idle/none too: its opt-out expires, so the master's NEXT stream is
                # followed again without a manual re-join.
                self._overridden = False
                self._last_auto_target = None
            elif current_target is not None and current_target == self._last_auto_target:
                # Follower was still on the master's now-stopped stream: follow it into idle.
                await self._unroute(current_target[1], current_target[0])  # clears _last_auto_target
            # else: follower is actively on ANOTHER source (a manual override) — leave it, and keep
            # the override, per the user's rule that only an idle follower resets on master-idle.
            return

        # In sync with the master (follow put us there, or the user manually re-joined it): clear any
        # override and remember the target.
        if current_target == leader_target:
            self._overridden = False
            self._last_auto_target = leader_target
            return

        # We're NOT on the master's stream while it IS playing. If we're somewhere other than where
        # follow last put us, the user moved us (to None or another source) — a LOCAL override, which
        # always wins while the master keeps this stream.
        if current_target != self._last_auto_target:
            self._overridden = True
        if self._overridden:
            return  # respect the local choice; do not auto-follow

        await self._route(leader_target[1], leader_target[0])

    async def _route(self, source_id: str, owning_unit_id: str) -> None:
        """Route our player onto `source_id`, owned by `owning_unit_id`.

        `owning_unit_id` is known precisely by the caller (our own unit for localActivity, the
        leader's ACTUAL current unit for follow — which `_player_status` resolves from the
        leader's own self-report, so it is correct even if the leader itself has roamed onto a
        third unit — never re-derived from the source_id, which is only unique within a unit).
        Local goes through our own Router as usual; anything else is delegated straight to the
        owning unit's mesh API, the same primitive Router's own cross-unit path uses, so its
        ambiguous same-process source lookup never gets a chance to misfire.
        """
        try:
            if owning_unit_id == self._local_unit_id:
                ok = await self._router.route_player(self._local_player_id, source_id)
            else:
                peer = self._peer(owning_unit_id)
                if peer is None:
                    logger.debug("follow: leader %s not currently discoverable; skipping this tick", owning_unit_id)
                    return
                ok = await self._delegate(peer, source_id, self._local_player_id)
        except RouteError:
            logger.exception("follow: routing %s onto %s (%s) failed", self._local_player_id, source_id, owning_unit_id)
            return
        if ok:
            self._last_auto_target = (owning_unit_id, source_id)
            logger.info("follow: routed %s -> %s (%s)", self._local_player_id, source_id, owning_unit_id)

    async def _unroute(self, source_id: str, owning_unit_id: str) -> None:
        """Detach our player from `source_id` (owned by `owning_unit_id`) so it goes idle — used to
        follow a leader into idle. Local via our Router; remote via the unroute delegate."""
        try:
            if owning_unit_id == self._local_unit_id:
                await self._router.unroute_player(self._local_player_id, source_id)
                ok = True
            elif self._unroute_delegate is None:
                return
            else:
                peer = self._peer(owning_unit_id)
                if peer is None:
                    return
                ok = await self._unroute_delegate(peer, source_id, self._local_player_id)
        except RouteError:
            logger.exception(
                "follow: unrouting %s from %s (%s) failed", self._local_player_id, source_id, owning_unit_id
            )
            return
        if ok:
            self._last_auto_target = None
            logger.info(
                "follow: unrouted %s from %s (%s) — followed leader into idle",
                self._local_player_id,
                source_id,
                owning_unit_id,
            )

    @staticmethod
    def _player_status(view: MeshView, unit_id: str) -> tuple[bool, tuple[str, str] | None]:
        """(is_idle, (owning_unit_id, source_id) or None) for `unit_id`'s own player, from its
        self-report.

        A unit's `local_player` is pushed by the PLAYER process itself (mesh/api.py
        `_player_state`), so it stays correct even when that player has roamed onto a different
        unit's server (or, for a leader, a third unit's — `owning_unit_id` in the result is
        wherever it ACTUALLY is, not necessarily `unit_id`). "Idle" here means not actually playing
        anything right now — ungrouped, or grouped to a source that has since gone quiet — not
        merely "never been routed," so a player is freed to be auto-managed again the moment its
        current source stops, matching the idle contract elsewhere in the mesh.
        """
        unit = view.unit(unit_id)
        lp = unit.local_player if unit else None
        if not lp or not lp.get("attached") or not lp.get("group_id"):
            return True, None
        server_unit = view.unit(lp.get("server_id"))
        if server_unit is None:
            # Attached to a server outside our mesh (Music Assistant, any foreign Sendspin server):
            # busy, but nothing we can route onto.
            return False, None
        src = next((s for s in server_unit.sources if s.group_id == lp["group_id"]), None)
        if src is None or not src.active:
            return True, None
        return False, (server_unit.unit_id, src.source_id)

    @staticmethod
    def _leader_status(view: MeshView, unit_id: str) -> tuple[str, str] | None:
        """(owning_unit_id, source_id) that a LEADER is currently the source of, or None if idle.

        A leader WITH a speaker self-reports where that speaker is, which is authoritative even when
        it has roamed onto a third unit — so for those, this is exactly `_player_status`.

        A leader with NO speaker cannot self-report anything: `local_player` is permanently None, and
        the player path reads that as "idle", which the caller treats as "the master's session
        ended" and responds to by UNROUTING every follower. So an ingest-only unit — whose entire
        job is to be followed — would kick its followers off within one tick of them joining. For
        that unit the leader's stream is derived from what it is actually INGESTING instead.

        Among several concurrently active sources, prefer the one with the most endpoints already
        attached, tie-broken by source_id. Two followers must reach the SAME answer independently,
        and it must not oscillate: the first follower to join raises that source's count, so the
        preference reinforces itself rather than flip-flopping. There is deliberately no way for the
        leader to nominate a source — see the note in docs/CLAUDE.md.
        """
        unit = view.unit(unit_id)
        if unit is None:
            return None
        if unit.has_player:
            return FollowReconciler._player_status(view, unit_id)[1]
        active = sorted(
            (s for s in unit.sources if s.active),
            key=lambda s: (-len(s.player_ids), s.source_id),
        )
        return (unit.unit_id, active[0].source_id) if active else None
