# Sendspin pairing — how it works here

> **Read this before changing `PLUM_ALLOW_UNENCRYPTED` or `PLUM_FLEET_PSK`, before touching the
> pairing UI, and before anyone asks "why can't Music Assistant see our speaker".**
>
> Written 2026-08-13 against the spec (<https://www.sendspin-audio.com/spec/>) and `aiosendspin`
> 9.1.0 as installed. §1-3 are the protocol; §4 onward is what Plum does with it.
>
> **Plum implements all three pairing methods, and unpaired access is off by default.** A unit pairs
> with its own speaker automatically, peers pair automatically when they share a fleet secret, and
> anything else is a deliberate act in the GUI. This document previously argued for the opposite —
> see §4 for what changed and why.

## 1. What actually changed

Until aiosendspin 7.0 a Sendspin connection was cleartext JSON over a WebSocket. From 7.0 the spec
makes encryption **mandatory for connections established through standard discovery**, using a Noise
`KKpsk2` handshake. Two things follow, and the second is the one that surprises people:

- **Identity became cryptographic.** A peer is its X25519 public key. That is why a client id is now
  a 43-character digest and why `/config/identity` matters (see `docs/OPERATIONS.md`).
- **The server is the Noise INITIATOR and the client is the responder — regardless of who dialled
  the socket.** So "who connects to whom" and "who drives the handshake" are different questions.
  Our player is always the responder even though servers dial it.

The handshake is four cleartext frames — `client/init`, `server/init`, two `noise/handshake` — after
which the transport is encrypted. Cipher suites: `25519_ChaChaPoly_SHA256` and
`25519_AESGCM_SHA256`; **servers MUST support both, clients at least one**. `aiosendspin` handles all
of this; none of it is ours to write.

## 2. The three PSK categories

Everything about pairing is really about *which pre-shared key* the handshake mixes in. The spec
defines three, distinguished by a `psk_id` hash:

| Category | What it is | Trust |
|---|---|---|
| **Sendspin PSK** | the long-term key minted by a successful pairing, persisted per (client, server) pair | `user` |
| **Pairing PSK** | a fixed per-client credential handed over out of band, in a token | `user` after finalize |
| **Sentinel PSK** | a **published constant**, used when no pairing record exists | `none` |

The sentinel is the important one for us: it is public, so a sentinel session is *encrypted but
unauthenticated*. The spec is explicit — **"unpaired playback connections are vulnerable to
man-in-the-middle attacks."** You get confidentiality against a passive listener and nothing against
an active one.

## 3. The three pairing methods

**Servers MUST implement all three. Clients MUST implement Pairing PSK** and may add the PIN methods
(and must implement them if they advertise them in `supported_pair_methods`).

1. **Pairing PSK** — no interactive step. A token (QR or copy/paste) carries the credential; the
   client sends `client/pair-finalize` straight after `server/activate`. `aiosendspin` gives you
   `encode_token`/`decode_token` for the QR-friendly string.
2. **Dynamic PIN** — a per-session PIN derived from the handshake hash and nonces. The *client*
   displays or speaks it, the operator types it into the server, and a CPace X25519 PAKE round
   authenticates both directions. A failure counter escalates to gesture-gating after 10 bad
   attempts. (This is why `cpace` is now a hard dependency.)
3. **Static PIN** — a fixed 8-digit device PIN. Every attempt is gesture-gated: the operator must
   physically confirm on the device.

The API surface, as we drive it:

```python
# server side — pairing rides a dial, as an "operator intent"
await server.connect_to_client(url, pairing_attempt=PairingAttempt(
    method=PairMethod.DYNAMIC_PIN, pin_provider=..., languages=("en",), owner="michael"))
await server.initiate_pairing(client_id, attempt)   # on an already-connected client
await server.end_pairing(client_id)                 # give up, keep the connection
await server.unpair(client_id)                      # forget the long-term PSK

# client side — the window an operator gesture opens
client.open_pairing_window(); client.pairing_window_open; client.consume_pairing_window()
```

`PairMethod` is `dynamic_pin | pairing_psk | static_pin`; `TrustLevel` is `none | user`.

Two properties of these that are easy to assume wrong, and both were found by testing rather than
reading: **`pairing_psk` needs no pairing window** — possession of the token is the authorisation, so
a shared secret pairs with no gesture at all. And **`PairingAttempt` refuses `PAIRING_PSK` without a
`pairing_psk`**, so the token is not optional garnish; it is the method.

The window itself is `_PAIRING_WINDOW_LIFETIME_S = 300` and admits exactly **one** attempt, claimed
by the first device to pair — not "open for five minutes to anything that shows up".

## 4. What Plum does: real pairing, with the sentinel path off by default

**Implemented 2026-08-13.** An earlier version of this document argued for skipping pairing and
running everything on the sentinel PSK. Two of its reasons did not survive review — "our player has
no display" is false, because every unit serves a web GUI and Sendspin needs a network anyway; and
"the operator is `deploy.sh`" stops applying once fleet deploy is a testing convenience rather than
how units are commissioned. It is kept in git history rather than here.

All three methods are offered. The wiring is one object, and its *presence* is what enables them:

```python
PairingSupport(
    gesture_prompt = ...,   # enables static PIN
    pin_display    = ...,   # either out-channel enables dynamic PIN
    offer_static_pin = True,
    secret_locations = ("device", "operator"),   # a CLOSED vocabulary — see below
)
```

`secret_locations` reads like prose and is not. It is validated in `__post_init__` against
`SECRET_LOCATIONS = {device, leaflet, operator}`, so a descriptive string raises **in
`SendspinPlayer.__init__`** — before the renderer opens a card, before the listener binds. The unit
deploys clean, `sendspin_server` runs, and `sendspin_player` sits in supervisord's `STARTING` with no
player in the mesh view. It shipped to `.7.122` that way on 2026-08-13, because the local probes
build `SendspinClient` directly and nothing constructed a real `SendspinPlayer`. Pinned now by
`test_the_pairing_secret_locations_are_from_the_librarys_closed_vocabulary`.

Both callbacks route to the GUI over the consume relay as a `t: "pair"` frame — loopback-only, so a
PIN never leaves the unit, and immediate rather than waiting on the 3 s state poll.

### Who pairs with whom, and how

| Pair | Method | Operator? |
|---|---|---|
| a unit ↔ **its own** speaker | Pairing PSK, from `/config/identity/local-pair.psk` | none — one device, one `/config` |
| unit ↔ **peer** unit's speaker | Pairing PSK, from the fleet secret | none, when `PLUM_FLEET_PSK` is set |
| unit ↔ **third-party** speaker | dynamic PIN, static PIN, or a pasted token | yes, in the GUI |

### How, mechanically: stage before the dial

Both automatic pairings above are **staged**, not initiated. `StagedPairingPsk` — the library's own
"operator-staged Pairing PSK awaiting a client" — is written into the server's pairing store *before*
the client is dialled, and `_psk_provider` consults it while **choosing** the handshake PSK. So the
connection comes up already in `PskCategory.PAIRING` and finalizes immediately. Two call sites:
`start()` for our own player (before the player process exists), and `reclaim_remote_player` for a
peer's, before the dial.

The alternative — `initiate_pairing` on an already-connected client — is what shipped first and it
does not survive a real mesh. Its PSK is the sentinel, so `_rehandshake_for_pairing_if_needed` tears
the Noise session down and rebuilds it mid-connection. A peer's player is contended (its own server
is dialling it too, and it holds exactly ONE websocket), so the re-handshake finds the socket gone:

