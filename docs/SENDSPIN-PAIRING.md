# Sendspin pairing — what it is, what we do instead, and what it costs

> **Read this before changing `PLUM_ALLOW_UNENCRYPTED`, before adding a pairing UI, and before
> anyone asks "why can't Music Assistant see our speaker".**
>
> Written 2026-08-13 against the spec (<https://www.sendspin-audio.com/spec/>) and `aiosendspin`
> 9.1.0 as installed. Plum currently implements **none** of the three pairing methods and relies on
> the unpaired path instead — a deliberate, spec-sanctioned choice with real limits, set out below.

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

The API surface, for when we do implement it:

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

## 4. What Plum does instead: trust-on-deploy

**We implement no pairing method.** Every Plum connection uses the sentinel PSK, and we make it work
with the spec's unpaired-access provision, which requires **both** halves:

- **client:** `ClientPairingConfig(unpaired_access_enabled=True)` — our player sets this in
  `sendspin_identity.client_pairing_store()`;
- **server:** `await server.trust_unpaired(peer_id)` — `sendspin_server._trust_player()`, called for
  our own player at startup and for a peer's player in `reclaim_remote_player` before it dials.

Miss either and the client is admitted, negotiated, grouped, at the right volume — and activated for
**no roles**. Silent, both logs clean. The measured truth table is in
`tests/Integration/t0_sendspin_protocol.py` steps 6–7.

**Trust is per-server AND per-peer.** A unit trusting its own player says nothing about a peer's.
That is why the trust call sits on the routing path rather than only at boot.

**Why this and not real pairing.** A pairing UI is an interactive, per-device operator flow; a Plum
mesh is a fleet provisioned from a workstation, where "the operator" is `deploy.sh`. Trust-on-deploy
matches how the units are actually commissioned, and costs nothing at run time. The security
difference is real but bounded: on the LAN these units already run unauthenticated HTTP APIs with
blanket CORS (`docs/CLAUDE.md` Open #8), so sentinel-PSK sessions are not the weakest link.

**And it does not apply to most of the traffic anyway.** Cleartext clients — every ESP32 speaker,
Music Assistant as a client, our own web GUI — skip the trust gate entirely: a legacy `client/hello`
is activated straight from the negotiated role set. That is what `PLUM_ALLOW_UNENCRYPTED=1` buys.

## 5. Doing it on our units, today

There is **no pairing to perform**. What exists is trust, and it is automatic:

```bash
# what identities this unit holds (its "device certificate")
docker exec plum-audio ls -la /config/identity
# server.key, player.key, server-pairing.json, player-pairing.json — all 0600, root-owned

# did the trust take? this is the only field that distinguishes audio from silence
curl -s localhost:5001/api/mesh/view |
  python3 -c 'import json,sys; [print(p["player_id"][:16], p.get("active_roles")) for u in json.load(sys.stdin)["units"] for p in u["players"]]'

# what the server decided, and about whom
docker exec plum-audio grep -E 'identity|trusted|unpaired' /config/logs/sendspin_server.log
```

A peer's player is trusted lazily, at the first cross-route to it, so "no trust line for unit B" is
normal until you route to B.

## 6. Doing it in the Music Assistant beta

MA 2.9.x pins `aiosendspin==6.0.5` and cannot speak Noise at all. **2.10.0-beta pins 9.0.0** and is
where their pairing work landed (#4846 encryption, #5472 pairing, #5591 auto-pair the built-in web
player, plus "use expanded_options for sendspin pairing method").

Their public docs do **not** document pairing yet — the player-support page still just says players
"appear automatically when clients connect" — so treat the following as the shape to expect rather
than a procedure to follow:

- MA becomes a **server that must trust or pair our player**. Our player advertises
  `unpaired_access_enabled`, so if MA offers unpaired access it should just work, exactly as our own
  server does for it.
- If MA instead insists on a pairing method, our player supports what `aiosendspin` implements —
  Pairing PSK by default. Expect a token or a PIN prompt in MA's player settings.
- Our player has **no display and no speaker**, so dynamic PIN (which requires the *client* to emit
  the PIN) is awkward for us; `pin_display` is None. Pairing PSK is the method that fits a headless
  Plum unit.

**Expect a step, not silence.** If MA 2.10 sees the speaker and it plays nothing, read
`active_roles` on our side first — that distinguishes "MA never trusted us" from anything else.

## 7. How this changes workflows and deployment

| | |
|---|---|
| **Commissioning** | One new artefact per unit: `/config/identity`. Created automatically on first boot, `0700`/`0600`, root-owned, excluded from the entrypoint's chown. Nothing to type. |
| **Deploy** | Unchanged in shape. `deploy.sh` now fails a unit whose player has no active `player@` role, so the silent-failure mode cannot ship quietly. |
| **Re-imaging a Pi** | **A new identity = a new device to every peer.** Trust is re-established automatically between Plum units (it is derived, not typed), but any *third-party* server that had paired with that unit must re-pair. |
| **Backups** | `/config/identity` is the one directory worth keeping. It survives `down`, `down -v`, `rm -f` and redeploys (bind mount at `/opt/plum-audio/config`). It does **not** survive a re-image or running the image without the compose mounts. |
| **Turning encryption on properly** | Blocked on `sendspin-cpp`, which has no Noise in any release. The day it ships, `PLUM_ALLOW_UNENCRYPTED=0` becomes testable — and that is also the day the web GUI's hand-rolled cleartext controller and the 3.2.1 browser player both stop working. Those are one change, not three. |

## 8. What we would have to build for real pairing

Not planned, recorded so the size is known:

1. a **server-side operator flow** — a pairing mode, a PIN entry field, a token scanner/paster, and
   the API to drive `initiate_pairing` / `end_pairing` / `unpair`;
2. **all three methods**, because the spec makes them mandatory for servers;
3. **persistence and revocation UX** on top of the pairing store we already carry;
4. a decision about the **web GUI**, which is a hand-rolled cleartext client today and would need a
   Noise implementation in TypeScript, or to move to `@sendspin/sendspin-js` 5.x (which is Noise-only
   and drops the caller-chosen `playerId` our browser-route reconciler joins on).

Item 4 is the expensive one and is the real reason `PLUM_ALLOW_UNENCRYPTED` is a standing setting
rather than a transitional one.
