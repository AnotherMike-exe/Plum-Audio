# Plum-Audio — Architecture

> What the system **is**. Read this first, then `docs/CLAUDE.md` for the rules that follow from it.
>
> This describes what ships today on four Raspberry Pi units. What landed when, and the hardware
> measurements that proved each piece, are in `docs/PHASE-HISTORY.md`. The failures that shaped
> particular decisions are in `docs/HARD-WON-LESSONS.md`. Procedures live in `docs/OPERATIONS.md`
> and `docs/HOST-PROVISIONING.md`.

---

## 1. Why Sendspin

Plum-Audio replaces Plum-Snapcast's Snapcast server + custom federation backbone entirely. Sendspin
is the sole sync engine. Three properties drove it:

- **Metadata, artwork, colour and visualizer are first-class protocol *roles*, delivered out-of-band
  from audio.** On Snapcast every properties push triggered `onResync()` on every client — an
  upstream bug that five hand-built guards in the AirPlay control script only *mitigated*. On
  Sendspin a metadata push is not an audio-stream event, so **the resync storm cannot happen by
  construction.** Those five guards are gone and must not be ported.
- **WebSocket transport** (JSON control + binary media) — proxy and firewall friendly, unlike
  Snapcast's raw 1704 protocol.
- **2-D Kalman time-filter sync** that runs on ESP32 as well as Linux, and a mesh that is a
  designed-in capability rather than something bolted on.

## 2. What the library gives us, and the one thing it does not

`aiosendspin` (pinned **6.0.5**) supplies the server, the client, the roles, and the group/stream
lifecycle. Two carry-over gotchas and one refuted idea:

- **`SendspinServer` always binds mDNS on UDP 5353**, which collides with the host Avahi. It is
  started with `advertise_addresses=[]` and `discover_clients=False`; all connections are driven by
  URL, and our mDNS goes through the host's Avahi over D-Bus (`mesh/avahi.py`).
- **Ingest is via the in-process `PushStream`** (`prepare_audio` + `commit_audio` +
  `set_live_source`), never `Roles.SOURCE`, which is unmerged upstream.
- **There is no DISCOVERY pre-connect, and there cannot be.** A `SendspinClient` holds exactly one
  attached websocket, so a playing player cannot be warmed on a second server — and a DISCOVERY dial
  would *steal* it rather than stand by. This was tried on hardware and refuted. It is also
  unnecessary: a roam is already inaudible, because the player never flushes and its ~300 ms jitter
  buffer drains straight through the ~25-55 ms reconnect.

Workarounds we carry for library gaps — including a **conformance bug in `client/state`** — are
tracked in `docs/UPSTREAM-AIOSENDSPIN.md`, which is the checklist to revisit on every pin bump.

## 3. The mesh model — servers stay, players roam

Every unit runs **both a Sendspin server and a Sendspin player**. Servers own audio ingest; players
are roamable render endpoints. Cross-routing is achieved by **servers dialing and reclaiming
players**, never by bridging audio between servers.

```
 iPhone ──AirPlay──► LIVING ROOM unit                  KITCHEN unit        BEDROOM unit
                     ┌──────────────────────┐          ┌──────────┐       ┌──────────┐
                     │ shairport-sync        │          │ player   │       │ player   │
                     │   ↓ PCM (FIFO)        │          │ (hw out) │       │ (hw out) │
                     │ Sendspin SERVER       │          └────┬─────┘       └────┬─────┘
                     │  PushStream → Group A │               │                  │
                     │   ├── LR  player      │◄──────────────┴── reclaimed ─────┘
                     │   ├── KIT player      │   (server dials players by URL)
                     │   └── BED player      │
                     │ Sendspin PLAYER       │
                     └──────────────────────┘

 Simultaneously BEDROOM ingests Bluetooth into its OWN server's Group B — independent clock and
 stream — playing only to itself. Multiple concurrent groups = multiple active servers, each
 hosting its own group(s); players roam to whichever group the user routes them to.
```

**Two routing tiers:**

1. **Intra-server** — the player is already on the ingesting unit's server. Route is a live
   `group.remove_client` / `add_client`: no reconnect, no ALSA re-init.
2. **Cross-server** — the target server calls `connect_to_client(player_url)`; the player leaves its
   current server with `GoodbyeReason.ANOTHER_SERVER`, joins the target, and is added to the group.

**Ordering matters:** correct routing is `remove_client(old)` → `add_client(new)`. A bare
`add_client` onto an already-grouped player stops that player's current source.

