# Upstream asks — `aiosendspin`

> Things Plum-Audio currently works **around** because the pinned `aiosendspin` (**6.0.5**) doesn't
> expose the seam cleanly. Each is a real, shipped workaround — not a hypothetical — so this file is
> the checklist to revisit on every pin bump: if a release closes one, delete the workaround and the
> corresponding note in `docs/SPEC-CONFORMANCE.md`.
>
> Library source for reference: <https://github.com/Sendspin/aiosendspin>. Raise these as issues/PRs
> when we next engage upstream. Ordered by conformance impact.

---

## 0. `client/state` never carries the spec's REQUIRED top-level `state` — **a library bug**

**Conformance impact: HIGH. This is a defect in `aiosendspin` itself, not a missing seam, and it
affects every client the library ships — including its own.**

The spec makes `state` a REQUIRED field of the `client/state` payload, at the top level, one of
`synchronized` / `error` / `external_source`. `SendspinClient.send_player_state()`
(`client/client.py`) sets it only inside the nested `player` object — the field the library's own
`models/player.py` annotates:

```python
# DEPRECATED(before-spec-pr-50): Remove once all clients send state at client level.
```

and leaves `ClientStatePayload.state` at its `None` default, which `omit_none = True`
(`models/core.py`) then strips from the JSON entirely. So the emitted message is:

```json
{"type":"client/state","payload":{"player":{"state":"synchronized","volume":42, ...}}}
```

Meanwhile the library's own **server** reads `payload.state` at the top level
(`server/connection.py`) — so it reads `None`, skips the transition, and leaves the client at its
default. **aiosendspin's client and server disagree with each other**, and it is invisible only
because both ends default to `SYNCHRONIZED`. A spec-strict third-party server sees a required field
missing on every state message, including the mandatory one at connect.

**Current workaround** — `sendspin_player.build_client_state_message()` constructs the message
directly and sets **both** fields (top-level for the spec, nested for peers mid-migration), then
sends it via `client._send_message()`. Guarded by `tests/Unit/test_client_state_conformance.py`,
whose last test asserts the upstream bug is still present: **when that test starts failing, the fix
has landed — delete the local builder and go back to `send_player_state()`.**

**Ask:** set `ClientStatePayload.state` in `send_player_state()`. One line, and it makes every
aiosendspin client conformant at once.

---

## 1. Let a client inspect `connection_reason` **before** committing to a dialing server

**Conformance impact: HIGH — this is the one open conformance gap in `docs/SPEC-CONFORMANCE.md`.**

The spec's multi-server arbitration (§ *Establishing a Connection*) has the client **complete the
handshake with the new server first**, then decide which server to keep:

- new server `connection_reason: playback` → switch to it;
- new `discovery` while the existing server is `playback` → keep the existing one;
- both `discovery` → prefer the persisted `server_id` of the last server seen `playing`.

We cannot express "accept both, then decide." `SendspinClient.server_info` (and thus
`connection_reason`) is only populated **after** `attach_websocket`, and `attach_websocket` refuses a
second socket while one is attached. So by the time we can read the new server's reason, we've
already had to accept or reject it blind.

**Current workaround** (`backend/scripts/sendspin_player.py`, `on_connection`): on a second dial we
*always* release the old connection (`goodbye: another_server`) and attach the new one — i.e. we
yield to the newest dialer unconditionally. The `server_id` of the last server that had us playing
IS now persisted (`player_state.json`), so only the deciding half is missing. Plum-to-Plum this is
indistinguishable from conformant because our servers **only ever dial `playback`**. Against a
foreign server running a `discovery` sweep it is wrong: we hand over a *playing* speaker. Observed
live — our unit's boot-time dial took a speaker back off Music Assistant ~1 min after MA claimed it.

A local workaround is *possible* but ugly: attach the incoming socket to a throwaway
`SendspinClient`, read `server/hello`, and only hand it to the real client if arbitration says
switch. Two clients, a hand-off dance, and racy. Not worth shipping over a clean upstream fix.

