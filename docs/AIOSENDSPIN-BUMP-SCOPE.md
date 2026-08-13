# Scoping the `aiosendspin` bump — 6.0.5 → 9.1.0

> **Status: HOLD the pin.** Scoped 2026-08-12 by source-reading both trees. This is a protocol
> migration, not a version edit: ~30 call sites break, three of them architectural, and the blocking
> problem has no clean answer while `sendspin-cpp` lacks encryption.
>
> This document is the evidence behind that call. Re-read it before bumping, and re-check
> `docs/UPSTREAM-AIOSENDSPIN.md` alongside it — that file tracks the workarounds, this one tracks the
> port. Nothing here has been tested on hardware; every claim is a source read of the two trees, and
> the items under *Needs a rig test* are explicitly not settled.

## Where we are

| Stream | Pinned | Latest | Gap |
|---|---|---|---|
| `aiosendspin` (backend) | **6.0.5** — 2026-06-10 | **9.1.0** — 2026-08-11 | 6.1.0, 6.1.1, 7.0.0, 8.0.0, 9.0.0, 9.1.0 — **3 majors** |
| `@sendspin/sendspin-js` (browser player) | `^3.2.1` — 2026-07-17 | **5.0.0** — 2026-08-11 | 4.0.0, 5.0.0 — **2 majors** |
| `sendspin-cpp` (the ESP32 clients, via ESPHome) | n/a — not ours | v0.7.2 — 2026-08-12 | **no release adds encryption** |

The pin was never a rejection of a newer version: 6.0.5 was current when the repo was scaffolded
(`dc9b696`, 2026-07-08) and has never been bumped. `git log -S"aiosendspin==" -- backend/requirements.txt`
returns exactly one commit.

The JS stream matters because it is the same migration: 4.0.0 is *"Add Noise encryption and PSK/PIN
pairing"*, 5.0.0 is *"Update pairing to the latest specification"*. `frontend/services/sendspinControllerClient.ts`
is hand-rolled raw WebSocket with no library to bump — it tracks the wire format by inspection, so it
has to be migrated by hand or it stops connecting.

## The three that actually cost

### 1. Identity becomes a Noise keypair

```python
# 9.1.0 server/server.py:149
def __init__(self, loop, identity: Identity, server_name, client_session=None, *,
             pairing_store: ServerPairingStore, allow_unencrypted: bool = False,
             allow_noncompliant_clients: bool = True, min_pin_length: int = ..., clock=None):
    self._id = identity.peer_id       # base64url X25519 public key
```

`server_id: str` is gone, `pairing_store` is a required keyword, and the same applies to
`SendspinClient`. The 9.1.0 client hello omits `client_id` entirely, so `PLUM_UNIT_ID` /
`PLUM_PLAYER_ID` no longer determine any Sendspin id. Three knock-ons, in descending nastiness:

- **`follow.py:289` breaks silently.** `view.unit(lp.get("server_id"))` joins a Sendspin `server_id`
  against the mesh unit table. That join only ever worked because 6.0.5 let us pass
  `server_id=self.unit_id`. Under 9.1.0 every unit's own player reads as attached to a *foreign*
  server — follow stops working, and `sendspinDataService.ts:226` flags local peers as
  `claimedByOutsider`.
- **The `ctrl:<source_id>:<nonce>` convention dies.** The GUI names its target source through the
  client id (`sendspin_server._requested_source`, `sendspinControllerClient.ts:333`). A client id is
  now a 43-char key digest carrying no hint. This is the code touched by Open #20 (2026-08-12) and
  needs a different channel entirely — this is the single largest design hole in the port.
- Everything keyed on `player_id` (`register_client_url`, `reclaim_client_for_playback`,
  `attach_player`, GUI routing) migrates to the pubkey. 9.1.0's new
  `SendspinServer.get_client_id_for_url(url)` helps, and is consistent with the existing rule that
  the **listener URL** is the real identifier.

**Work implied:** persist an `Identity` per unit and per player under `/config`
(`Identity.generate()` / `from_private_bytes`, `private_b64u` to store); adopt
`FileServerPairingStore` / `FileClientPairingStore`; keep `unit_id` as the mesh key and explicitly
sever the `server.id == unit_id` assumption.

### 2. Encryption is on by default, and the client has no legacy mode

```python
# 9.1.0 server/connection.py:827 — _establish_transport
if msg_type == "client/init": ... run_handshake_server(...)
if msg_type == "client/hello" and self._server.allow_unencrypted:
    self._logger.warning("Accepting unencrypted legacy connection (transition mode)")
    ...
raise HandshakeAbortedError(f"unexpected first frame type {msg_type!r}")
```