### Stream membership is fixed at `start_stream()`

**This is the single most surprising property of the library, and it was live for months looking
like a dead speaker.** A client that *connects* while a stream is live receives it at handshake. A
client that was already connected and is *then* added to the group does **not** — it sits in the
group, visible in the GUI, at the correct volume, and completely silent, with nothing in any log.

`attach_player` therefore calls `SourceFeeder.refresh_stream()` after `add_client`, re-acquiring the
stream so the new member is in it. The cost is that the whole group's stream restarts, so existing
listeners take a brief discontinuity on a routing change — the deliberate trade.

Roaming hides this entirely, because a reconnect gets the stream for free. Only the intra-server
path was ever affected, which is why a passing roam test proves nothing here. Do not optimise the
refresh away.

### Why not a master server with audio bridging

Feeding every unit's local source into one master server needs inter-unit PCM transport, i.e. the
unmerged `Roles.SOURCE` path. Servers-stay/players-roam keeps **all ingest in-process on the unit
that physically received it**, using only shipped primitives for the cross-unit part. It is also the
conceptual shape of the old Snapcast federation, so the orchestration concepts ported directly.

## 4. Per-unit process model

One container per unit. **supervisord runs exactly four programs**: `sendspin_server`,
`sendspin_player`, `config-api` and `nginx`.

- **`sendspin_server`** (:8927) — the in-process `SendspinServer`, the PushStream feeders (one per
  active local source FIFO), the group/stream lifecycle, and the metadata/artwork/visualizer role
  state. Also hosts the **mesh orchestrator** and the **source managers**, because both must reach
  the audio event loop.
- **`sendspin_player`** (:8928) — the unit's hardware render endpoint, out to `hw:<card>` via
  PortAudio. Attached to exactly one server at a time. Its jitter buffer is what makes a roam
  seamless, and it echoes its volume and its actually-opened output back into
  `/data/player_state.json`.
- **`config-api`** (:5002, Flask) — settings, integrations, audio. Pure persistence.
- **`nginx`** (:80) — serves the built React app and proxies both APIs same-origin.

**Source daemons are not supervisord programs.** The source managers own them, so `ps` inside the
container is how you confirm shairport-sync / go-librespot / bluealsa are up. A source missing there
but enabled in `settings.json` is a manager problem, not a supervisord one.

**Two processes own persistent state, deliberately kept apart.** The config API owns
`settings.json`; the audio process owns `/data/player_state.json`. The player's level and chosen
output are runtime state the player is the source of truth for, not user configuration — a separate
file means no cross-process writers and no API round-trip just to remember how loud the room was.

**FIFO handling is single-consumer** — one feeder reads each source FIFO. No `tee`; that complexity
existed only in the abandoned dual-engine plan.

**The host keeps what only the host can do**: the Bluetooth radio and its AVCTP channel, the D-Bus
policy, Avahi, WiFi (NetworkManager owns `wlan0`), and the audio HAT's device-tree overlay and
mixer. See `docs/HOST-PROVISIONING.md`.

## 5. Sources — the source-manager contract

Every multi-instance source follows one shape (`sources/source_manager.py` is the base,
`spotify_manager.py` the reference):

- One daemon per configured **endpoint**, writing PCM to `/tmp/<source>-<id>-fifo`.
- The manager polls `settings.json` **inside the audio loop** and reconciles both the Sendspin
  sources and the daemon processes — **source first**, so the feeder has created the FIFO before the
  daemon opens it.
- The integrations API is **pure persistence**. It does not render configs or respool daemons: that
  process cannot reach the audio loop, and the dev rig has no supervisord. One reconciler makes rig
  and container behave alike.
- Config templates are rendered through `sources/config_render.py`, which **escapes for the target
  syntax and writes atomically**. A device name reaches a quoted libconfig/YAML scalar and then a
  daemon is respooled; shairport's `sessioncontrol` can run shell commands.
- Artwork is decoded through `sources/artwork.py`, on a worker thread. Never on the audio loop.

Per source: **AirPlay** runs a private D-Bus session per endpoint, because shairport's MPRIS bus
name is fixed and multi-endpoint would otherwise collide. **Spotify** uses go-librespot over HTTP+WS
(no D-Bus; spotifyd was dropped when 0.4.x removed MPRIS). **Bluetooth** relays metadata over AVRCP
— A2DP carries none — and its position ceiling lives in `bluetoothd`, which is why the host needs
patching. **DLNA and Plexamp have no backend at all**: a settings stub and a GUI card, nothing else.