**Ask:** either
- (preferred) have the library implement the spec's arbitration internally — persist the last
  `playing` `server_id`, compare `connection_reason` on a competing dial, and expose a **policy
  hook** to override the decision; or
- surface the parsed `server/hello` (`server_id`, `name`, `connection_reason`) to
  `ClientListener.on_connection` / a pre-attach callback, so the application can arbitrate before
  committing the single websocket.

**Refs:** `sendspin_player.py` `on_connection`; `sendspin_server.py`
`reclaim_remote_player` docstring; `docs/SPEC-CONFORMANCE.md` § *Connection lifecycle*.

---

## 2. Fresh-stamp metadata progress on re-emit

**Conformance impact: LOW (correct today) — this is a complexity/robustness ask.**

> **Corrected 2026-08-12.** This entry used to carry a second half — "the join snapshot carries the
> stale anchor too" — and an ask to build it from the live position. **That half was never true, not
> even on 6.0.5.** It was written from the wire symptom, not from the library source. See the struck
> point 2 below. Re-verified against both 6.0.5 and 9.1.0 while scoping the bump
> (`docs/AIOSENDSPIN-BUMP-SCOPE.md`); the whole file is a 2-line unrelated diff between them.

The Sendspin metadata model is built for **sparse** progress updates: emit `(track_progress,
track_duration, playback_speed)` with a `timestamp`, and every client extrapolates the live position
from there. A source like shairport-sync that only emits progress every few seconds *should* need
nothing more.

Two library behaviours were recorded here as breaking that assumption. **Only the first is real:**

1. `MetadataGroupRole.update()` / `set_metadata()` **inherit the previous metadata's
   `timestamp_us`** (`replace()` copies it; `set_metadata` only stamps a fresh timestamp when it is
   `None`). A re-emit that doesn't explicitly clear the timestamp leaves clients extrapolating from
   an ever-older anchor — the position runs past the end and clamps to 100%.
2. ~~The per-client **join snapshot** a late-joining client receives carries that same stale anchor,
   so a client that connects mid-track reads a clamped 100% until the next source update.~~
   **FALSE — the library already does this correctly, and did on 6.0.5.**
   `MetadataGroupRole._send_state_to_role` (`server/roles/metadata/group.py:42`, reached from
   `on_member_join`) stamps a fresh `timestamp` and then *overwrites* the snapshot's progress with
   the live value:
   ```python
   metadata_update = self._current_metadata.snapshot_update(timestamp)
   current_progress = self._get_current_track_progress()
   if current_progress is not None and ...track_duration is not None and ...playback_speed is not None:
       metadata_update.progress = Progress(track_progress=current_progress, ...)
   ```
   and `_get_current_track_progress` (`:66`) genuinely extrapolates —
   `elapsed_us = now - _track_progress_timestamp_us`, scaled by `playback_speed`, clamped to
   `track_duration`. A stale anchor therefore still yields a **correct** live position at join.
   (It extrapolates only while `has_active_stream`; with no stream it returns the stored value, which
   is the right behaviour for a paused/stopped join.) When any of the three progress fields is unset,
   `snapshot_update`'s own guard drops `progress` entirely — so no stale anchor reaches the client on
   that path either.

**Current workaround** (`backend/scripts/sources/airplay_metadata.py`):
- every `_emit_progress` passes `timestamp_us=None` to force a fresh stamp; **and**
- a **1 Hz `_progress_ticker`** re-emits the extrapolated position while playing, ~~purely to keep
  the server's anchor (and thus the join snapshot) from going stale~~ — **that justification is
  void** given the correction above. The `timestamp_us=None` stamp alone covers the real defect
  (point 1). **The ticker is now a deletion candidate, not a documented necessity** — but it has not
  been removed, because nobody has checked on hardware whether anything else came to depend on a
  1 Hz metadata cadence (our own GUI extrapolates client-side and should not care; a third-party
  controller that does not extrapolate would). Test before deleting.