```
could not pair player G2UChhEv…: expected Noise message 2 (TEXT), got CLOSE
[airplay-1] reclaim of remote player G2UChhEv… timed out          ← then, forever
```

Never stage or initiate against a **cleartext** client. A pairing handshake over the legacy path is
aborted by the library outright, so every ESP32 would go offline — see §5.

**The fleet secret** (`PLUM_FLEET_PSK`) is one Pairing PSK every unit accepts, minted once by
`deploy.sh` into the gitignored `docker/.deploy.env` and written identically to every unit. Without
it, four units mean twelve directed pairings, repeated whenever one is re-imaged — a new identity is
a new device to every peer. It is a **shared secret**: anyone holding it can pair with any unit.
Weaker than a per-pair record, stronger than the sentinel PSK, which is *published*. Leaving it
unset is a supported, stricter posture — units then pair only with their own speaker automatically.
Rotating it unpairs the fleet, so redeploy every unit together afterwards.

### Unpaired access is now OFF

`unpaired_access_enabled` + `trust_unpaired()` remain, gated by a setting whose precedence is
**settings.json > `PLUM_UNPAIRED_ACCESS` > off** — the `audio.output.device` shape, with a **null**
sentinel in `DEFAULT_SETTINGS` because `false` is a real user choice and a literal there would
outrank the env permanently.

