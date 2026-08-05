# Plum Audio — Architecture & Rework Plan

> **Status**: Design / planning (2026-07). Supersedes `SENDSPIN-INTEGRATION-PLAN.md`,
> which scoped Sendspin as a *coexisting second engine* with federation deferred.
> **This plan**: Sendspin **replaces** the Snapcast + federation backbone entirely,
> in a **new `Plum-Audio` repo**, with the **mesh/cross-routing as a first-class,
> Phase-1 capability** and **multiple concurrent groups** supported.
>
> Decisions locked with the user (2026-07):
> | Question | Decision |
> |---|---|
> | Snapcast fate | **Full replacement** — Sendspin is the sole backbone |
> | Project form | **New repo** (`Plum-Audio`); port integrations + GUI |
> | Concurrency | **Multiple concurrent groups** (independent audio to independent endpoint sets) |
> | This session | **Plan + verify the mesh unknown** |

---

## 1. Why this pivot

Yesterday's hardware test showed Sendspin sync over WiFi is a notable improvement over the
Snapcast pipeline, and Sendspin's protocol structurally removes the pain we've been fighting:

- **Metadata / artwork / visualizer / color are first-class protocol *roles*, delivered
  out-of-band from audio.** On Snapcast, every `Plugin.Stream.Player.Properties` push
  triggers `onResync()` on all clients — the unfixable upstream bug that `CLAUDE.md §6`
  documents and that five hand-built guards in `airplay-control-script.py` only *mitigate*.
  On Sendspin a metadata push is not an audio-stream event, so **the resync storm cannot
  happen by construction.** The guards become unnecessary.
- **WebSocket transport** (JSON control + binary media) — proxy/firewall friendly, unlike
  Snapcast's raw 1704 protocol.
- **2-D Kalman time-filter sync**, runs on ESP32 and Linux → cheap future satellite speakers.
- **The mesh we want is a designed-in capability, not something we bolt on** (see §3).

---

## 2. Verified findings (source audit of `aiosendspin` 6.0.5, on R&D unit `.201.133`)

The load-bearing question was: *can a player be re-pointed at runtime — to a different
group, and to a different server — cheaply, the way our snapcast auto-switch fast-switch
does?* **Answer: yes, and the library is architected around exactly this.** Evidence from
the installed package:

| Primitive (in `aiosendspin.server`) | What it proves |
|---|---|
| `SendspinGroup.add_client()` / `remove_client()` + `_send_group_update_to_clients()` | **Intra-server routing is live control-plane.** Move a player between groups/streams over its existing WS — no reconnect. But `add_client` does NOT put an already-connected player into a stream that is already running; the feeder must re-acquire it. See "Stream membership". |
| `SendspinServer.connect_to_client(url, connection_reason=…)` | **Servers dial players.** A player is addressable (`ws://player:8928/sendspin`); a server initiates the connection to it. |
| `reclaim_client_for_playback(client_id, timeout_s)` — docstring: *"reclaim clients that disconnected with 'another_server'"* | **Cross-server roaming is first-class.** Contention between servers is a *designed* handoff, not a hack. |
| `ConnectionReason.{DISCOVERY, PLAYBACK}` | ~~**Two-tier presence.** Hold a player in a lightweight idle (DISCOVERY) connection; upgrade to PLAYBACK when audio routes to it.~~ **❌ REFUTED ON HARDWARE (2026-07-14) — this was a source-reading error.** A `SendspinClient` holds exactly ONE websocket (`attach_websocket` raises if already connected), so a player **cannot** be held on a 2nd server while playing on the 1st. Worse, `connection_reason` is only reported in `server/hello`, which the client sees *after* it attaches — so a player cannot even decline a DISCOVERY dial while busy; it would surrender its current server. Pre-connect is removed. **It is also unnecessary:** the roam is already inaudible (next row). |
| Player jitter buffer (~300 ms) vs. reconnect (~25–55 ms) | **This is the real gap-masker.** A roam fires no `stream_clear`/`stream_end`, so the buffer is never flushed — the DAC drains straight through the reconnect. Measured: emitted-silence counter (`pad_ms`) is *unchanged* across a roam. ~6× headroom. |
| `GoodbyeReason.ANOTHER_SERVER` | Explicit protocol reason for a player leaving server A to join server B. |
| `group.start_stream()` auto-calls `request_client_playback_connection()` for disconnected group members | Multi-server reclaim is wired into the normal "start playing" path. |
| `Roles = {PLAYER, CONTROLLER, METADATA, ARTWORK, VISUALIZER, COLOR}` | Metadata/artwork/visualizer/color are out-of-band roles — resync storm gone; visualizer feed is native. |
| `PushStream.prepare_audio()` / `commit_audio(play_start_us=)` / `set_live_source()` | Mature in-process ingest path (MA-proven). No dependency on the unmerged `Roles.SOURCE`. |

**Conclusion:** the mesh is de-risked at the design level. What remains is the *empirical
audible cost* of each transition (measured/probed in §7), not whether it's possible.

**Carry-over gotchas (still true):**
- `SendspinServer` always constructs `AsyncZeroconf` (binds UDP 5353) → collides with our
  container/host Avahi. Mitigation unchanged: `start_server(advertise_addresses=[],
  discover_clients=False)` and/or monkeypatch the zeroconf constructor; we drive
  connections by explicit URL, not mDNS.
- `aiosendspin` is fast-moving — **pin** a known-good version (6.0.5 validated) and add a
  smoke test around `start_stream` / `PushStream` / `reclaim`.

---

## 3. Target architecture — the mesh

**Core model:** every Plum Audio unit runs **both a Sendspin server and a Sendspin
player**. Servers own audio ingest; players are roamable render endpoints. Cross-routing is
achieved by **servers dialing/reclaiming players**, not by bridging audio between servers
(which would need the unmerged source role).

```
   iPhone ──AirPlay──► LIVING ROOM unit                    KITCHEN unit            BEDROOM unit
                        ┌───────────────────┐              ┌──────────┐           ┌──────────┐
                        │ shairport-sync     │              │ player   │           │ player   │
                        │   ↓ PCM (FIFO)     │              │(hw out)  │           │(hw out)  │
                        │ Sendspin SERVER    │              └────┬─────┘           └────┬─────┘
                        │  PushStream→Group A │                   │  reclaimed to LR       │
                        │   ├── LR player ────┼───────────────────┼── Group A ─────────────┘
                        │   ├── KIT player ◄──┘ (server dials/reclaims players by URL)
                        │   └── BED player ◄──┘
                        │ Sendspin PLAYER (own hw out) │
                        └───────────────────┘

   Simultaneously, BEDROOM ingests Bluetooth into its OWN server's Group B — independent
   clock/stream — playing only to the bedroom player. Multiple concurrent groups = multiple
   active servers, each hosting its own group(s); players roam to whichever group the user routes.
```

