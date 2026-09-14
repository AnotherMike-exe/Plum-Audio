# Routing model — proposed

> **Status: rule 1 implemented 2026-08-12** (`SourceFeeder._go_idle`, `sendspin_server.py`) —
> unit-tested, hardware verification pending (see this file's own staging step 3). Implemented
> directly against the existing `detach_player`/`group.remove_client` primitives, **not** through
> the `attach`/`detach(mode=hold|release)` vocabulary in rules 3-4 below, which remain an
> unimplemented recommendation — the minimal version was the deliberate choice, not a first slice of
> the full rollout. Rules 2 and 4 are otherwise unchanged from when this was written 2026-08-10,
> after an evening of connection-lifecycle bugs (see `UPSTREAM-AIOSENDSPIN.md` §4/§5 and commit
> `2d3f548`) exposed that the routing API has two overlapping vocabularies and one undefined state.
> The rule in *"None" is a true none* was Michael's call, made explicitly; the rest is still just a
> recommendation.

## Why now

Every bug found on 2026-08-10 lived in **connection lifecycle** — a dial teardown that did not tear
down, a registry eviction armed by a teardown, a stream handover on reconnect. Not one lived in
**group membership**: `add_client`/`remove_client` behaved correctly throughout, including under
deliberate stress. That asymmetry is the design input.

> **Prefer membership changes over connection changes, and make the expensive one explicit.**

The second input is that the ours/foreign split has already leaked. `unit-7204`'s logs show
`adopted foreign speaker player-7204 (ws://192.168.7.204:8928/sendspin)` at 19:05, 20:06 and 21:21 —
the GUI routing the unit's **own** player through the foreign-speaker path. The backend maintains two
code paths for what the GUI already treats as one action.

## The rules

### 1. "None" is a true none

An endpoint that is set to none, **or whose source dies**, holds no attachment and never
auto-resumes, regardless of what it last played. Exactly two things re-attach an endpoint, both
explicit settings:

| mechanism | setting | what it does |
|---|---|---|
| local activity | `autoSwitch.localActivity` (default **on**) | this unit's own player, while idle, picks up one of this unit's **own** sources when it becomes active. Rising-edge only. `follow.py:159` |
| follow | `autoSwitch` follow / slaved-to | a follower tracks its master's target over time, including to none. `mesh/follow.py` |

Nothing else. A speaker you joined by hand to someone else's stream is torn down when that stream
dies and stays down.

**The rule is uniform** — a unit's own player is an endpoint like any other and goes to true none
with everything else. It does not need an exception, because `localActivity` already expresses the
"just AirPlay to the kitchen speaker and it plays" behaviour as a *setting* rather than as an
implicit attachment that survives idle. Turning `localActivity` off is how a user says "this unit
ingests but does not render its own source", which today can only be expressed as
absence-of-attachment and therefore cannot survive a restart.

**Was a change from today; now built.** `SourceFeeder._go_idle` used to announce
`playback_state=stopped` and deliberately leave the group intact — `CLAUDE.md` used to say
*"Groups/anchors persist, so routing survives"*. Measured on `unit-7204` 2026-08-10 21:36–21:38:
source died, both endpoints stayed attached, sender returned 2 minutes later, audio resumed with no
re-route. **Implemented 2026-08-12**: `_go_idle` now detaches every player-role client via
`group.remove_client()` (the same primitive `detach_player` already used for a manual "set to
none"), and only `localActivity` brings the local one back automatically. Unit-tested
(`tests/Unit/test_sendspin_server.py`, the `_go_idle` section); hardware verification of the
cross-unit "stays down" case and the local rising-edge re-attach is the remaining open step (see
Staging below).

### 2. Pauses and disconnects stay distinct — as built

A pause is not a session end. AirPlay reaches idle via the 300 s `PLUM_SOURCE_IDLE_TIMEOUT` (shairport
holds the FIFO open, so EOF never arrives on that path), and that cooldown is deliberate. The MPRIS
`sender gone` signal is **not** to be wired into `_go_idle`: acting on it would make a five-minute
coffee break tear down the whole multiroom group. Confirmed as designed 2026-08-10.

Consequence to keep in mind, not a defect: the source reads `active=true` for up to five minutes
after a sender walks away, and the GUI's idle-source filtering lags by the same amount.

### 3. One vocabulary, keyed on the listener URL

Replace `route`/`unroute`/`adopt`/`release` with one pair:

```
attach(source_id, endpoint)
detach(source_id, endpoint, mode = hold | release)
```

`attach` decides internally — already connected → group it; ours but disconnected → reclaim;
foreign → dial. That logic exists and works today; it is only split across `router.route_player` and
`engine.adopt_foreign_client`. This is a seam change, not new behaviour.

`detach`'s `mode` is the real distinction that `unroute` vs `release` was groping at, and it is
**orthogonal to ownership**:

- `hold` — leave the group, keep the websocket. The endpoint is at true none; re-routing later is
  instant and involves no teardown.
- `release` — leave the group and hand the device back to the network, so its own server can claim
  it (Music Assistant, a Home Assistant server, whatever advertised it).

**Default to `hold`.** Release is the expensive, risky operation — teardown, redial, then re-attach
into a live stream, which is precisely the sequence every 2026-08-10 bug lived in. Today the GUI
issues a *release* for foreign speakers on "set to none", i.e. the costly path on the most routine
action a user performs. Neither mode auto-resumes anything; holding a socket is invisible to the
user and does not weaken rule 1.

**Identity is the listener URL, everywhere.** `CLAUDE.md` already says the URL is the only identifier
both views share — mDNS names by instance, the handshake by MAC — yet `unroute` and `release` still
take `player_id`. That inconsistency forced `_resources/spike/unwedge_probe.py` to look up a
handshake id after adopting by mDNS name, and it is a standing bug generator.

### 4. Follow drives the same primitives

Follow is **not** a parallel implementation and should not be described as one: it is a policy layer
that tracks a master's target *over time*, which nothing else does. What unification buys is that its
effects become `attach` / `detach(hold)` rather than a second routing vocabulary.

## What this forces a decision on

**`Open #13` — a follower stops following when its leader switches source.** Today `follow.tick()`
cannot distinguish "went idle because the source stopped" from "the user moved me": both surface as
`current_target = None`, and the `_overridden` guard treats them alike. Rule 1 may dissolve this:
if nothing auto-resumes, and followers re-follow by definition, then `_overridden` only has to
survive long enough to express "the user moved this follower off its master", and the idle case stops
being ambiguous because idle is no longer a state anything recovers from on its own. That needs
checking against `follow.py` properly before it is claimed as a free win.

## Staging

**Taken 2026-08-12: a minimal path, not this list.** Steps 1 and 2 below were deliberately NOT done
as originally sequenced — `_go_idle` was changed to detach directly against the existing
`detach_player`/`group.remove_client` primitives, skipping the `attach`/`detach` vocabulary
introduction entirely (see the status header). That was a scope decision, not a discovery that the
vocabulary work is unnecessary — it's still a reasonable recommendation, just not decided on. Steps
3-5 below are unchanged and still open if the vocabulary unification is ever picked up:

1. ~~Add `attach`/`detach` as the real implementation; make the four existing routes thin
   aliases.~~ — not done; skipped in favour of the direct route.
2. ~~Change `_go_idle` to detach, and rewrite the idle-contract rule in `CLAUDE.md`~~ — **done
   2026-08-12**, directly, without step 1.
3. Verify `localActivity` covers the local-player case end-to-end on hardware, including its
   rising-edge behaviour after a true-none. **Still open** — reading the code says it already
   handles this (`follow.py:287`, `router.py:126-140`), but it hasn't been run on the rig against
   this specific transition yet. That code-read is also where this was missed: it only considered
   a *released* player, and on 2026-09-07 `.7.200` showed the real steady state is a player parked
   by Music Assistant, which read as busy and disarmed the whole branch. Fixed in
   `follow._player_status`; the rig check is still owed. HARD-WON-LESSONS.
4. Migrate the GUI to the new pair; default "set to none" to `detach(hold)`. Blocked on 1, which
   wasn't done.
5. Retire the aliases. Blocked on 1 and 4.

Each step is rig-testable on its own. The original warning — do not do 1 and 2 in one deploy, since 2
is a behaviour change users will feel and wants to be isolatable if it turns out to be wrong — still
applied even without step 1 existing: 2 shipped alone.

## Related

- `CLAUDE.md` — the connection-lifecycle rules this proposal is built on
- `UPSTREAM-AIOSENDSPIN.md` §4, §5 — the two library bugs behind the "prefer membership" principle
- `_resources/Research/ISSUE-adopt-foreign-client-group-disruption.md` — the full 2026-08-10 investigation