It is the sentinel path: encrypted but unauthenticated, which the spec calls MITM-vulnerable. With
it off, an encrypted client that has not paired is admitted, negotiated, grouped — and activated for
nothing. That state is what the GUI's Pair button keys on.

Turning it off applies live on the server side (`untrust_unpaired` revokes every existing approval).
The client half rides in `client/hello` and so is fixed for a connection's life, taking effect on
the player's next restart — the same deliberate trade as a device rename.

## 5. What the GUI shows, and why

A device that cannot play must not offer controls that would silently do nothing. So **Pair replaces
Join Stream and the stream picker** on a device's row, rather than sitting beside them.

Four states, and telling them apart is the whole feature:

| State | Means | Button? |
|---|---|---|
| `cleartext` | `security: null` on a connected client — the legacy path | **no** — can never pair |
| `unpaired` | encrypted, no record, activated for nothing | **yes** — the only one |
| `trusted` | encrypted, playing, but on the sentinel PSK | no |
| `unknown` | an older peer, or a speaker nobody has connected to — **the default** | no |

`unknown` rendering nothing is the same rule as `has_player` defaulting true. Guessing `unpaired`
would put a Pair button on every mDNS speaker on the segment, most of them cleartext, every one a
dead end.

`trusted` is deliberately not folded into `paired`: it is not paired, and it is exactly the state
that disappears when unpaired access is turned off.

**Settings → Pairing** carries the fleet-wide action: open every unit for five minutes to accept one
new device each. That is the protocol's `management` role — a server already paired with a device may
stand in for its physical gesture — which is *why* a unit pairs with its own speaker at startup. The
fan-out goes from the GUI, not unit-to-unit: each unit opens only its own speaker, so the mesh call
is a nudge and never a transfer of trust over an API that has no authentication.

## 6. Doing it on a unit

```bash
# identities and secrets — a device certificate, not runtime state
docker exec plum-audio ls -la /config/identity
#   server.key  player.key  local-pair.psk  server-pairing.json  player-pairing.json   (all 0600)

# ACTIVATED vs negotiated: the only field that separates audio from silence
curl -s localhost:5001/api/mesh/view | python3 -c 'import json,sys; [print(p["player_id"][:16], p.get("security"), p.get("paired"), p.get("active_roles")) for u in json.load(sys.stdin)["units"] for p in u["players"]]'

# what pairing has been attempted here, and how it went
curl -s localhost:5001/api/mesh/pairing

# what the server decided at startup, and about whom
docker exec plum-audio grep -E 'identity|paired|pairing|unpaired' /config/logs/sendspin_server.log
```

Env: `PLUM_FLEET_PSK` (shared, base64url 32 bytes) · `PLUM_UNPAIRED_ACCESS` (deploy-time default
only; the GUI wins and persists) · `PLUM_STATIC_PIN` (exactly 8 digits, refused otherwise).

## 7. Music Assistant

MA 2.9.x pins `aiosendspin==6.0.5` and cannot speak Noise, so it can no longer claim a Plum speaker —
measured, see `docs/PHASE-HISTORY.md`. MA 2.10.0-beta pins 9.0.0 and dev pins 9.1.0, so this closes
on their side. Until then, route MA to a Plum **AirPlay** endpoint.

When MA does move, it becomes a server that must pair with our player. Our player offers all three
methods and displays a PIN in the GUI, so expect a step rather than silence — and if a speaker
appears in MA and plays nothing, read `active_roles` on our side first.

## 8. What is deliberately not built

- **`management/add-record`** — a paired server can *install* a peer's credential directly, skipping
  the PAKE. It would make fleet provisioning a push rather than a shared secret. Powerful, and a
  bigger security surface; not needed while `PLUM_FLEET_PSK` exists.
- **Turning `PLUM_ALLOW_UNENCRYPTED` off.** Blocked on two things we do not control: `sendspin-cpp`
  has no Noise in any release, and our own web GUI is a hand-rolled cleartext client on `:8927` with
  no proxy in front of it. Those are one change, not three — see `docs/SPEC-CONFORMANCE.md`.
- **Pairing the browser player.** It rides `@sendspin/sendspin-js` 3.2.1, which is cleartext, so it
  needs none. 5.0.0 is Noise-only and would need a per-browser trust flow.