`allow_unencrypted=True` is **mandatory** — without it every existing client is rejected: our own
player, the ESP32 speakers, Music Assistant, and the GUI controller. The rejection is at least
diagnosable server-side (`unexpected first frame type 'client/hello'`); from the client it is just a
dropped socket.

For *third-party cleartext* clients the legacy path is otherwise intact — they get the full
negotiated role set minus `source@v1` (the only role marked `requires_pairing=True`). Two constraints
worth knowing: `connection_reason` is clamped to `DISCOVERY` for anything outside
`{DISCOVERY, PLAYBACK}` (benign — our routing primitive is PLAYBACK), and `_admit_legacy_client_id`
**rejects** an unencrypted client claiming an id that already has a pairing record. That last one is
a foot-gun the moment anything pairs, including a rollback after a partial migration.

The blocking half: the aiosendspin **client** has no counterpart flag — `client/connection.py` only
ever calls `run_handshake_client`. **A 9.1.0 player cannot talk to a 6.0.5 server.** Mixed-version
mesh is broken in both directions, so all four units must cut over atomically, which is a rollout
mode `deploy.sh all` has never had to provide.

### 3. Our own endpoints connect and play nothing, silently

An unpaired client handshakes under the published `SENTINEL_PSK` (`noise/constants.py:11`, a public
constant that authenticates nothing). The server then gates roles:

```python
# 9.1.0 server/connection.py:1091 — _playback_capable
if self._noise_psk.category is PskCategory.LONG_TERM:      return True
if self._noise_psk.category is PskCategory.SENTINEL:
    return self._client_info.unpaired_access.enabled and self._trusted_unpaired
return False
```
```python
# :1125 — _roles_to_activate
if not self._playback_capable:  return []
```

Both halves of the sentinel condition default **off** (`unpaired_access_enabled: bool = False`,
`noise/trust_store.py:191`; `trusted_unpaired` is operator-populated). So under 9.1.0 defaults our
own player connects, handshakes, appears in the group at the right volume, receives **zero roles and
zero activities**, and renders nothing — with no error at either end.

That is verbatim the failure signature `CLAUDE.md` already documents for the stream-membership bug:
*"it sits in the group, in the GUI, at the right volume, and silent, with nothing in any log."*
**Anyone who bumps the pin and tests "does it connect?" will conclude it worked.** Write that into
the test plan before touching the pin.

**MEASURED 2026-08-12** against a real 9.1.0 server and client (`_resources/spike/mesh_smoke.py`
steps 6-7, run locally — no hardware needed). This was a source read when first written; it is now a
truth table:

| client `unpaired_access_enabled` | server `trust_unpaired()` | `negotiated_role_ids` | `active_role_ids` |
|---|---|---|---|
| no | no | `player@v1` | — |
| **yes** | no | `player@v1` | — |
| no | **yes** | `player@v1` | — |
| **yes** | **yes** | `player@v1` | **`player@v1`** |

Two things this pins down that the code read did not. **The role is always negotiated**, in every
combination — so a failed endpoint is indistinguishable from a healthy one by connection state,
client list, or negotiated roles. And **both opt-ins are required**; either alone is a silent dud, so
this is not a single flag anyone can forget once. The observable signature is `negotiated_role_ids`
diverging from `active_role_ids`, and `client.activities` is **not** it — that reports what the
server declares in `server/activate`, and is legitimately empty on a healthy client-dialled
connection. Anything we build to health-check endpoints must read `active_role_ids` server-side.

Fix per endpoint, including our own: either pair it (long-term PSK), or set
`unpaired_access_enabled=True` on the client config **and** call `await server.trust_unpaired(client_id)`.
The second is the pragmatic route since we own both ends — but it requires the persisted `Identity`
from break #1, because a regenerated identity is a brand-new untrusted device.

**Trust store persistence:** `FileServerPairingStore` / `FileClientPairingStore`, caller-chosen path,
no default. Holds raw PSKs, staged pairing PSKs and the trusted-unpaired list; the client side also
holds `last_playback_server_id`. Written `0o600` via atomic replace (the 7.0.0 permissions fix).
**Must persist** — losing it un-pairs every device on the unit — so it belongs on `/data`. Note it
ignores `UMASK=002` by construction, so the file is root-only in the container. The X25519 private
keys are *not* in the trust store; `Identity` persistence is entirely ours.

## Breakage list

### Hard — architectural