## 6. Routing, groups, and the three volumes

A *group* is {a source on some unit's server} + {the set of players currently rendering it}. Groups
coexist; a player belongs to at most one at a time, so **contention is defined**: routing a player
elsewhere pulls it, and that is the `ANOTHER_SERVER` handoff.

A source's group and its anchor client (`src:<source_id>`) persist whether or not anything is
streaming, so routing survives an idle source. The anchor is a transport-less non-player client,
which is what stops the group being auto-deleted when the last real player leaves.

**Three volumes, and only two are the protocol's:**

| Level | What it is | How it moves |
|---|---|---|
| Per-player | One endpoint's own output | Player-role command · `POST /api/mesh/volume` |
| Group | Every endpoint on a source | Controller-role `volume`/`mute` — **the library does the delta-preserving redistribution**; do not fan out per client |
| Source | The level on the **sending device** — the phone's slider, Spotify Connect | Not a Sendspin concept. `POST /api/mesh/source-volume` + `SourceState.source_volume`, driven per source |

Source volume stacks with the endpoint levels and must never be conflated with them in the GUI. The
main card's slider is **this unit's own endpoint**; the group is moved deliberately, via the group
buttons; the source slider appears only when the source can actually accept one.

**A server cannot read a player's level, only command it.** `PlayerV1Role.set_volume()` does not
move the server's own view — only the player's `client/state` does, and the library sends exactly
one of those, at connect, carrying `initial_volume`. So the player **must** echo after every
command, and persist it, or every level in the mesh reads 100% forever while the audio is
demonstrably quieter.

## 7. Auto-route and follow

`mesh/follow.py`. Two behaviours, both configured in Settings → Playback and both reconciled by
polling — there is no event for "a source went active" or "a player was routed".

- **`localActivity`** — when this unit's own player is idle and one of its own local sources becomes
  active, route the player onto it. Keyed on the **inactive→active edge**, a genuinely new
  connection, not a source that was already streaming. So "just AirPlay to the kitchen speaker"
  needs no manual routing, while selecting *None* during a live AirPlay session sticks.
- **`slave`** — mirror another unit's player (`masterUnitId`): follow whatever stream it is on, and
  follow it into idle when it stops. **A local override always wins** — once the user moves the
  follower off what follow put it on, follow keeps hands off until the follower is manually back in
  sync with the leader, or slave mode is turned off.

Both the follower's and the leader's status come from `UnitSnapshot.local_player` — each unit's
player self-reports where it is attached, which is the only way to learn this once a player has
roamed onto a different unit's server. If a leader's speaker is attached to a server outside our
mesh (Music Assistant, any foreign Sendspin server) there is no `source_id` to route onto, and
follow degrades to a no-op until it returns.

**A `source_id` is only unique within a unit** — every unit's own AirPlay endpoint is happily also
`airplay-1`. A target is therefore tracked as an `(owning_unit_id, source_id)` pair everywhere, and
following a leader's source must go through the explicit peer-delegate primitive rather than
`Router.route_player`, which resolves a bare id against our own view and would silently attach the
player locally instead.

## 8. Interop — we are a peer on a standard network

Third-party interop is the point of adopting a standard protocol, not a bonus.

- **mDNS goes through the host's Avahi over D-Bus.** Players advertise `_sendspin._tcp`, servers
  `_sendspin-server._tcp`. This is what makes us discoverable by Music Assistant. A client picks ONE
  direction: while advertising we are server-dialed and must not dial out.
- **The mesh view and the neighbourhood are different things.** The view is our own units,
  aggregated from peer snapshots over `/api/mesh/*`. The neighbourhood
  (`GET /api/mesh/neighbourhood`) is every Sendspin service mDNS can see on the segment, ours or not.
- **Adopt and release** bring a foreign speaker into one of our groups and hand it back. A speaker
  is identified by its **registered URL**, never by a client id — mDNS names by instance and the
  handshake by MAC, so those genuinely differ. `connect_to_client(url)` is a no-op when a dial
  registration already exists, so adopt cancels the existing dial first; without that, re-adopting a
  speaker that rebooted silently does nothing and then times out claiming it never connected.
- **The consume relay** (`ws://…/api/mesh/consume`) makes a session on a *foreign* server observable
  and controllable from our GUI — metadata, artwork and visualizer state relayed from the player
  that is rendering it. It is our own protocol on our own port; nothing about it touches the wire.

**One spec MUST remains unmet**: multi-server arbitration. We persist the `server_id` of the server
that most recently had us playing, but we cannot yet *decide* between two servers, because the
library commits to a dialing server before `connection_reason` is readable. See UPSTREAM §1.

## 9. Metadata, artwork and the visualizer

All out-of-band, on Sendspin roles, never on the audio stream.

Progress is an **anchor**, not a ticker: the source publishes `track_progress` with a server
timestamp and a playback speed, and every consumer extrapolates
`progress + (now − timestamp) × speed`. This is why a source's own 1 Hz updates are suppressed by
the library when they merely track wall clock — client frame rate is not a health signal.

The **visualizer role is per-SOURCE and server-computed**: the server bins its own FFT of the source
audio to the shape the client requested. That is what makes it cheap, and it is also the boundary —
audio rendered from a foreign server has no source on our side to analyse, so it cannot be
visualized. The wire format the GUI implements is documented in
`docs/SENDSPIN-CONTROLLER-PROTOCOL.md`.

## 10. Audio output selection

**The output device's identity is the ALSA card name, never `hw:C,D`.** Card numbers move — the
HiFiBerry on one unit was card 2, then 1, then 2, then 0 across four reboots with config unchanged.
`settings.json` stores `<card_name>:<device>`; `hw:C,D` is re-derived on every scan, for display and
`speaker-test` only.

**Availability rests on three signals that fail in different places** and must not be collapsed into
one: `is_active` (what we are configured to render to — survives everywhere), `in_use` (read from
`/proc/asound`, which is masked in the container and so bind-mounted from the host at
`/host/asound`), and PortAudio exposure. PortAudio **enumerates by opening**, so a card we hold
exclusively vanishes from its device list entirely.

A switch applies live, without restarting the player, and the player **echoes the device it actually
opened** — which is what lets the API report `pending` rather than claiming a switch that never
happened. A failed switch restores the previous device rather than leaving silence.

## 11. The GUI

nginx serves the built app from inside the unit container, so the GUI is same-origin and needs no
CORS, no dev proxy, and no build-time host baked in.

- **The page IS the unit that served it.** The mesh view is identical from every unit, so identity
  can only come from the responder — `local_unit_id`. "Our own" players are matched by listener host.
- **One controller WebSocket per SOURCE**, not per unit (`ctrl:<source_id>:<nonce>` client id): a
  client sits in exactly one group, so one socket sees exactly one source. The nonce is persisted so
  a page reload does not mint a new identity.
- **The browser player dials IN** — the inverse of the native player, which is dialed by servers.
  A browser tab has no listener socket, so cross-server roam does not apply to it.
- **Device pickers list active sources only.** A source exists for every configured endpoint whether
  or not anyone is feeding it, so the unfiltered list shows ghosts long after the phone
  disconnected. Idle devices are still routable — the router always supported it.

## 12. Container and deployment

The base image is pinned to **`python:3.13-slim-trixie`** — glibc for trivial PyAV/PortAudio/numpy
wheels, and *trixie specifically* to match the units' Debian 13 so bluez-alsa and shairport-sync
behave as validated. The Dockerfile **asserts its build-time dependencies**, because every one of
those failures is invisible at runtime: a dead transport control, or a Bluetooth source that simply
never appears.

Host networking is required — mDNS needs layer 2. `/config` holds config and logs, `/data` holds
runtime state; there is no `/media`. Procedures, and the deploy conflicts that fail deceptively, are
in `docs/OPERATIONS.md`.

## 13. Risks and open questions

| Risk | Standing |
|---|---|
| `aiosendspin` moves fast | Pinned 6.0.5; smoke-test on bump; workarounds tracked in UPSTREAM |
| Multi-server arbitration unmet | Spec MUST; needs a library change (UPSTREAM §1) |
| Third-party players vary | Codec choice belongs to the client; a heterogeneous group is normal |
| No unit coverage on `sendspin_server.py` | Including `refresh_stream` — the guard for the worst bug found so far |
| Bluetooth depends on host patches | Unpatched units silently lose scrub reporting |

**Open question:** route persistence across a reboot. Groups and anchors survive an idle source, but
nothing restores routing after a unit restarts.

## 14. Sources

- Sendspin spec: <https://www.sendspin-audio.com/spec/> · `aiosendspin`:
  <https://github.com/Sendspin/aiosendspin>
- Predecessor: the **Plum-Snapcast** repo — this repo implements the design its architecture doc set out.
