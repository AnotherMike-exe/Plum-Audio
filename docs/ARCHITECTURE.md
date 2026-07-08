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
| `ConnectionReason.{DISCOVERY, PLAYBACK}` | **Two-tier presence.** Hold a player in a lightweight idle (DISCOVERY) connection; upgrade to PLAYBACK when audio routes to it. This *is* the "pre-connect to a silent group, fast-switch" pattern we hand-built for snapcast auto-switch — here it's a library primitive. |
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
   `ANOTHER_SERVER` → joins target → `add_client` to the group. Held pre-connected in
   `DISCOVERY` when idle so the switch is fast.

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
- Integration services: `shairport-sync`, `spotifyd`, `gmrender-resurrect`, `bluealsa`, Plexamp.
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
| `auto-switch-service.py` (slave fast-switch) | `ConnectionReason.DISCOVERY→PLAYBACK` + `reclaim` (library primitive) |
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
  Held in `DISCOVERY` by its home server when idle; reclaimed to whichever server is routing
  to it. Latency knob → `static_delay_ms`.
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
  `add_client`. Removing = `remove_client` (and drop to DISCOVERY on its home server).
- **Contention is defined:** a player can only render one group; routing it elsewhere pulls
  it (that's the `ANOTHER_SERVER` handoff). The GUI shows current group membership per player.
- **Volume:** per-player volume via player-role command; group volume = delta-preserving
  redistribution across the group's players (port existing logic).

---

## 7. Mesh verification — spike status

- **API/design tier — DONE (this session).** Source audit confirms live intra-server
  re-routing and first-class cross-server reclaim (§2). This was the primary risk.
- **Protocol-timing tier — probe on `.201.133`** (see `_resources/spike/`): run a server +
  programmatic PLAYER client, feed a continuous tone, perform a live group re-route, and
  confirm the player's WS stays connected (no goodbye/reconnect) and measure the
  stream-clear→stream-start gap. Distinguishes "live re-route" from "reconnect" empirically.
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
1. `sendspin_server.py`: in-process server + one PushStream feeder for the AirPlay FIFO.
2. `sendspin_player` supervised service → `hw:<card>` out; latency→`static_delay_ms`.
3. Control-script metadata/artwork → Sendspin roles (retire Snapcast-properties path).
4. Settings/audio APIs adjusted; supervisord configs; mDNS-off + our discovery.
5. **Milestone:** AirPlay → one unit → its own speaker, with metadata/art, no resync storm.

### Phase 2 — Mesh (the differentiator)
6. Mesh orchestrator: peer discovery, state aggregation, routing engine
   (`connect_to_client`/`reclaim`/`add_client`), DISCOVERY-preconnect idle pool.
7. REST surface parity with today's federation API (route, volume, snapshot) for GUI reuse.
8. Multi-concurrent-group model + contention handling.
9. **Milestone:** ingest on unit A, route to A+B+C; second independent group on unit B; smooth handoffs.

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
| Audible gap on cross-server reclaim (ALSA/PortAudio reinit) | **Med** | Design supports it; measure in §7 audible tier. Mitigate with DISCOVERY pre-connect + `static_delay_ms` buffer. |
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
   DISCOVERY pre-connects on boot)?
5. **Naming**: server_id / player_id scheme for stable identity across reboots and IP changes.

---

## 11. Sources
- `aiosendspin` 6.0.5 (installed, audited): `server/{server,group,push_stream}.py`, `client/client.py`, `models/types.py`.
- Music Assistant Sendspin provider (reference for the servers-dial-players / reclaim topology).
- Prior: `docs/SENDSPIN-INTEGRATION-PLAN.md` (dual-engine, superseded), `_resources/Research/sendspin-protocol-research.md`.