| Site | What |
|---|---|
| `sendspin_server.py:440` | `SendspinServer(server_id=…)` → `identity=` + required `pairing_store=` |
| `sendspin_player.py:437` | `SendspinClient(client_id=…)` → `identity=` + required `pairing_store=` |
| `follow.py:289` | `server_id`↔`unit_id` join no longer holds — **fails silently** |
| `sendspin_server._requested_source`, `sendspinControllerClient.ts:333` | `ctrl:<source_id>:` hint has no carrier |
| `sendspin_player.py:51,103,414,844,971,1000-1008` | `ClientStateType` deleted — **ImportError, player never starts** |

### Hard — mechanical

| Site | Symbol | Change |
|---|---|---|
| `sendspin_player.py:1030` | `client._send_message` | moved to `SendspinConnection` |
| `sendspin_player.py:819` | `client.send_goodbye()` | moved to `SendspinConnection`; use `disconnect(reason=…)` |
| `sendspin_player.py:811-826` | `attach_websocket` | no longer raises on double-attach — **delete the RuntimeError-retry dance** |
| `sendspin_player.py:654` | `ServerInfo.connection_reason` | `ServerInfo` is now `client/models.py` with only `server_id`, `name` — `AttributeError` |
| `sendspin_server.py:382,596,789,819,970,997` | `client.negotiated_roles` | renamed `negotiated_role_ids` (6 sites + fakes) |
| `server/client.py` | `attach_connection` | gained a required `negotiated_roles=` kwarg |
| `_resources/spike/*.py` | constructors | 5 sites — **`mesh_smoke.py` is the mandated pre-bump gate, so it must be fixed first** |
| `backend/requirements.txt` | deps | +`cryptography>=42`, +`noiseprotocol>=0.3.1`, +`cpace>=0.1.0` |
| `tests/Unit/` | `test_client_state_conformance.py`, `test_player_health.py` (+13 assertions), `test_sendspin_server.py:96`, `test_true_none_reattach.py:66` | fakes and canaries |

### Verified unchanged

`SendspinGroup`'s entire public API is byte-identical (`add_client`, `remove_client`, `stop`,
`start_stream`, `stop_stream`, `group_role`, `clients`, `has_active_stream`, `group_id`,
`group_name`, `add_event_listener`). `PushStream`'s public API likewise (`prepare_audio`,
`commit_audio`, `set_live_source`, `sleep_to_limit_buffer`). `ClientListener` diff is empty.
`MetadataGroupRole` / `ArtworkGroupRole` / `VisualizerGroupRole` identical, so all three source
handlers are safe. `AudioFormat` moved to `aiosendspin/audio/format.py` but `server/audio.py`
re-exports it — our import path still works. Also unchanged: `has_role_family`, `PlayerV1Role`,
`StreamStoppedError`, `ClientAddedEvent`, `ClientUpdatedEvent`, `RepeatMode`,
`start_server(advertise_addresses=[], discover_clients=False)`, `connect_to_client`,
`reclaim_client_for_playback`, `register_client_url`, and the private reaches
`client._connection` / `server._connection_tasks` / `_cleanup_handle`.

## Workaround status — `docs/UPSTREAM-AIOSENDSPIN.md` §0–§6