**The two routing tiers:**

1. **Intra-server** (players already on the ingesting unit's server): route = live
   `group.remove_client` / `add_client`. No reconnect, no ALSA reinit (given a stable
   format). This is the cheap, common case for units already grouped together.
   **`add_client` alone is not enough when the source is already streaming** — a stream's
   membership is fixed when `start_stream()` is called, so a player that was ALREADY connected
   and is then added to the group never receives it: it sits in the group, in the GUI, at the
   right volume, and silent. `attach_player` therefore calls `SourceFeeder.refresh_stream()`
   after `add_client`, which re-acquires the stream so the new member is in it. The cost is that
   the whole group's stream restarts, so existing listeners get a brief discontinuity on a
   routing change. See §"Stream membership" below — this was live for months and looked like a
   dead speaker.
2. **Cross-server** (pull a player from another unit): the target server calls
   `connect_to_client(player_url)` → player leaves its current server with
   `ANOTHER_SERVER` → joins target → `add_client` to the group. No DISCOVERY pre-connect (a
   client holds one websocket — see §2); the switch is already inaudible because the player's
   jitter buffer drains through the reconnect.

**Why this beats the alternative (single master server + audio bridging):** feeding every
unit's local source into one master server needs inter-unit PCM transport = the unmerged
`Roles.SOURCE` path (high risk, per the original plan's risk register). The
"servers-stay / players-roam" model keeps **all ingest in-process on the unit that
physically received it** (mature PushStream) and uses only shipped, designed primitives for
the cross-unit part. It's also the exact conceptual shape of our *current* Snapcast
federation (remote snapclient roams to the ingesting server) — so the orchestration
concepts port over.

---

## 4. Reuse vs. replace ledger

**Reused (source/integration layer is protocol-agnostic — it writes PCM to a FIFO and emits
metadata):**
- Integration services: `shairport-sync`, `go-librespot`, `gmrender-resurrect`, `bluealsa`, Plexamp.
- Multi-instance endpoint APIs + control-script wrapper pattern (`airplay_endpoints_api.py`,
  `spotify_endpoints_api.py`, `dlna_endpoints_api.py`).
- Metadata/artwork extraction in the control scripts (`airplay-control-script.py` & siblings)
  — but **re-targeted** from Snapcast Properties to Sendspin metadata/artwork roles.
- `playback_api.py` position architecture (Sendspin makes it cleaner / possibly redundant
  once metadata is a native role — TBD).
- Settings/integrations Flask layer; most of the GUI (NowPlaying, PlayerControls,
  Visualizer, theming, color extraction).

**Replaced:**
| Snapcast-era | Plum Audio |
|---|---|
| `snapserver` | in-process `SendspinServer` + per-source `PushStream` feeders |
| `snapclient` (integrated hw player) | Sendspin player (per unit), `hw:<card>` out |
| `federation/*` (ws_manager, router, discovery, remote_snapclient_manager, api) | **Mesh orchestrator** — same *concepts* (aggregate N units, route endpoints), new protocol (Sendspin WS + server-dials-player + reclaim) |
| `auto-switch-service.py` (slave fast-switch) | `reclaim` (library primitive). No pre-connect needed — the roam is inaudible (jitter buffer covers the reconnect); DISCOVERY pre-connect proved impossible (§2) |
| Browser audio wire protocol (`snapStreamService.ts`, `useBrowserAudioClient.ts`) | `sendspin-js` browser player (if browser playback is kept) |
| Snapcast JSON-RPC frontend transport (`snapcastService.ts`, `snapcastDataService.ts`) | Sendspin controller-role WS client + engine-agnostic data service |

**Dropped entirely:** the five AirPlay resync guards (volume debounce, track-change pause
guard, resume suppression, metadata debounce, content-dedup) — no longer needed once
metadata is out-of-band. Keep source volume debounce only if the *source* (MPRIS) still
warrants it.

---

## 5. Per-unit process model (new `Plum-Audio` container)

One supervised Python process tree per unit:

- **`sendspin_server`** — in-process `SendspinServer` (port 8927). Owns: PushStream feeders
  (one per active local source FIFO), the group/stream lifecycle, metadata/artwork/color/
  visualizer role state. mDNS advertising disabled (we drive by URL); our own discovery.
- **`sendspin_player`** — the unit's hardware render endpoint (port 8928), `hw:<card>` out.
  Connected to exactly one server at a time; reclaimed to whichever server is routing to it.
  Latency knob → `static_delay_ms`. Its jitter buffer is what makes a roam seamless.
- **Mesh orchestrator** (successor to `federation/`) — discovers peer units, aggregates their
  server/group/player state into the app's `Server`/`Stream`/`Client`/`Group` types, and
  executes routing (`connect_to_client` / `reclaim` / `group.add_client`) on user action.
  Exposes the same REST surface the frontend already calls (route, volume, snapshot) so the
  GUI port is minimal.
- **Integration services** (unchanged) writing PCM to `/tmp/<source>-fifo` + control scripts
  emitting metadata to the server's role state (replacing the Snapcast-properties path).
- **Flask APIs** (settings, integrations, audio, endpoints) — largely as-is.

**FIFO handling is now simpler than the dual-engine plan:** single consumer (the PushStream
feeder reads each source FIFO once). No `tee` needed — that complexity existed only because
we were feeding *both* snapserver and Sendspin. Full replacement removes it.

---

## 6. Multiple concurrent groups — routing model

Sendspin supports N independent groups across N servers natively (each group = its own
stream + clock domain). The orchestrator's job is to present this coherently:

- **App model:** a *Group* = {a source/stream on some unit's server} + {set of player
  endpoints currently rendering it}. Multiple groups coexist; a player belongs to at most
  one group at a time.
- **Route action:** "play `<source on unit X>` to `<players P…>`" ⇒ for each player: if
  already on X's server, `group.add_client`; else `connect_to_client`/`reclaim` then
  `add_client`. Removing = `remove_client` (back to a solo group on its current server).
- **Contention is defined:** a player can only render one group; routing it elsewhere pulls
  it (that's the `ANOTHER_SERVER` handoff). The GUI shows current group membership per player.
- **Volume — three levels, two of them the protocol's** (implemented 2026-08-03):
  - *Per-player* (one endpoint's output): player-role command, `POST /api/mesh/volume`.
  - *Group* (every endpoint on a source): the controller-role `volume`/`mute` command. **The
    library does the delta-preserving redistribution** (`roles/player/group.py`) and republishes
    the average as `controller.volume` — nothing to port, and it works identically against a
    foreign server (Music Assistant) through the consume relay.
  - *Source* (the level ON THE SENDING DEVICE — the phone's AirPlay/Bluetooth slider, the Spotify
    Connect device volume): **not a Sendspin concept**, so it rides our own
    `POST /api/mesh/source-volume` + `SourceState.source_volume` and is driven per source (MPRIS /
    `MediaTransport1.Volume` / go-librespot `/player/volume`). It stacks with the endpoint levels.
  - **GUI mapping:** the main card's slider is THIS unit's own endpoint (per-player REST) — moving
    it must never touch another room; the Synced Devices rows are the other endpoints; the group
    +/-/mute buttons are the protocol group command. The source slider appears only when the source
    can actually report/accept one.
  - **AirPlay writes go through `SetVolume`, not the property** — shairport-sync exposes MPRIS
    `Volume` READ-ONLY plus a custom `SetVolume` method. A Properties.Set is refused, and since the
    readback is a separate call the failure looks like success in the GUI.
  - **A server cannot read a player's level, only command it** — `PlayerV1Role.set_volume()` does
    not move the server's own view; only the player's `client/state` does, and the client library
    sends one at connect carrying `initial_volume`. So the player MUST echo its level after every
    command (`sendspin_player._publish_render_state`) or the whole mesh reads 100% forever, and it
    persists that level (`/data/player_state.json`) so a restart doesn't reset the room.

---

## 7. Mesh verification — spike status

- **API/design tier — DONE (this session).** Source audit confirms live intra-server
  re-routing and first-class cross-server reclaim (§2). This was the primary risk.
- **Protocol-timing tier — `_resources/spike/handoff_probe.py`**: runs a server + a
  programmatic PLAYER client that renders to the onboard DAC (PortAudio), feeds a continuous
  tone, performs handoffs, and reports the `stream_end`→`stream_start` gap, WS-disconnect count
  (live-re-route vs reconnect), and ALSA xruns + jitter-buffer starvations. Runs headless (no
  speakers); `--no-dac` measures the protocol gap only. **Loopback (dev) results so far**:
  intra-server re-route is a true **live re-route** — WS stays connected (0 disconnects),
  sub-ms control gap, and the ~250 ms player buffer lead fully absorbs it → **0 xruns**.
  Cross-server reclaim is **reconnect-class** (WS drops, ANOTHER_SERVER), ~80 ms control gap in
  loopback; the player-side reclaim handshake (goodbye-old → attach-new) still needs a proper,
  race-free implementation (Phase 2). Hardware xrun/gap numbers on `.201.133` still to be taken.

  **Load-bearing lifecycle rule discovered here** (affects all routing code): re-route MUST be
  `old_group.remove_client(player)` → `new_group.add_client(player)`. A bare `add_client` calls
  `old_group.stop()`, which would kill the source the player is *leaving* for every other
  listener. Each source group therefore keeps a stable player member (or the feeder self-heals),
  and `SourceFeeder` re-acquires its `PushStream` on `StreamStoppedError` rather than dying.

  **Stream membership — the second half of that rule (found on hardware 2026-08-04).** The probe
  above measured re-route against an idle-then-started source, which is why it reported a clean
  live re-route and missed this: a stream's client set is fixed at `start_stream()`. A client that
  CONNECTS while a stream is live is handed it during the handshake, but one already connected and
  later added to the group is NOT. Measured on `.7.204`: with `airplay-1` streaming, unrouting and
  re-routing the attached local player produced no second `Stream started` on the client and a
  renderer buffer that never left 0 ms. Nothing in the logs said anything was wrong.

  Roaming never exposed it because a cross-server roam RECONNECTS (`ANOTHER_SERVER`) and a
  reconnect gets the stream for free — only tier 1, the path advertised as seamless, was affected.
  It is also why every manual workaround was "unjoin it and rejoin it": that forces the reconnect.

  `SourceFeeder.refresh_stream()` re-acquires the stream after `add_client` when one is live (a
  no-op when idle — the next chunk starts a stream that includes everyone). Trade-off, stated
  plainly: the group's stream restarts, so anyone already listening hears a brief discontinuity
  when someone else is routed in. That is the same cost the manual workaround already paid, it
  only happens on a deliberate routing change, and it beats one endpoint being silently mute.
- **Audible tier — NEXT hardware step (needs user + speakers on two units).** Two-node test:
  `.133` ingests a source into Group A, `.113`'s player joins; then reclaim `.113` to a
  second server / move between groups and **listen** for gap/continuity. Mirrors the
  user-confirmed sync test from the first Sendspin trial. Blocked only on physical speakers +
  someone to listen.

---

## 8. Phased plan

### Phase 0 — Repo scaffold + spike harden
- `/new-project Plum-Audio` scaffold (Plum standards: `docs/`, `_resources/`, `ARCHITECTURE.md`
  seeded from this file, `CLAUDE.md`, Docker).
- **Base image decision:** R&D units are **Debian 13 (glibc)**, not Alpine/musl. glibc makes
  PyAV/PortAudio/numpy wheels trivial (the Alpine PyAV pain in old notes disappears).
  Recommend a **Debian-slim** base for Plum-Audio. Revisit multi-arch build.
- Pin `aiosendspin`; vendor a smoke test.
- Port the spike into a real `sendspin_server.py` skeleton.

### Phase 1 — Single-unit core playback
1. ✅ `sendspin_server.py`: in-process server + AirPlay FIFO feeder (self-healing PushStream).
2. ✅ `sendspin_player.py` supervised service → onboard DAC out; latency→`static_delay_ms`.
   Server↔player auto-attach glue (dial + re-attach, self-heals across restarts).
   shairport-sync config + supervisord/Docker wiring.
3. ✅ AirPlay metadata/artwork → Sendspin metadata/artwork roles (`sources/airplay_metadata.py`,
   in-process reader on shairport's metadata pipe; the five Snapcast resync guards dropped, not
   ported). Title/artist/album + 512×512 cover art confirmed live on hardware.
4. ✅ supervisord + Dockerfile + entrypoint wiring; mDNS-off. (Full settings/audio Flask APIs +
   GUI deferred — see Phase 3; not required for the single-unit milestone.)
5. **Milestone — FULLY ACHIEVED ON HARDWARE, IN DOCKER (2026-07, `.201.133`):** real AirPlay
   from an iPhone → shairport → FIFO → server → player → onboard DAC → speaker, **with live
   metadata + album art**, **0 xruns, no resync storm**. Runs as one supervisord container
   (image builds arm64; onboard DAC opens in-container via `--device /dev/snd`). Live re-route
   ~0.1 ms / 0 xruns; cross-server reclaim reconnect-class (~85 ms protocol gap). **Later
   corrected (2026-07-14): the reconnect is INAUDIBLE — the player's jitter buffer drains through
   it, emitting zero silence. The "~200 ms audible silence" estimate and the DISCOVERY-pre-connect
   mitigation were both wrong; pre-connect is impossible (§2) and unnecessary.**

### Phase 2 — Mesh (the differentiator)
6. Mesh orchestrator: peer discovery, state aggregation, routing engine
   (`connect_to_client`/`reclaim`/`add_client`). [No DISCOVERY-preconnect pool — refuted, see §2.]
7. REST surface parity with today's federation API (route, volume, snapshot) for GUI reuse.
8. Multi-concurrent-group model + contention handling.
9. **Milestone:** ingest on unit A, route to A+B+C; second independent group on unit B; smooth handoffs.

**Backbone BUILT + single-node-validated (2026-07, branch `feature/phase2-mesh`).**
`backend/scripts/mesh/` + the engine seam, all logic/HTTP-tested; boots inside `sendspin_server`
(`PLUM_MESH_ENABLED`, in-process — no new supervisord program):
- `discovery.py` — UDP broadcast beacon (:8929), TTL peer table. **Not** mDNS (python-zeroconf
  would bind 5353 → Avahi collision). Peer IP taken from the datagram source; derives peer
  server/player `ws://` URLs. *Two-node broadcast round-trip pending a 2nd LAN unit (loopback
  can't validate it).*
- `model.py` — normalized `UnitSnapshot`/`SourceState`/`PlayerState` + aggregated `MeshView`
  (`find_source`/`find_player`), JSON wire form. `PlumSendspinServer.snapshot()` supplies the local view.
- `sync_engine/` — `SyncEngine` ABC refit to the router-facing seam; `SendspinEngine` facade over
  `PlumSendspinServer` keeps the mesh aiosendspin-free. Adds `reclaim_remote_player()` (cross-server
  roam) to the server. (`preconnect_player()` existed briefly but was removed — see §2.)
- `aggregator.py` — local snapshot + peers' snapshots (polled via discovery+client) → one `MeshView`;
  peer `host` filled from the beacon source IP; unreachable peers omitted for the cycle.
- `router.py` — three paths: intra-server (`attach_local_player`), cross-server (`reclaim_remote_player`),
  or **delegate to the source's owning unit** (audio never leaves its ingesting unit). Deps injected.
- `api.py` — **aiohttp** (not Flask: must call the async router/aggregator in the audio event loop;
  WSGI would need a 2nd process). `/api/mesh/{snapshot,view,route,unroute,volume,source}`,
  federation-parity, CORS on. `client.py` — aiohttp client (aggregator poll + router delegate).
- `orchestrator.py` — composes the above around one running server; wired into `sendspin_server` main().

**TWO-NODE HARDWARE VALIDATION PASSED (2026-07-13, `.201.133` Pi4-02 + `.201.113` PoE-Temp):**
- **Discovery:** mutual beacon over the real LAN (each unit discovers the other; loopback couldn't test this).
- **Aggregation:** each unit's `/api/mesh/view` shows both units; peer `host` from the beacon IP; group_ids consistent across the HTTP snapshot poll between hosts.
- **Cross-server roam:** player ping-ponged 4 hops between units — **~54 ms route API, 0 xruns / 0 starvation** through every handoff. (The ~54 ms is the *protocol* gap; it is INAUDIBLE — see the reclaim-gap resolution below.)
- **Four bugs fixed, only reproducible on hardware** (commit `70ff0ed`): (1) `reclaim_client_for_playback` is synchronous, not awaitable; (2) disconnected clients left routing stubs — snapshot now connected-only; (3) reclaim URL must be the player's *own* listener URL (roamed players keep their origin host), now carried in `PlayerState`; (4) the Phase-1 auto-attach supervisor fought roams — `attach_local_player(supervise=not mesh_enabled)`.

**RECLAIM-GAP + LIVE-AIRPLAY VALIDATION PASSED (2026-07-14):**
- **The roam is inaudible.** Instrumented the renderer with unconditional silence accounting (`pad_ms`). Across a cross-server roam the player fires no `stream_clear`/`stream_end`, so its jitter buffer is never flushed and the DAC drains straight through the reconnect — measured `pad_ms` is *unchanged* across detach→attach. The DISCOVERY pre-connect idea was refuted (impossible + unnecessary — commit `de50035`, §2).
- **Live AirPlay end-to-end:** real shairport-sync PCM ingested (not a tone); **title/artist/album + 512×512 JPEG artwork** flowed to the Sendspin metadata/artwork roles; 0 xruns.
- **Multi-room:** player-133 + player-113 both joined `.133`'s live-AirPlay group and played it in sync.
- **Roam off live AirPlay:** player-113 detached holding **435 ms of real AirPlay audio** → reattached with the buffer intact, **`pad_ms` unchanged** (zero audible dropout). The tone-based finding holds for real bursty content.

**MULTI-CONCURRENT-GROUP VALIDATED ON HARDWARE (2026-07-13):** item 8 milestone met. Two sources
active at once on one unit (`airplay` + a runtime-created `spotify` via `POST /api/mesh/source`),
each anchoring its own group; two players split across them (one local, one roamed cross-server in
~54 ms) with an idle group coexisting on the other unit; **0 xruns under concurrent load**. Adds
(commit `77ff59b`): dynamic source create/stop REST, per-player volume/mute (`PlayerV1Role` via
`roles_by_family`; player applies as render-side gain), idempotent `attach_player`. A 5th
hardware-only bug fixed (`7e39c47`): the player read volume from the wrong payload level
(`payload.player.{volume,mute}`). Contention policy: routes idempotent, last-writer-wins player
placement, source groups persist with 0 players (anchor keeps the feeder alive for instant re-route).

### Phase 3 — Remaining sources + parity
10. Spotify / DLNA / Bluetooth / Plexamp feeders (same PushStream pattern).
11. Frontend: controller-role WS client, engine-agnostic data service, wire `App.tsx`
    handlers, port Settings.
12. Browser audio via `sendspin-js` (if kept). Visualizer over the native visualizer role.
13. WiFi setup, theming, polish. Hardware soak test across ≥3 units.

**SETTINGS CORE + SPOTIFY SLICE DONE (2026-07, branch `feature/phase3-sources-gui`).**

*Config APIs* — `backend/scripts/apis/` is a Flask host on **:5002** (the mesh API owns :5001 and must
stay in the audio event loop; see §5). `settings_api.py` (device name/hostname/theme/visualizer,
versioned, **atomic writes** — the file is a cross-process contract) + `integrations_api.py` (endpoint
CRUD). GUI: real Settings overlay wired into `MeshApp`, `useThemeSettings` applies theme/accent.

*Spotify = **go-librespot**, not spotifyd* — spotifyd 0.4.x dropped standard MPRIS (only
TransferPlayback/volume remain) and has no arm64 build with it. go-librespot ships native arm64 and
exposes richer metadata/transport over a loopback HTTP+WS API, with **no D-Bus at all**. Same house
rule as always: consume what the daemon natively provides. `zeroconf_backend: avahi` registers Connect
discovery through the system Avahi, so nothing binds 5353 behind our back.

*Two contracts worth not re-deriving:*

1. **Source manager = one owner, reconciled from `settings.json`.** `sources/spotify_manager.py` polls
   the settings file inside the audio loop and owns BOTH halves of an endpoint: its Sendspin source
   (group + feeder + monitor) and its daemon process. Endpoint add/rename/enable/disable/remove apply
   live, in seconds. The integrations API is therefore **pure persistence** — it runs in a separate
   Flask process and cannot reach the audio loop, and the dev rig has no supervisord, so a
   render-then-`supervisorctl` apply step was both unreachable from the right process and divergent
   between rig and container. New multi-instance sources should copy this shape, not the old one.
   Order matters on start: source first (its feeder creates the FIFO), then the daemon.
2. **One controller WS per SOURCE, never per unit.** A Sendspin client holds exactly one websocket and
   therefore sits in exactly one group, so a per-unit controller only ever observes the source it was
   grouped into. The GUI opens one controller per source with client id `ctrl:<source_id>:<nonce>`;
   `_maybe_group_controller` honours that hint (unhinted ids still land on the primary source).
   Transport advertisement is gated per source on that source having a remote.

*Also fixed here, both latent Phase-2 bugs a second source exposed:* the metadata role stores ONE
progress anchor and clients extrapolate from its timestamp — a bare `playback_speed` flip re-stamps a
stale anchor, so play/pause must re-anchor to the daemon's real position; and a peer advertising its
player as `ws://127.0.0.1:8928` made cross-server reclaim dial *our own* loopback (router now
substitutes the unit's beacon host — but rigs should still be configured with a LAN player URL).

**Hardware-validated on both Pis:** Spotify audio, metadata, artwork, transport, timeline, live
endpoint CRUD, and cross-server roam of a Spotify stream.

**INTEROP: WE ARE A PEER ON A STANDARD NETWORK (2026-07).** Standing on a spec is the point — a unit
must serve audio from its own integrations, render audio from *any* Sendspin server (ours, Music
Assistant, a third party), and put a GUI over both. Two things were in the way, both now fixed:

*Discovery.* Sendspin discovery is mDNS with two service types in opposite directions — players
advertise `_sendspin._tcp` (8928), servers advertise `_sendspin-server._tcp` (8927), TXT `path` +
optional `name`. We had BOTH switched off, because aiosendspin's python-zeroconf binds UDP 5353 and
the host Avahi owns it (and Avahi is not optional: it advertises AirPlay and Spotify Connect). So no
third-party server could find our speakers and we could see nothing but ourselves. `mesh/avahi.py`
registers and browses the same records through the system Avahi over D-Bus — same wire result, one
responder per host, the way go-librespot does it. `mesh/neighbourhood.py` publishes our server
record, watches both types, and serves `GET /api/mesh/neighbourhood`. Note the division of labour:
the **mesh view** covers Plum units (they answer `/api/mesh/snapshot`); the **neighbourhood** covers
the wider Sendspin network, which has no mesh API and is reachable only by the protocol itself. mDNS
is link-local, so this sees one L2 segment.

Two hardware-only lessons: Avahi resolves once per interface AND family (one player arrives as
loopback, link-local v6, docker0 and the real LAN address — addresses are merged per instance and
ranked, since a docker0-only advert is what made spotifyd unreachable earlier), and
`ServiceBrowserNew` announces before D-Bus signal handlers can attach, so cached entries were missed
until we moved to Server2's `ServiceBrowserPrepare`/`Start` pair.

*Direction.* The spec forbids mixing: "Do not manually connect to servers if you are advertising
`_sendspin._tcp`." Ours is the **server-dialed** direction (reclaim-by-URL requires it), so the
player refuses an explicit home-server dial while advertising.

**INTEROP PROVEN ON A THIRD PARTY (2026-07-21, VLAN-7 rig: Music Assistant 2.9.9 + a Home Assistant
Voice PE).** Not a stand-in — real foreign implementations on their own segment:
- MA discovered our player over mDNS and dialed it **1.0 s** after it began advertising.
- A speaker claimed by another server stays visible: the player process self-reports
  `{attached, server_id/name, group, playback_state, title/artist}` to its unit's mesh API
  (`local_player` in the snapshot), because a unit's server can only see clients attached to
  *itself*. The GUI renders "→ Music Assistant · <track>" rather than losing the device.
- `POST /api/mesh/adopt` pulls a FOREIGN speaker onto one of our sources — same primitives as a peer
  player (dial for PLAYBACK + `group.add_client`), since a foreign speaker is just a player whose URL
  came from mDNS. `POST /api/mesh/release` hands it back. Release needs FOUR steps and hardware
  settled it: detach from the group, cancel our dial, **close the live websocket**, forget the
  registry entry — the first three each looked sufficient and left the speaker ESTABLISHed to us,
  out of reach of its own server. Only `SendspinConnection.disconnect()` hangs up, and 6.0.5 exposes
  it solely via a private attribute (upstream ask).
- The GUI folds `/api/mesh/neighbourhood` into its device list, matching foreign speakers **by URL**:
  their mDNS instance name and Sendspin client_id differ (the Voice PE advertises
  `home-assistant-voice-a1b2c3`, connects as its MAC). One mover handles all three cases — ours in
  the mesh (router), ours held by a foreign server (adopt by URL; the router cannot reclaim what is
  in no unit's view), and not ours at all (adopt/release).
- MA as a controller target: a freshly connected controller lands in MA's OWN solo group (`d1c40416`,
  stopped, `[volume, mute, switch]` only) — but that is the WRONG bridge. Our PLAYER is a member of
  the playing group, and to a member MA emits the FULL command set + metadata + the visualizer role
  (256-bin spectrum + loudness) and honors commands back. **So we fully observe, control and
  visualize MA-served audio** (commit 6148204) via the player relaying its member-view to the GUI.

**CONSUME + OBSERVE + CONTROL FOREIGN PLAYBACK (2026-07-23, `6148204`).** The completeness of the
"consumption" side: our player negotiates PLAYER+METADATA+CONTROLLER+VISUALIZER+ARTWORK, so wherever it
plays — our source, a peer, or a foreign server (Music Assistant) — it observes that group's
controller state + visualizer as a spec member and can drive transport. The player is INVARIANT to
audio origin, so this is uniform. Relay to the GUI (internal, since the GUI talks to our server, not
the player): the player is a producer on a `/api/mesh/consume` WS (mesh API), streaming ctrl +
spectrum and taking commands; the GUI synthesizes a `foreign::` stream from the player's self-report
so the existing now-playing/transport/visualizer render on it. Album art rides the same relay
(the artwork role → a JPEG data URL). Reuses the mesh API (no new player port) — the only new surface is one WS. This is what makes "any Sendspin server serving our
endpoints" a first-class, controllable, visualizable source in the GUI. Hardware-verified against
real Music Assistant: featured now-playing WITH ALBUM ART, live visualizer off MA's spectrum, and
pause/play/next driving MA. The visualizer per-source boundary from the prior section is thus SUPERSEDED for the
player's own session — it visualizes whatever the speaker plays, foreign included, via the relay.

**VISUALIZER = the native Sendspin visualizer role (2026-07-23).** aiosendspin's `visualizer@v1`
role auto-computes `spectrum` and `loudness` (also f_peak/peak/pitch) from the source's PushStream
audio, in-library (numpy DSP in `roles/visualizer/features.py`, driven by `push_stream.on_audio_chunk`).
So the SERVER SIDE IS FREE: a controller that also negotiates `visualizer@v1` and is grouped into a
streaming source's group receives binary frames on the audio timeline. No browser audio, no
"Listen in Browser" dependency, no server code from us. Verified on hardware — with noise feeding a
source, a visualizer client got 270 spectrum + 270 loudness frames over ~9s.

Wire: client/hello carries `visualizer@v1_support` = `{buffer_capacity, rate_max, types:[spectrum,
loudness], spectrum:{n_disp_bins, scale:lin|log|mel, f_min, f_max}}`. Frames are
`[type:1][ts:8 BE][payload]`: **16=loudness** (`>H` uint16, 0-65535), **19=spectrum** (uint16[] BE,
one per display bin, our requested n_disp_bins), 18=f_peak, 20=peak, 21=pitch. `beat` (17) needs
offline analysis and is skipped. The GUI's Visualizer component (ported from Plum-Snapcast) wants a
`Uint8Array` of 0-255 bins, so spectrum bins are scaled uint16→uint8 — a REWIRE of its data source
from WebAudio FFT to these frames, not a rebuild.

**DONE + hardware-verified (2026-07-23, `9114149`):** the full-screen blob is spectrum-reactive off
the native role, no browser audio. Two hardware-only bugs fixed: the canvas must read frames INSIDE
its render loop (a ref), not via React state (per-frame setState canceled the render rAF → blank
canvas); and `calculateBarHeights` (built for a raw linear FFT) sliced our already-log-binned
spectrum wrong → invisible — replaced with a direct pre-binned-spectrum→bars mapping.

**Boundary (the model, not a limitation to fix):** the visualizer role is per-SOURCE, computed by
the group's server. So it follows whatever source the local player is on — OUR source or a PEER's,
both spec-native and working. Audio a foreign server (MA) renders to our player has no source on any
Sendspin server we can read (MA isolates a connecting controller in its own solo group), so it
cannot be visualized. That is correct: "what the speaker plays" is visualizable exactly when some
Sendspin server computes a visualizer role for that group.

Full conformance status: **docs/SPEC-CONFORMANCE.md**. Test strategy: **docs/TESTING.md**.

**KNOWN INTEROP GAP — client-side arbitration.** The spec's multi-server rules are client-side: on a
second server connecting, a client accepts the handshake, compares `connection_reason`
(`playback` beats `discovery`), breaks ties with the persistently stored `server_id` of the last
server that had `playback_state: playing`, and sends `client/goodbye` reason `another_server` to the
loser. Our player implements only the first branch — it always yields to the newest dialer — and
persists nothing. Harmless in a Plum-only mesh (we only ever dial `playback`, having removed the
DISCOVERY tier per §2), but a foreign server running a discovery sweep would take a playing speaker
off us. Not locally fixable: `SendspinClient.server_info` exposes `connection_reason` only after
`attach_websocket`, which refuses a second socket — "accept both, then decide" needs an aiosendspin
change. Track as an upstream item.

**MULTI-ENDPOINT AIRPLAY + PER-UNIT GUI (2026-07).**

*AirPlay* is now config-driven and multi-instance on the same manager pattern: N endpoints, each with
its own shairport-sync process, RAOP/UDP port block, FIFO, metadata pipe and Sendspin source. The
one non-obvious part is transport: shairport's MPRIS bus name is FIXED, so N instances on the system
bus fight over it and only the first gets play/pause/next/previous. Each endpoint therefore runs a
private `dbus-daemon --session` and starts shairport with `mpris_service_bus = "session"` pointed at
it; `AirplayRemote` connects by bus address. Hardware-verified: both endpoints own MPRIS unopposed.
`sources/source_manager.py` holds the shared machinery (poll/reconcile/daemon supervision, N daemons
per endpoint started in order, killed in reverse, respawned as a set since they are interdependent);
Spotify and AirPlay are thin subclasses and DLNA should be a third. Nothing source-shaped is started
from env any more — the local player attaches to `PLUM_LOCAL_PLAYER_SOURCE` (default `airplay-1`)
once its manager brings it up.

*Idle is announced, not inferred.* A source's stream exists ONLY while a sender is feeding it:
first audio → `group.start_stream()` (playback_state=**playing**); EOF on the FIFO (session end) or
silence past `PLUM_SOURCE_IDLE_TIMEOUT` (default 5 min) → `SendspinGroup.stop()`, which is the spec's
way to say "not playing" — it sets playback_state=**stopped**, pushes a `group/update` to every
client, and freezes the metadata progress anchor. (`stop_stream()` deliberately does NOT: it keeps
clients logically PLAYING across a stream-to-stream handover.) The spec has no separate
"idle"/"unrouted" state — `stopped` is it. The group and its anchor client persist through all of
this, so players stay routed to an idle source and the next session simply starts a new stream.
`SourceState.active` in the mesh view mirrors what we announce; it is not a second opinion.

*Per-unit GUI*: nginx (in the container, under supervisord — Plum-Audio is one container per unit,
not app + frontend) serves the built React app from `/app/www` and proxies `/api/mesh` → :5001 and
`/api/{settings,integrations,audio,playback}` → :5002. Same-origin, so no CORS, no dev proxy, and no
`VITE_*` host baked in: one build artifact serves every unit. The controller WS is NOT proxied — the
GUI opens one per source directly at `ws://<unit>:8927`, peers included, which nginx here could
never front. Tailwind is compiled into the bundle (it was a CDN script — dev-only JIT and an
internet dependency at page load, untenable on an isolated AV VLAN). `.env.production` pins the API
URLs empty: Vite inlines env at BUILD time, so a dev `.env` pointing at one unit gets baked into the
artifact and every unit's page then renders that unit.

*The page is the unit it is served by.* `/api/mesh/view` carries `local_unit_id` (the view is
identical from every unit, so identity can only come from the responder), and a player is "ours" by
its listener host rather than by which unit currently reports it — so it stays ours after a roam.
The left card is always us (our name, our player, our source); the right card is everyone else and
never ourselves. The source picker is a CONTROL: choosing a source routes this unit's player there
along with everyone sharing its group, so a synced room moves together. Only sources with a sender
on them are listed; idle ones drop out and their devices read as idle, while staying routable.

**GUI POLISH PASS (2026-08-05, `8dfaca2`..`6a64b88`).** Four defects found in the first real visual
review, three of which are worth recording because the reasoning is not recoverable from the code:

- *Ghost sources.* The per-device pickers were handed the full stream set on the theory that a peer
  parked on an idle source must stay reachable. That theory was wrong about its own code —
  `viewClients` already reports such a peer as idle — and the cost was every configured endpoint
  appearing in every device's picker forever, minutes after the sender left. Device lists now take
  the same active-only truth the top picker uses. Routing to an idle source is legal and silent,
  which is exactly why the stale entries read as ghosts rather than as errors.
- *Idle devices could not be routed from the GUI at all.* They had only "Join Stream", which
  requires a stream the current page is on — so an idle unit's page controlled nothing. The router
  had supported this the whole time (`route_player` reclaims a groupless player via the listener URL
  in its own unit's self-report, and delegates when the source is a peer's); only the UI was
  missing. Every device row now carries the same picker.
- *A speaker renamed itself on every join and leave.* Attached it is named by the Sendspin
  handshake; idle it is named by mDNS, and a third-party device usually publishes no `name` TXT key,
  so the same Voice PE read as "Home Assistant Voice PE - 01" or "home-assistant-voice-a1b2c3"
  depending on whether it was playing. The two views share exactly one identifier — the listener
  URL, not the client id (mDNS names by instance, the handshake by MAC; the same asymmetry that
  makes `adopt` match by URL). The protocol name is memoised against that URL and persisted, since
  "idle at page load" is the common case.

Also: the output picker moved to its own **Audio** tab (Playback is where audio comes *from*; Audio
is where it comes *out* — the Plum-Snapcast split, restored), Settings opens on `tabs[0]` rather
than a hardcoded default that had drifted, and scrollbars are themed. See CLAUDE.md for why themed
scrollbars need `color-scheme` *and* `::-webkit-scrollbar`, and why the two must not be combined
with `scrollbar-color`.

**BLUETOOTH: THE POSITION/SEEK CEILING IS IN `bluetoothd`, AND WE PATCH IT (2026-07-29).**

The A2DP slice relays BlueZ's `org.bluez.MediaPlayer1` into the metadata/artwork roles
(`sources/bluetooth_avrcp.py`) — A2DP itself carries no metadata or position, so everything but the
audio comes over AVRCP. Three separate hunts went looking for "a scrub on the phone never reaches the
timeline" in our relay, the GUI and the metadata role. It is in none of them:

- `avrcp.c: avrcp_register_notification()` registers `EVENT_PLAYBACK_POS_CHANGED` with an interval of
  `UINT32_MAX / 1000` — **49.7 days** — "as we only use it to resync". AVRCP 1.5 §6.7.2 trigger
  condition 1 (registered interval reached) therefore never fires, leaving play-status change, track
  change and end/start of track: exactly the "position only ever arrives bundled with something else"
  pattern observed on hardware. It also kills **seek** detection, because targets size their
  jump-detection window from that same interval (AOSP notifies when the position leaves
  `[pos ± interval]`), so an in-track scrub reads as no change at all.
- There is no fallback. `GetPlayStatus` (PDU 0x30, the only *measured* position) is issued only from
  the GetCapabilities response, a status change, a track change and the media-player-list parse, and
  no D-Bus method triggers one. `MediaPlayer1.Position` is a local wall-clock interpolation from the
  last notification, unclamped — hence 454520 ms reported on a 400346 ms track.

`backend/config/bluez/` therefore carries two DEP-3 patches and `install_patched_bluez.sh`, which
rebuilds the **distro** source package for the version already installed (so Raspberry Pi's `+rptN`
patches are kept) at `<version>+plumN`, installs the `bluez` binary package and holds it. Patch 1
polls `GetPlayStatus` every 2 s while the player is playing — it works even against a target that
never advertises event 0x05, which iOS commonly does not, and it also corrects the interpolation
drift. Patch 2 registers position-changed with a 1 s interval, restoring 1 Hz push ticks and ~1 s
jump detection on targets that *do* advertise 0x05; it is last in series because it is the droppable
half. This is **host provisioning**, in the same class as the rfkill unblock and the D-Bus policy: the
host owns the radio and the AVCTP channel, so a second `bluetoothd` in the container would only fight
it for hci0. Nothing in our Python depends on the patch — `_apply_position_signal` compares an
incoming position against our own anchor, so extra re-reads are discarded and an unpatched unit
behaves exactly as before.

Still genuinely impossible: **absolute** seek toward the phone. AVRCP has no such command at any
version — only press-and-hold FF/REW (`MediaPlayer1.Hold(0x49/0x48)` + `Release()`), which is a
coarse seek we do not currently expose.

**BLUETOOTH ALBUM ART — three silent failure modes (2026-07-30).** Cover art rides a separate OBEX
(BIP) conversation: `MediaPlayer1.ObexPort` is the phone's L2CAP PSM, `Track.ImgHandle` names the
image, and `sources/bluetooth_coverart.py` fetches it over a private per-endpoint obexd. Every way
this breaks looks identical from outside — a stale image (or none) with **nothing in the log**,
because nothing was ever attempted. "No art and no errors" means *we never asked*:

1. *A replaced bluetoothd gives no teardown event.* Its objects do not depart with
   `InterfacesRemoved`; the service vanishes. So the relay sees no "player gone", the cached session
   path is never invalidated, and `prepare()` early-returns forever. The rebuild is therefore keyed
   off the player **BIND** — the one event that reliably follows any disruption — and only one
   rebuild runs at a time, since binds arrive in bursts.
2. *`ObexPort` is never signalled.* BlueZ fills it from the AVRCP SDP record, which routinely lands
   after the player is exported, and `media_player_set_obex_port()` is the one setter in `player.c`
   with no `g_dbus_emit_property_changed`. `obexport_exists()` also hides it while 0, so an early
   `GetAll` shows no key and no signal follows — it has to be polled after a bind.
3. *Our own obexd starts after the bind* (10 s, on `.201.113`). Losing that race is permanent, not
   transient, so `prepare()` is retried for ~30 s.

Underneath all three: **a phone publishes `ImgHandle` only while a BIP session exists** — no session,
no handle, no fetch, no session. And two device-side facts worth not re-deriving: the handle is **not
a track identity** (iOS reuses one value, so fetches are keyed on handle *plus* track), and a phone
serves **one BIP session at a time**, so the distro's D-Bus-activated user `obexd` steals the channel
and ours is refused with `ECONNREFUSED` — mask `obex.service`, as we already disable
`bluealsa-aplay.service`.

*Stale art is worse than none*: a track change with no art clears the artwork role rather than
leaving the last album's cover under the new title, after a short grace period (handles arrive late,
and BlueZ re-sends partial `Track` dicts). The GUI defaults `albumArtUrl` to an inline SVG
placeholder for the same reason — an empty `src` drew the browser's broken-image glyph on every
reconnect and hard refresh.

**Known gap:** art cannot appear for the track already playing when a session opens. BlueZ issues
`GetElementAttributes` (which does request cover art) only on a *track change*, and no D-Bus method
triggers a re-query, so the phone is never re-asked once the session exists. Art therefore lands on
the first track change. Closing it needs a third bluetoothd patch exposing a metadata re-query —
the same shape as the position pair above.

**CONTAINER BUILD DONE (2026-07-31).** `backend/Dockerfile` + `docker/{build,deploy}.sh` +
`docker/units.conf`. All three R&D Pis now run the unit as a container; the `~/plum-test` dev stack
is stopped on each (left on disk, so reverting is `docker compose down` + `run_*.sh`).

*The base image is pinned to the units' Debian release, not "some slim base".* Two integrations
depend on exactly what **trixie** ships: bluez-alsa 4.3.1-3 still installs its daemon as `bluealsa`
(upstream renamed it `bluealsad` in 4.0), and shairport-sync **4.3.7** is the build whose MPRIS
behaviour multi-endpoint AirPlay was verified against. Bookworm would silently hand us shairport 4.1
and a different bluez-alsa. The Dockerfile therefore asserts `shairport-sync -V | grep -- -mpris`
plus the `bluealsa`/`obexd` paths **at build time** — each of those failures is invisible at runtime
(transport controls that do nothing; a Bluetooth source that never appears; cover art that never
loads), so the build is the only place they can be caught cheaply.

*What stays on the host, and why the split is not arbitrary:* Avahi, the system D-Bus and
`bluetoothd` (with the AVRCP patches) run on the host and are reached through mounted sockets under
host networking. The host owns the radio, the AVCTP channel and the mDNS responder — a second
responder in the container is the exact collision `start_server(advertise_addresses=[])` exists to
avoid. Verified on `.201.133`: shairport in the container advertises `_raop._tcp` through the host
Avahi (`6E1713303D0B@Plum Audio` on :5050), both go-librespot endpoints register Spotify Connect,
and the mesh publishes `_sendspin-server._tcp` while discovering its peer.

*Three conflicts the cutover has to clear, all of which fail deceptively:*

1. **The host's nginx served the pre-container GUI** (`/var/www/plum-audio`, the same proxy config
   the image now ships). Under host networking the container's nginx crash-loops on `bind()` while
   the host keeps answering :80 — so the GUI looks *fine* and serves a stale build. `deploy.sh`
   disables the host unit and treats a bound port as a hard failure rather than a warning.
2. **A `SIGTERM`-deaf shairport.** Once its private session bus is killed, shairport-sync traps
   SIGTERM and hangs in shutdown: `pkill` reports success, the process survives, and it keeps RAOP
   port 5050. Endpoint ports are configurable so the port sweep cannot enumerate them — the deploy
   escalates every dev-stack pattern to `SIGKILL` unconditionally.
3. **Identity defaults.** `sendspin_server.py` defaults to `unit-local` and a `127.0.0.1` player
   URL, which are wrong for every unit in a mesh: peers *reclaim this player by the URL it
   registers*, so a loopback default advertises an endpoint no peer can reach and the roam silently
   never lands. The entrypoint derives the unit id from the hostname and the player URL from the
   real LAN address; `units.conf` pins the ids the rig already uses, so a unit keeps its routes.

*Compose reaches the units two ways and that is fine:* `.7.122` runs Docker CE from Docker's repo
(compose as a CLI plugin), the Debian-packaged units run trixie's `docker-compose` 2.26 standalone.
Same compose file; `deploy.sh` detects the invocation. Note trixie has **no** `docker-compose-v2`
package — the name is `docker-compose`, and it is v2, not the retired Python v1.

*Still outside the image:* DLNA (gmrender) and Plexamp, which land with their slices.

### Phase 4 — Cutover
14. Migrate the two production units (`.200`/`.203`) once ≥3-unit soak passes; freeze the
    Snapcast codebase on a tag for rollback.

---

## 9. Risk register (updated)

| Risk | Sev | Notes |
|---|---|---|
| ~~Audible gap on cross-server reclaim~~ | **RESOLVED** | Measured 2026-07-14: **no audible gap.** The player never flushes on a roam, so its ~300 ms jitter buffer drains through the ~25–55 ms reconnect — emitted-silence counter unchanged. The DISCOVERY-pre-connect mitigation was neither possible nor needed (§2). |
| `aiosendspin` pin drift | Med | Fast-moving. Pin 6.0.5; smoke test; track releases. |
| mDNS 5353 collision with our Avahi | Low | Known; disable server mDNS, drive by URL. |
| Player maturity on `hw:<card>` (PortAudio on Pi) | Med | Validate the `sendspin` player + latency mapping on hardware (Phase 1). |
| Opus encode CPU on constrained units | Med | Prefer pcm/flac for LAN players; opus for remote/constrained only. |
| Browser player (`sendspin-js`) maturity | Med | Defer to Phase 3; browser audio is non-core. |
| Losing federation edge-cases we already solved (dedup, remote naming, stale streams) | Med | Port the *intent* of `federation/api.py` dedup/display logic into the orchestrator. |

---

## 10. Open questions
1. **Player: bundled `sendspin` CLI vs. programmatic `SendspinClient(PLAYER)`** in our own
   process? Programmatic gives tighter control (latency, device, reclaim events) — lean that way.
2. **Is `playback_api.py` still needed** once position/metadata are native roles? Likely
   retire; confirm the roles carry position.
3. **Do we keep browser audio at all**, or is it a nice-to-have we drop for v1?
4. **Group persistence** across restarts — do routes survive a unit reboot (re-establish
   routes on boot)?
5. **Naming**: server_id / player_id scheme for stable identity across reboots and IP changes.

---

## 11. Sources
- `aiosendspin` 6.0.5 (installed, audited): `server/{server,group,push_stream}.py`, `client/client.py`, `models/types.py`.
- Music Assistant Sendspin provider (reference for the servers-dial-players / reclaim topology).
- Prior: `docs/SENDSPIN-INTEGRATION-PLAN.md` (dual-engine, superseded), `_resources/Research/sendspin-protocol-research.md`.
