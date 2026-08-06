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

## 2. Fresh-stamp metadata progress + compute the join snapshot from the live position

**Conformance impact: LOW (correct today) — this is a complexity/robustness ask.**

The Sendspin metadata model is built for **sparse** progress updates: emit `(track_progress,
track_duration, playback_speed)` with a `timestamp`, and every client extrapolates the live position
from there. A source like shairport-sync that only emits progress every few seconds *should* need
nothing more.

Two library behaviours break that assumption:

1. `MetadataGroupRole.update()` / `set_metadata()` **inherit the previous metadata's
   `timestamp_us`** (`replace()` copies it; `set_metadata` only stamps a fresh timestamp when it is
   `None`). A re-emit that doesn't explicitly clear the timestamp leaves clients extrapolating from
   an ever-older anchor — the position runs past the end and clamps to 100%.
2. The per-client **join snapshot** a late-joining client receives carries that same stale anchor, so
   a client that connects mid-track reads a clamped 100% until the next source update.

**Current workaround** (`backend/scripts/sources/airplay_metadata.py`):
- every `_emit_progress` passes `timestamp_us=None` to force a fresh stamp; **and**
- a **1 Hz `_progress_ticker`** re-emits the extrapolated position while playing, purely to keep the
  server's anchor (and thus the join snapshot) from going stale.

The ticker is compensating for the library, not for the wire protocol. If the library did the right
thing, the AirPlay reader could just forward shairport's sparse `prgr` frames verbatim.

**Ask:**
- `set_metadata`/`update` should **re-stamp `timestamp_us` by default** whenever a progress field
  changes (or take an explicit `restamp: bool`); and
- the server's join snapshot should be built from the **live extrapolated** position at join time —
  the group role already has `_get_current_track_progress()`; call it when composing the snapshot.

With both, `_progress_ticker` and the `timestamp_us=None` dance can be deleted.

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

## Revisit checklist (per pin bump)

Run `_resources/spike/mesh_smoke.py` first (per `CLAUDE.md`), then check each ask above against the
new release's changelog/API. For any that's resolved: remove the workaround, update
`docs/SPEC-CONFORMANCE.md`, and delete the entry here.