| § | Verdict on 9.1.0 | Action |
|---|---|---|
| **0** `client/state` top-level `state` | **Fixed — by deleting the field.** `state` → `available: bool`; `ClientStateType` gone tree-wide; the library's client now sets the top-level field itself, and the server *flags* the legacy shape (`legacy_state_used` → `_flag_noncompliance`) | Delete `build_client_state_message`, the four private reaches, and the canary test. **Our workaround becomes the non-conformance** |
| **1** multi-server arbitration | **Fixed.** `attach_websocket` brings the socket up provisionally, handshakes, then arbitrates by a new `Activity` rank, tie-broken on a persisted `last_playback_server_id` | Delete the yield-to-newest-dialer logic; `player_state.py`'s persistence becomes redundant; **closes Open #6** |
| **2** metadata re-stamp | **Unchanged** (2-line unrelated diff) | Keep `timestamp_us=None`. The join-snapshot half of this ask was **never true** — corrected in that doc 2026-08-12 |
| **3** public hang-up | **Unchanged** — still no `server.disconnect_client` | Keep the reach-through, but switch `getattr(client, "_connection")` → the **public** `client.connection`, which existed in 6.0.5 too |
| **4** `disconnect_from_client` doesn't stop the dialer | **Partially fixed.** 6.0.5 `await self._message_loop_task` (`connection.py:1220`) → 9.1.0 `await self._connection_done.wait()` (`:2300`), with a `cancelled` flag gating the `.set()` (`:1634,1678,1691`), so the cancel now propagates. Still **synchronous**, and the `finally` still clobbers `_connection_tasks[url]` unconditionally | Keep `_stop_dialing` (it degrades to a correct await-for-death); rewrite its now-stale docstring |
| **5** `_schedule_cleanup` orphans its timer | **Unchanged, and worse** — see below | Keep `_cancel_pending_cleanup` |
| **6** encryption trap | **Confirmed, and understated.** `allow_unencrypted` is real and defaults False — but it is necessary, not sufficient (breaks #1 and #3 above) | Rewrite that entry against this document |

## What gets worse

- **§5's eviction bug is unfixed and the window grew 6×.** `CLIENT_CLEANUP_DELAY` went **30 s → 180 s**
  (`server/client.py:53`). `_schedule_cleanup` still assigns `_cleanup_handle` without cancelling,
  and `_do_cleanup` still guards only on `self._connected` with no registry-identity check. So the
  orphaned timer that evicts a re-routed *playing* speaker now has three minutes to do it, arriving
  far later than the unroute that armed it and correspondingly harder to attribute. 9.1.0 adds a
  private `_cancel_cleanup()` helper, which our workaround could call instead of poking the handle —
  but it does not fix the orphan, because it only cancels the handle the orphaning already dropped.
- **Encoding moved off the thread pool onto the event loop.** `_transform_and_deliver`'s docstring
  goes from *"parallelized across unique TransformKeys via a thread pool"* to *"runs sequentially on
  the event loop, yielding every few keys"*, and `_encode_pcm_sequence` became `async` with a 500 ms
  yield interval. On a Pi driving several heterogeneous-codec endpoints from the loop that **also**
  serves the mesh API on :5001, that is a plausible xrun source. Measure; do not assume.
- **`min_buffer_ms` and the lead-time formula.** The 250 → 500 default is server-side only and is
  overwritten by the client's first `client/state`; the client SDK default is still 250, so it never
  applies to our own player. The real change is `_role_send_ahead_us`: for a live source it is now
  `min_buffer + static` where it was `max(required_lead, min_buffer) + static`. Identical at our
  defaults, but a client declaring a small `min_buffer` and a large `required_lead` now gets *less*
  lead. Late-join anchoring also now includes the full send-ahead — and a cross-server roam **is** a
  late join, so the code path behind "a roam is inaudible" changes, in the safe direction.

## New dependencies

9.1.0 adds three **mandatory** runtime deps (not optional extras, so they install even with
encryption disabled): `cryptography>=42`, `noiseprotocol>=0.3.1`, `cpace>=0.1.0`.

Build risk is low — both new ones ship `py3-none-any` wheels and `cryptography` has manylinux aarch64
wheels, so the arm64 image should not need a Rust toolchain. Worth recording rather than worrying
about: **`cpace` has exactly one release ever** (0.1.0, 2026-07-13) by Artur Pragacz, the same
contributor who wrote the aiosendspin encryption PR. A one-release PAKE implementation becomes a hard
dependency of the audio backend. The primitives come from `cryptography`, so this is a supply-chain
note, not a defect.

## Do these regardless of the bump

1. **A/B the seamless `refresh_stream` on the pinned 6.0.5.** This is the highest-value finding in
   the whole exercise. 8.0.0's *"keep Sendspin stream alive when replacing active PushStream"*
   changed only the **caller** — `PushStream.clear()` and `stop(keep_stream=True)` already exist in
   6.0.5 (`push_stream.py:2495`, `:2541`) with identical semantics. Our `SourceFeeder.refresh_stream()`
   goes through `group.start_stream()`, so the seamless path is testable **today**, against the
   deliberate audible discontinuity `CLAUDE.md` records as the cost of the membership rule.
   It may even be a true wire no-op: `stop(keep_stream=True)` skips the `on_stream_end` fan-out, and
   the successor's `on_stream_start` only *marks* a pending start, which is then deduplicated against
   `_last_sent_format` — state that lives on the role and survives the swap. Whether the audio is
   genuinely gapless is a timing question the source cannot settle: it needs the rig A/B, capturing
   frames exactly as the 2026-08-10 `unit-7204` measurement did.
   **Caveat:** upstream gates *its* version behind `allow_noncompliant_clients=False` (strict mode),
   explicitly *"while legacy clients still mishandle stream/clear"* — backwards for us, since the
   legacy clients it protects against are the ones we need. Our own call would not be gated, which
   is precisely why it must be measured against a real ESP32 client and not just our own player.
2. **De-privatise `client._connection` → `client.connection`** in `release_foreign_client`. The
   public property existed in 6.0.5 all along (`server/client.py:190`).
3. **Fix the §2 doc claim.** Done 2026-08-12.

## Recommendation

**Hold the pin.** The blocker is not effort, it is that `sendspin-cpp` has no encryption as of v0.7.2
— so `allow_unencrypted=True` is **permanent, not transitional**. We would pay the full identity,
pairing, and atomic-cutover cost in order to run in a mode upstream itself labels *"non-spec
transition mode"*. Against that, the one real prize is Open #6 (multi-server arbitration), which is a
single conformance gap.

Add that 9.x shipped **three majors in five days** (8.0.0 Aug 7, 9.0.0 Aug 10, 9.1.0 Aug 11) and that
`cpace` is a one-release dependency, and the version worth porting to is probably not 9.1.0.

**Revisit when any of these becomes true:**
- `sendspin-cpp` gains Noise/PSK support — this is the big one, and it also removes the interop
  argument for staying;
- we want the **source role** (a line-in or ESP32 capture device feeding a unit — additive ingest,
  *not* a reason to change the routing model, see below);
- we want **seek** in the GUI;
- the 9.x line goes a month without a major.

**Port order when we do go**, since the dependencies are strict: `mesh_smoke.py` first (it is the
mandated gate and is itself broken), then identity + pairing, then `client/state`, then the ~20
mechanical renames, then the frontend's two streams.

## Explicitly not a reason to bump: the source role

`Roles.SOURCE` now exists, and two `CLAUDE.md` rules are written around its absence. It does **not**
change the mesh model. There is no server↔server primitive — bridging would mean unit A running an
aiosendspin *client* holding the source role, dialled at unit B, which requires **pairing between
every pair of units** (`requires_pairing=True`, and trusted-unpaired is explicitly not enough for
source) plus a second encode/decode hop through a drift-correcting `SourceBridge`. That is a new
long-lived connection per pair and a new resynchronisation boundary — more of exactly what
`docs/ROUTING-MODEL.md` found every bug in. **"Servers stay, players roam" stands.**

## Needs a rig test, not a code read

1. **Do the ESP32 speakers and Music Assistant work at all through `allow_unencrypted=True`?** The
   transition path is explicitly non-spec and its `LegacyServerHelloPayload` differs from what 6.0.5
   sent. **This gates the entire bump** and cannot be answered by reading.
2. **Can four units cut over without a dead window?** A 9.1.0 client cannot reach a 6.0.5 server, so
   there is no rolling upgrade.
3. **Does a legacy-mode `ConnectionReason.PLAYBACK` still make an ESP32 leave its current server?**
   The clamp does not fire for PLAYBACK, but the legacy hello is a different message shape. This is
   the roam primitive.
4. **Does our `ConnectionReason.PLAYBACK` dial surface as `Activity.PLAYBACK`** in `server/activate`?
   The new arbiter ranks on activity, and a dial declaring none ranks 0 and loses the tiebreak.
5. **Re-measure roam audibility** against the changed lead-time formula and late-join anchoring.
6. **Measure xruns** with several heterogeneous-codec endpoints, against the event-loop encoder.

## Open design calls — no mechanical answer

- **`_health()`'s ERROR state has nowhere to go.** `synchronized`/`error` both collapse to
  `available=True`; only `external_source` maps to `False`. Whether `available=False` is an
  acceptable proxy for ERROR, or the signal moves to the now-free-form `player.state` string, is a
  call to make with a starved renderer on hardware.
- **How does the GUI name its target source** once client ids are key digests? The
  `ctrl:<source_id>:` hint has no carrier, and `_default_controller_source` (Open #20) is only the
  ambiguous-default half of that mechanism.
- **The new arbiter has no policy hook.** `SendspinClient.__init__` takes no admission-policy
  argument; overriding means subclassing and replacing the private `_should_admit_connection`.

## Method

Both trees read side by side: 6.0.5 from the local install, 9.1.0 from the PyPI sdist, extracted to a
scratchpad. Module-level diff first (nothing removed, 27 modules added: `noise/` ×11, `audio/` ×4,
`server/roles/source/` ×4, `client/{connection,management,models,source}`, `server/compliance.py`),
then signature and behaviour reads on every symbol we touch. Upstream release notes and the sdist's
own tests were used as corroboration, not as primary evidence — note that `tests/conftest.py` is not
shipped, so the upstream suite is not runnable as extracted, and `tests/test_client_admission.py`
covers the *client-side* arbiter, not server-side admission.
