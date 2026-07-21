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
| `SendspinGroup.add_client()` / `remove_client()` + `_send_group_update_to_clients()` | **Intra-server routing is live control-plane.** Move a player between groups/streams over its existing WS — no reconnect. |
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
- **Volume:** per-player volume via player-role command; group volume = delta-preserving
  redistribution across the group's players (port existing logic).

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