The ticker is compensating for the library, not for the wire protocol. If the library did the right
thing, the AirPlay reader could just forward shairport's sparse `prgr` frames verbatim.

**Ask:** `set_metadata`/`update` should **re-stamp `timestamp_us` by default** whenever a progress
field changes (or take an explicit `restamp: bool`). With that, the `timestamp_us=None` dance can be
deleted.

**Status against 9.1.0: UNCHANGED.** `set_metadata` (`:110`) still stamps only when the caller left
it `None`, and `update()` (`:196`) still does `replace(current, **kwargs)` off an already-stamped
`_current_metadata`. No `restamp` parameter was added. Keep the workaround.

**Refs:** `airplay_metadata.py` `_emit_progress` / `_progress_ticker`; aiosendspin
`server/roles/metadata/group.py` `set_metadata` / `_get_current_track_progress`.

---

## 3. A public "hang up on this client"

**Conformance impact: NONE — interop correctness (releasing an adopted foreign speaker).**

To hand an adopted third-party speaker back so **its own** server can reclaim it, we must actually
close the websocket to it. In 6.0.5 none of the public methods do:

- `disconnect_from_client(url)` only cancels **our** server-initiated *dial task* for that URL;
- `remove_client(id)` is registry-only;
- `client.detach_connection(reason)` sets internal state.

The live socket stays `ESTABLISHED` — verified on a real speaker: without closing it, Music
Assistant could **not** take its speaker back. Only `SendspinConnection.disconnect()` closes the
socket, and it's reachable only via the **private** `client._connection`.

**Current workaround** (`backend/scripts/sendspin_server.py`, `release_foreign_client`):
```python
conn = getattr(client, "_connection", None)
if conn is not None:
    await conn.disconnect(retry_connection=False)
```

**Ask:** a public server method, e.g.
`await server.disconnect_client(client_id, reason=GoodbyeReason.USER_REQUEST)`, that emits the
goodbye and closes the live socket in one call.

**Refs:** `sendspin_server.py` `release_foreign_client`.

---

## 4. `disconnect_from_client()` does not stop the dialer — **a library bug**

**Conformance impact: NONE — but it is the worst interop bug found so far.**

Found 2026-08-10 bringing up two third-party speakers (an Esparagus HiFi board, a FutureProof Homes
Satellite1) on unit-7204. Routing a source at one played for a few seconds and dropped back to idle,
every time, and retrying made it worse.

`disconnect_from_client(url)` cancels the server-initiated dial task and returns. The task does not
die. `SendspinConnection._handle_client` awaits the message loop as a **separate task**, and
`_run_message_loop` catches `asyncio.CancelledError` and returns normally — so `await
self._message_loop_task` consumes the cancel. The dialer sees a clean session end, backs off ~1 s
and **reconnects**; a session that lasted ≥ `STABLE_SERVER_INITIATED_SESSION_S` (10 s) resets the
backoff, so against a real speaker it never reaches the ceiling that would end it. Meanwhile the
caller's next `connect_to_client(url)` has already installed its own task, and the doomed task's
`finally` then pops `_connection_tasks[url]` — **the new task's entry** — so the next call does not
hit the "already dialling" guard and opens a third dialer, and so on.

Measured against 6.0.5 with a fake speaker counting sockets, driving the disconnect+reconnect pair
six times 3 s apart:

```
after adopt #1: speaker holds 1 websocket(s), 1 dial task(s) alive, registry knows 1
after adopt #2: speaker holds 2 websocket(s), 2 dial task(s) alive, registry knows 1
...
after adopt #6: speaker holds 6 websocket(s), 6 dial task(s) alive, registry knows 1
```

A Sendspin client holds exactly ONE websocket, so those dialers fight over it: audio starts, plays
a few seconds, and dies with `close_code=1006`, repeatedly — which is exactly what
`/config/logs/sendspin_server.log` on unit-7204 shows across the 19:05–19:39 test window.

