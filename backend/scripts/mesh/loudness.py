#!/usr/bin/env python3
"""
Plum-Audio mesh — LoudnessReconciler: keep grouped endpoints at the same loudness in the room.

Two endpoints at the same volume percentage are not the same loudness: speaker sensitivity, amp
gain, distance and room gain all differ and none of it is visible to the protocol. Once each
endpoint has a measured curve (see calibration.py), "make the kitchen as loud as the living room"
is arithmetic — this is where it is applied.

THE MODEL. A group has a transient target loudness `T`. Endpoint i renders at
`invert_i(T + trim_i)`, clamped to its own ceiling. Moving ANY endpoint's slider sets `T` from that
endpoint's own curve (its trim removed first, so a room pinned 3 dB quiet does not drag the house
down by 3 dB every time it is the one you touched) and every other member re-derives. `trim_i` is
persistent per-room taste, edited in the calibration UI rather than by dragging, which is what lets
"the kitchen is always a little quieter" survive every re-derive.

HOW "THE USER MOVED THIS ONE" IS DETECTED. There is no event for it. The volume POST can land on
any unit — the GUI addresses the unit that owns the player — and routing and follow both happen with
no browser open at all, so a GUI-side implementation would be wrong in several directions at once.
Instead this remembers the volume it last COMMANDED for each player and watches for the mesh view
reporting something else: that divergence is a human. It costs up to one poll interval of settle
after the slider is released, and it needs no new cross-unit plumbing.

WHERE IT RUNS. On the unit that owns the SOURCE, over that source's own group members. That is not
an arbitrary choice: `set_player_volume` resolves a client on the local server, so a unit can only
drive the players attached to its own groups — which is exactly `SourceState.player_ids`. So every
member is driven by exactly one unit and two reconcilers can never fight over one speaker.

WHY IT IS A POLLING RECONCILER rather than a hook in `attach_player`. Same reason FollowReconciler
is: the audio hot path stays untouched, membership changes and volume changes are handled by one
mechanism rather than two, `tick()` is directly unit-testable, and the whole thing degrades to a
no-op for uncalibrated endpoints. The cost is the settle delay above.

DELIBERATE EXCEPTION TO THE "DO NOT FAN OUT PER CLIENT" RULE. CLAUDE.md says group volume belongs to
the library's delta-preserving redistribution and must not be reimplemented per client. That rule is
about the GROUP slider, which stays exactly as it was. This is a different quantity: a per-endpoint
correction the protocol has no concept of, which by definition cannot be expressed as one group
level. It is confined to groups whose members are calibrated AND in scope, so a mesh with no
calibration behaves identically to one without this file.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os

from calibration import (
    EndpointCalibration,
    MatchPolicy,
    match_volume,
    matching_partition,
    merge_calibrations,
    target_db_for,
)
from calibration_tone import CAL_SOURCE_PREFIX

from mesh.aggregator import DataAggregator
from mesh.model import MeshView, UnitSnapshot
from mesh.router import Router

logger = logging.getLogger("plum.mesh.loudness")

POLL_INTERVAL_S = 2.0

# A commanded level and the level the view reports back differ by rounding and by the player's own
# echo timing, so only a difference bigger than this counts as a human moving a slider.
USER_INTENT_EPSILON = 2

# Below this the endpoint is effectively off, and re-deriving partners from a target of "silence"
# would drag a whole group to zero because one room was muted. Muting one endpoint must mute that
# endpoint, nothing else.
MIN_REFERENCE_VOLUME = 2


class LoudnessReconciler:
    """Polls the mesh view and holds calibrated group members at a matched loudness."""

    def __init__(
        self,
        aggregator: DataAggregator,
        router: Router,
        *,
        local_unit_id: str,
        settings_file: str | None = None,
        poll_interval: float = POLL_INTERVAL_S,
        tone_player_provider=None,
        on_calibration_export=None,
    ) -> None:
        self._aggregator = aggregator
        self._router = router
        self._local_unit_id = local_unit_id
        self.settings_file = settings_file or os.environ.get("PLUM_SETTINGS_FILE", "/data/settings.json")
        self.poll_interval = poll_interval
        # A calibration tone deliberately drives one endpoint to a level that has nothing to do with
        # the group's. Without this the matcher would read that as the user moving a slider and
        # re-level the whole house to the tone, mid-measurement.
        self._tone_player = tone_player_provider or (lambda: None)
        # Republish this unit's own stored map into its snapshot. The GUI can only write calibration
        # to the unit serving the page, so without this a record made on one unit's page would be
        # invisible to the unit that actually owns the group — and matching would appear to work or
        # not depending on which page you happened to open.
        self._on_calibration_export = on_calibration_export
        self._exported: dict | None = None
        # player_id -> the volume THIS reconciler last commanded. The view reporting anything else
        # is how a human is detected; see the module docstring.
        self._commanded: dict[str, int] = {}
        # Endpoints we have commanded whose echo has not come back yet. They are NOT readable as
        # user intent while in here, and that covers two cases with one rule. The benign one is
        # lag: our command has not landed, so the reported level is stale rather than deliberate.
        # The damaging one is an endpoint that never echoes at all — a server cannot READ a
        # speaker's volume, only command it (docs/SPEC-CONFORMANCE.md), and only our own player
        # echoes `client/state` because we made it. A third-party speaker reports its connect-time
        # level forever, so the divergence never closes; without this it would read as "a human
        # moved this" on every tick, and being frozen high it would win `max(moved)` and peg its
        # whole group to its 100% loudness. An endpoint that does echo clears itself within a tick
        # or two and is eligible again.
        self._unconfirmed: set[str] = set()
        # group key -> the target dB currently held, so a membership change re-levels the joiner to
        # the group rather than re-deriving the group from whoever happens to sort first.
        self._targets: dict[str, float] = {}
        # Endpoints clamped to their ceiling this cycle: the GUI badges these rows so a speaker that
        # physically cannot keep up is visible rather than just quietly wrong.
        self._at_limit: set[str] = set()
        # A volume the user just asked for, reported by the volume route the moment it happens.
        # The poll cannot see it yet — the mesh view is a 2 s cache and the player's echo has not
        # arrived — so an immediate tick that only re-read state would find nothing changed and do
        # nothing. Carrying the intent is what makes the fast path actually fast.
        self._user_intent: tuple[str, int] | None = None
        self._nudge: asyncio.Task | None = None
        # Set whenever an intent arrives; cleared by the nudge task as it begins a cycle. If an
        # intent lands while a cycle is already past the point where it reads one, this is what
        # makes the task go round again instead of silently dropping it — measured on the rig, that
        # drop cost a full poll interval and was the difference between 3 ms and 2.95 s.
        self._nudge_pending = False
        self._settings_stamp: int | None = None
        self._settings_cache: dict | None = None
        self._task: asyncio.Task | None = None
        self._stop_evt = asyncio.Event()

    # -- lifecycle -------------------------------------------------------------------------------

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        self._stop_evt.set()
        if self._nudge is not None:
            self._nudge.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._nudge
            self._nudge = None
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def note_user_volume(self, player_id: str, volume: int) -> None:
        """A volume the USER asked for, reported at the moment it is applied.

        Detecting intent by divergence costs up to a whole poll interval, which is the entire lag
        between moving one slider and the rest of a group following. A volume request IS the event,
        so the route hands it straight here.

        Always the right unit: `Router.set_volume` only resolves a client on the local server, and a
        player attached to a group is by definition connected to the server that owns that group —
        so the unit receiving a volume POST is always the unit whose matcher cares about it.

        Divergence detection stays, because it catches the changes this cannot see: a third-party
        controller such as Music Assistant commanding one of our players, or anything else that
        moves a level without coming through our API.
        """
        if not player_id:
            return
        self._user_intent = (player_id, max(0, min(100, int(volume))))
        self._nudge_pending = True
        if self._nudge is not None and not self._nudge.done():
            return  # a cycle is running; the pending flag makes it go round again for this intent
        self._nudge = asyncio.ensure_future(self._nudge_once())

    async def _nudge_once(self) -> None:
        """Reconcile now, and again if another intent arrived while we were doing it.

        Coalescing to a single in-flight task is right, but only if a request that lands mid-cycle
        is still honoured. Simply returning early would drop it whenever the running cycle had
        already read `_user_intent` — which on the rig meant one slider move in three fell back to
        the poll and took 2.95 s while its neighbours took 3 ms.
        """
        while self._nudge_pending and not self._stop_evt.is_set():
            self._nudge_pending = False
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - never surfaces in the caller's HTTP response
                logger.exception("loudness reconciler: nudged cycle failed")

    async def _run(self) -> None:
        while not self._stop_evt.is_set():
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - a bad cycle must never kill the loop
                logger.exception("loudness reconciler: cycle failed; retrying next tick")
            await asyncio.sleep(self.poll_interval)

    # -- reconcile -------------------------------------------------------------------------------

    def _read_settings(self) -> dict | None:
        """settings.json, re-read only when it has actually changed.

        This runs on the AUDIO event loop, so the read competes with the feeder's 20 ms commit
        cadence for the same thread — and on a class-10 SD card being written concurrently by the
        Flask settings API, an unlucky read is not free. The mtime check makes the steady state a
        single stat() instead of a parse. FollowReconciler polls the same file on the same interval;
        this deliberately does not double that cost.
        """
        try:
            stamp = os.stat(self.settings_file).st_mtime_ns
        except OSError:
            return None
        if stamp == self._settings_stamp and self._settings_cache is not None:
            return self._settings_cache
        try:
            with open(self.settings_file, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        self._settings_stamp = stamp
        self._settings_cache = data
        return data

    def status(self) -> dict:
        """Introspection for tests and logs: each group's held target, and who is at their ceiling.

        Deliberately NOT a REST surface. The GUI derives "at limit" itself from the endpoint's
        resolved ceiling and its current level, both of which it already holds — exposing this would
        add a poll to learn something the client can compute.
        """
        return {
            "targets": dict(self._targets),
            "atLimit": sorted(self._at_limit),
        }

    async def tick(self) -> None:
        """One reconcile cycle. Public (not `_tick`) so unit tests can drive it directly."""
        settings = self._read_settings()
        if settings is None:
            return
        audio = settings.get("audio") or {}
        local_map = audio.get("calibration") or {}
        if local_map != self._exported:
            self._exported = local_map
            if self._on_calibration_export is not None:
                self._on_calibration_export(local_map)

        policy = MatchPolicy.from_dict(audio.get("loudnessMatch"))
        if policy.mode == "off":
            self._targets.clear()
            self._at_limit = set()
            return

        view = self._aggregator.view()
        my_unit = view.unit(self._local_unit_id)
        if my_unit is None:
            return

        # Every unit's map, not just ours — see merge_calibrations for why the GUI cannot write
        # directly to the unit that owns the group.
        calibrations = merge_calibrations([local_map, *(u.calibration for u in view.units)])
        if not calibrations:
            return

        tone_player = self._tone_player()
        follow_members = self._follow_members(view, my_unit)
        at_limit: set[str] = set()
        live_keys: set[str] = set()
        live_players: set[str] = set()

        for source in my_unit.sources:
            # A calibration tone is a deliberate one-endpoint level; never match against it.
            if source.source_id.startswith(CAL_SOURCE_PREFIX):
                continue
            for label, members in matching_partition(list(source.player_ids), policy, follow_members):
                # Keyed on the partition LABEL, never on a member id: membership order comes from
                # the library's client iteration and changes on any detach, so a member-derived key
                # churns — and a churned key is pruned as stale, which silently re-baselines the
                # group from whoever happens to sort first. Toning one member triggers exactly that,
                # since it pulls that member out of the group.
                key = f"{source.source_id}|{label}"
                live_keys.add(key)
                live_players.update(members)
                await self._reconcile_group(key, members, calibrations, view, tone_player, at_limit)

        # Drop remembered targets for groups that no longer exist, so a regrouped set of speakers
        # starts from whoever the user moves rather than a stale level from an old session.
        for stale in set(self._targets) - live_keys:
            self._targets.pop(stale, None)

        # And drop the PER-PLAYER memory for anyone no longer in a matched group. Without this, an
        # endpoint that leaves, gets turned up by hand while solo, and rejoins looks like "a human
        # just moved this" the instant it comes back — because the level we last commanded is still
        # remembered from its previous membership — so it becomes the reference and drags the whole
        # group to itself. That is the opposite of the documented intent for a joiner.
        for stale_player in set(self._commanded) - live_players:
            self._commanded.pop(stale_player, None)
            self._unconfirmed.discard(stale_player)
        self._at_limit = at_limit

    def _follow_members(self, view: MeshView, my_unit: UnitSnapshot) -> frozenset[str]:
        """Player ids belonging to this unit and to every unit slaved to it.

        `follows_unit_id` is published in each unit's snapshot precisely so this is answerable: the
        follow setting lives on the FOLLOWER, so without it a leader cannot tell a room locked to it
        from a room that joined the same stream by hand — which is the whole distinction the default
        scope rests on.

        Only each unit's OWN speaker counts, read from its `local_player` self-report. A unit's
        `players` list is every client attached to its server, which after a roam includes speakers
        belonging to units that follow nobody — using it would sweep exactly the rooms this scope
        exists to leave alone into the match. A third-party speaker has no unit and so is never in
        the follow set, which is correct: it follows nothing.
        """
        units = [u for u in view.units if u.unit_id == my_unit.unit_id or u.follows_unit_id == my_unit.unit_id]
        members: set[str] = set()
        for unit in units:
            local = unit.local_player or {}
            player_id = local.get("player_id")
            if isinstance(player_id, str) and player_id:
                members.add(player_id)
        return frozenset(members)

    async def _reconcile_group(
        self,
        key: str,
        members: list[str],
        calibrations: dict[str, EndpointCalibration],
        view: MeshView,
        tone_player: str | None,
        at_limit: set[str],
    ) -> None:
        usable = [(pid, calibrations[pid]) for pid in members if pid in calibrations]
        usable = [(pid, cal) for pid, cal in usable if cal.enabled and cal.calibrated]
        if len(usable) < 2:
            self._targets.pop(key, None)
            return

        levels = {pid: self._observed_volume(view, pid) for pid, _ in usable}

        # A level the user just asked for OVERRIDES what the view reports for that endpoint. The
        # view is a poll behind and the player's echo is a round trip behind that, so on the fast
        # path the observed number is simply out of date — trusting it would either do nothing or,
        # worse, re-derive the group from the pre-move level.
        intent = self._user_intent
        intent_ref: tuple[str, EndpointCalibration] | None = None
        if intent is not None and intent[0] in levels:
            levels[intent[0]] = intent[1]
            intent_ref = next(((pid, cal) for pid, cal in usable if pid == intent[0]), None)
            self._user_intent = None

        # An echo that matches what we asked for clears the endpoint back to trusted.
        for pid, _ in usable:
            observed, commanded = levels[pid], self._commanded.get(pid)
            if observed is not None and commanded is not None and abs(observed - commanded) <= USER_INTENT_EPSILON:
                self._unconfirmed.discard(pid)

        # Whoever the view disagrees with us about is the endpoint a human just moved.
        #
        # If SEVERAL diverge in one window the choice is genuinely ambiguous: a 2 s poll cannot
        # order two slider drags, so "most recent" is not knowable here. The largest divergence
        # wins — the biggest deliberate change — which is at least deterministic and independent of
        # dict order. It is not always the user's last act: drag A far and then B slightly, inside
        # one tick, and A sets the target. The GUI's 5 s optimistic hold makes that visible as B
        # snapping back. Ranking by recency would need the volume route to timestamp intent, which
        # is a real option if this ever bites in practice.
        moved = [
            (pid, cal)
            for pid, cal in usable
            if pid != tone_player
            and pid not in self._unconfirmed
            and levels[pid] is not None
            and abs(levels[pid] - self._commanded.get(pid, levels[pid])) > USER_INTENT_EPSILON
        ]
        # Stated intent beats inferred intent: we were told, rather than having to guess.
        reference = intent_ref or (
            max(moved, key=lambda item: abs(levels[item[0]] - self._commanded.get(item[0], levels[item[0]])))
            if moved
            else None
        )

        target = self._targets.get(key)
        if reference is not None:
            ref_id, ref_cal = reference
            ref_volume = levels[ref_id]
            if ref_volume is not None and ref_volume < MIN_REFERENCE_VOLUME:
                # One endpoint muted must mute that endpoint, not the house.
                self._commanded[ref_id] = ref_volume
                return
            target = target_db_for(ref_cal, ref_volume)
            if target is None:
                return
            self._targets[key] = target
            self._commanded[ref_id] = ref_volume
            self._unconfirmed.discard(ref_id)  # agreeing with an observation is not commanding it
            logger.info(
                "loudness: %s set the target to %.1f dB (%d%%); re-levelling %d endpoint(s)",
                ref_id,
                target,
                ref_volume,
                len(usable) - 1,
            )
        elif target is None:
            # No target yet and nobody has moved: adopt the current state as the baseline rather
            # than picking a reference and moving speakers the user never asked us to move.
            seed = next(((pid, cal) for pid, cal in usable if levels[pid] is not None), None)
            if seed is None:
                return
            target = target_db_for(seed[1], levels[seed[0]])
            if target is None:
                return
            self._targets[key] = target
            for pid, _ in usable:
                if levels[pid] is not None:
                    self._commanded[pid] = levels[pid]
            return

        for pid, cal in usable:
            if reference is not None and pid == reference[0]:
                continue
            if pid == tone_player:
                continue
            result = match_volume(cal, target)
            if result is None:
                continue
            if result.at_limit:
                at_limit.add(pid)
            current = levels[pid]
            # "Already there" may only be judged from the OBSERVED level when that level is
            # trustworthy. For an endpoint still awaiting its echo the report is stale — or, for one
            # that never echoes, frozen at its connect-time value — and a frozen 100 would match a
            # target of 100 and skip a command the speaker (actually sitting at 60) needs.
            already_there = pid not in self._unconfirmed and current == result.volume
            # "Already asked" is always safe, and it is what stops an endpoint whose echo never
            # arrives being re-commanded every 2 s for as long as the group exists. A target change
            # still re-sends, because the wanted value moves with it.
            already_asked = self._commanded.get(pid) == result.volume
            if already_there or already_asked:
                self._commanded[pid] = result.volume
                continue
            try:
                await self._router.set_volume(pid, result.volume, False)
            except Exception as exc:  # noqa: BLE001 - one unreachable endpoint must not stop the rest
                logger.warning("loudness: could not set %s to %d%%: %s", pid, result.volume, exc)
                continue
            self._commanded[pid] = result.volume
            self._unconfirmed.add(pid)

    @staticmethod
    def _observed_volume(view: MeshView, player_id: str) -> int | None:
        found = view.find_player(player_id)
        return int(found[1].volume) if found is not None else None