**Current workaround** (`sendspin_server.py`, `_stop_dialing`): cancel in a loop until the task is
genuinely `done()`. The swallow only happens *inside* the message loop; a cancel landing during
connect or during the backoff sleep propagates normally, so re-cancelling terminates. Everything
that tears a dial down (`adopt_foreign_client`'s stale path, `release_foreign_client`) goes through
it, and `adopt_foreign_client` no longer redials at all when it already holds a live connection to
that URL.

**Ask:** make `disconnect_from_client(url)` actually stop the dial — either have
`_run_message_loop` re-raise `CancelledError`, or have `_handle_client_connection` track its own
"cancelled" flag and skip the retry. Awaitable would be better still
(`await server.disconnect_from_client(url)`), so a caller can redial safely. Related: the `finally`
block should only clear `_connection_tasks[url]` when the entry is still *its own* task.

**Refs:** `sendspin_server.py` `_stop_dialing` / `adopt_foreign_client` / `release_foreign_client`;
guards in `tests/Unit/test_sendspin_server.py`; `aiosendspin/server/server.py:593`
`_handle_client_connection`, `aiosendspin/server/connection.py:618` `_run_message_loop`.

---

## 5. `_schedule_cleanup` orphans its previous timer, which later evicts a LIVE client — **a library bug**

**Conformance impact: NONE — but it silently unroutes a playing speaker.**

Found 2026-08-10 on unit-7204 with DEBUG on, chasing "reroute a speaker, it plays for a few seconds,
then drops back to idle".

`SendspinClient._schedule_cleanup` assigns `self._cleanup_handle = ...` **without cancelling
whatever handle was already there**. Schedule twice and the first timer is orphaned: nothing
references it, so `attach_connection`'s "Cancel pending cleanup if client reconnected before cleanup
fired" can never reach it, and it fires regardless. `_do_cleanup`'s only other guard is
`if self._connected` on the object that owns the timer — which does not protect a connection that
came back on a different object — and it then calls `remove_client(self._client_id)`, evicting
**whichever client currently holds that id**.

Two schedules is the normal shape of a release: the connection teardown carries no goodbye reason
(→ 30 s DELAYED cleanup) and the `USER_REQUEST` goodbye ~250 ms later adds an IMMEDIATE one on top.

```
20:53:32,209  Scheduling delayed cleanup in 30s (reason: None)
20:53:32,507  Received client/hello           <- rerouted, reconnected
20:53:32,571  attached player 98:A3:16:D0:9E:E8   <- playing
20:54:02,210  Cleaning up client from registry
20:54:02,211  removing 98:A3:16:D0:9E:E8 from group   <- evicted mid-playback
```

Exactly 30 s after the **unroute**, not the reroute — which is why the symptom reads as a random
15–60 s dropout scaling with how quickly the speaker was re-routed. Over that session the library
logged 3 cancelled cleanups and 13 executed ones, against two ESP32 speakers and a browser client.

**Current workaround** (`sendspin_server.py`, `_cancel_pending_cleanup`): cancel the pending handle
ourselves at both points that would otherwise leave one armed — in `attach_player` (a routed player
must never have an eviction pending, which covers adopt, route and reclaim) and in
`release_foreign_client` before the goodbye adds the second schedule. Best-effort: it touches a
private attribute and must not break routing on a version that no longer has it.

**Ask:** cancel the existing handle at the top of `_schedule_cleanup`, and make `_do_cleanup` verify
the registry still maps `client_id` to `self` before removing it. Either alone fixes this.

**Refs:** `sendspin_server.py` `_cancel_pending_cleanup`; guards in `tests/Unit/test_sendspin_server.py`;
`aiosendspin/server/client.py:555` `_schedule_cleanup`, `:584` `_do_cleanup`, `:388` `attach_connection`.

---

## 6. Encryption is opt-in-by-omission today — will become opt-out on the next big pin bump

**Conformance impact: NONE today — a forward-looking trap, not a current bug.**

Discovered 2026-08-10 researching third-party ESP32 (ESPHome/Sendspin) client hardware. Our pinned
`SendspinServer.__init__` (6.0.5) has no psk/pairing/noise/encryption parameter at all — the entire
`aiosendspin/noise/` package (Noise Protocol `KKpsk2` handshake, PSK pairing) doesn't exist until
upstream **8.0.0** (2026-08-07) and **9.0.0** (2026-08-10). The current spec page now describes
encryption as mandatory for standard-discovery connections, which is the 8.0.0+ shape, not what we
run. Post-8.0.0 `SendspinServer` carries an `allow_unencrypted: bool = False` escape hatch.

**Why this matters for a pin bump, not just new clients:** every client we currently interoperate
with — our own units, and any third-party ESP32 board running `sendspin-cpp` (ESPHome's Sendspin
client, no noise/psk code as of the version checked) — is cleartext-only. If `aiosendspin` is ever
bumped past 8.0.0 without also passing `allow_unencrypted=True` in `sendspin_server.py`, every
existing client fails the handshake silently the day the pin moves, with nothing in the logs
pointing at encryption as the cause.

**Ask:** none — this is us, not upstream. Just a checklist item: when bumping past 8.0.0, either
pass `allow_unencrypted=True` deliberately, or scope out what pairing/PSK distribution to our own
players and any adopted third-party clients would require before flipping it off.

**VERIFIED against 9.1.0, 2026-08-12 — the entry was right but understates it.** `allow_unencrypted:
bool = False` is real (`server/server.py:155`). It is **necessary but not sufficient**, and it is not
the expensive part:

- `SendspinServer(server_id: str)` became `identity: Identity` with `self._id = identity.peer_id`
  (an X25519 pubkey), plus a **required** `pairing_store`. Client ids stop being ours to choose,
  which breaks the `follow.py` `server_id`↔`unit_id` join and the GUI's `ctrl:<source_id>:` hint.
- With the flag on, our **own** player still connects, handshakes, joins the group at the right
  volume — and gets **zero roles**, because an unpaired sentinel-PSK client fails `_playback_capable`
  unless `unpaired_access_enabled` **and** `trust_unpaired(client_id)` are both set. Silent on both
  sides. "Does it connect?" is not a valid test.
- `sendspin-cpp` has no encryption as of v0.7.2, so the flag is **permanent, not transitional**, for
  as long as we want ESP32 interop.

Full port scope, breakage list and the recommendation to hold the pin:
**`docs/AIOSENDSPIN-BUMP-SCOPE.md`**.

**Refs:** `_resources/Research/Esparagus/Sendspin-Conversion-Plan.md` (where this was found);
`sendspin_server.py:347`.

---

## Revisit checklist (per pin bump)

Run `_resources/spike/mesh_smoke.py` first (per `CLAUDE.md`), then check each ask above against the
new release's changelog/API. For any that's resolved: remove the workaround, update
`docs/SPEC-CONFORMANCE.md`, and delete the entry here.

**Note that `mesh_smoke.py` itself does not survive the 9.x constructor changes** — it has 5 broken
constructor sites, so the mandated gate must be ported before it can gate anything.

**Status as of 2026-08-12** (all six read against 9.1.0 — see `docs/AIOSENDSPIN-BUMP-SCOPE.md`):

| § | 9.1.0 |
|---|---|
| 0 | **Fixed**, by replacing `state` with `available: bool` — our workaround becomes the non-conformance |
| 1 | **Fixed** — real arbitration in `attach_websocket`; closes `CLAUDE.md` Open #6 |
| 2 | Unchanged (and half the original ask was never true — see the correction above) |
| 3 | Unchanged |
| 4 | **Partially fixed** — the dial task now dies on cancel; still sync, still clobbers `_connection_tasks[url]` |
| 5 | Unchanged, and **worse**: `CLIENT_CLEANUP_DELAY` 30 s → 180 s widens the orphaned-eviction window 6× |
| 6 | Confirmed and understated — see the block in that section |
