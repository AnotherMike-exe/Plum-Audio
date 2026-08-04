# CLAUDE.md - Plum-Audio

> **Purpose**: Project memory for Claude Code. Defines rules, workflows, and preferences.
> **Status**: **Phase 2 complete + merged to `main` (2026-07-14)** — single-unit AirPlay and the
> full mesh (discovery/aggregation/roam/multi-group/per-player volume) are hardware-validated on
> two Pis, incl. live AirPlay + metadata/artwork + multi-room. **Phase 3 in progress**
> (`feature/phase3-sources-gui`): settings core + the **Spotify slice are DONE and fully
> hardware-validated** (audio, metadata/artwork, transport, timeline, live endpoint CRUD, roam).
> **Multi-endpoint AirPlay** (private D-Bus session per endpoint for MPRIS), the **per-unit nginx
> GUI**, and **third-party interop** (Sendspin mDNS via Avahi; adopt/release of foreign speakers —
> proven against Music Assistant + a Home Assistant Voice PE) are in and hardware-verified.
> **The container build is DONE (2026-07-31)** — all three R&D Pis run the unit as a container
> (`docker/build.sh` → `docker/deploy.sh`), with the `~/plum-test` dev stack stopped but left on
> disk. Remaining: DLNA/Plexamp and the conformance gaps in docs/SPEC-CONFORMANCE.md.

## Project Overview

**Plum-Audio** is a multi-room audio streaming system and the successor to **Plum-Snapcast**.
It replaces the Snapcast server + custom federation backbone **entirely** with **Sendspin**
(Open Home Foundation sync protocol; WebSocket transport; `aiosendspin` server library) as the
**sole** sync engine. Source integrations (AirPlay/Spotify/DLNA/Bluetooth/Plexamp) and the
React/TS GUI are **ported** from Plum-Snapcast, not rewritten.

### Key Features
- **Mesh multi-room**: every unit runs both a Sendspin server (local ingest) and a roamable
  player. Cross-route any source to any set of endpoints; multiple concurrent groups.
- **Out-of-band metadata/artwork/visualizer** (Sendspin roles) — structurally eliminates the
  Snapcast `onResync()` storm that Plum-Snapcast fought with five control-script guards.
- Multi-instance AirPlay/Spotify/DLNA sources, per-client volume, real-time metadata + album art.
- React web UI, full-screen visualizer, album-art theming (ported).

### Project Context
- **Stage**: Phase 2 (mesh) merged to `main`; Phase 3 (remaining sources + GUI) starting
- **Team**: Solo developer + AI assistance
- **Priority**: Correct mesh + audio reliability first; port UI second

---

## Claude Code Preferences
- **Model**: Sonnet (daily) / Opus (architecture)
- **Planning**: Complex multi-step tasks only
- **Communication**: Concise — explain major changes, skip obvious details
- **Testing**: Manual integration testing on Raspberry Pi hardware; headless protocol/xrun probes where possible

---

## Technology Stack

### Backend
- **Base image**: **`python:3.13-slim-trixie`** (glibc) — NOT Alpine/musl. Deliberate: glibc makes
  PyAV / PortAudio / numpy wheels trivial and removes the Alpine packaging pain Plum-Snapcast had.
  **Do not "optimize" this back to Alpine.** Multi-arch amd64 + arm64.
  **Trixie specifically, matching the units' Debian 13**: bluez-alsa 4.3.1-3 still names its daemon
  `bluealsa` (renamed `bluealsad` upstream in 4.0) and shairport-sync **4.3.7** is the MPRIS build
  multi-endpoint AirPlay was verified against. The Dockerfile asserts both at BUILD time — each
  failure is invisible at runtime (dead transport controls, a Bluetooth source that never appears).
- **Language**: Python 3.13
- **Sync engine**: `aiosendspin` (**pinned 6.0.5**; fast-moving — pin + smoke-test on bump), PyAV, numpy
- **APIs**: Flask REST on **:5002** (settings, integrations, audio); **mesh API is aiohttp on :5001**
  — it must call the async router/aggregator inside the audio event loop, so WSGI Flask (2nd
  process) doesn't fit
- **Audio sources**: shairport-sync (AirPlay), go-librespot (Spotify), gmrender-resurrect (DLNA),
  BlueZ+bluez-alsa (Bluetooth), Plexamp (Debian sidecar)
- **Infra**: supervisord, Avahi (mDNS), D-Bus, host networking

### Frontend
- React 19, TypeScript 5, Vite 6 (ported)
- Sendspin **controller-role** WS client + engine-agnostic data service
- ColorThief (album-art color), react-colorful

---

## Project Structure

```
├── _resources/           # Dev references (NOT in git); spike/ holds mesh probes
├── docs/
│   ├── ARCHITECTURE.md    # Canonical design + phased plan (READ FIRST)
│   ├── SPEC-CONFORMANCE.md # Where we stand against the Sendspin spec (interop is the point)
│   ├── TESTING.md         # Test tiers 1-6 + what is not yet reproducible
│   ├── CLAUDE.md          # This file (symlinked to root)
│   ├── DEV-SETUP.md
│   └── QUICK-REFERENCE.md
├── backend/
│   ├── Dockerfile         # multi-stage, debian-slim
│   ├── requirements.txt   # aiosendspin==6.0.5 pinned
│   ├── config/            # sendspin/shairport/etc configs
│   ├── scripts/
│   │   ├── sendspin_server.py   # in-process SendspinServer + PushStream feeders
│   │   ├── sync_engine/         # engine seam (base + sendspin impl)
│   │   ├── mesh/                # orchestrator: discovery, aggregator, router
│   │   ├── sources/             # per-integration control scripts (metadata→roles)
│   │   └── apis/                # settings/integrations/audio Flask APIs (mesh API lives in mesh/api.py, aiohttp)
│   └── supervisord/       # process .ini configs
├── frontend/src/{components,services,hooks,assets}/
├── docker/                # compose + build.sh/deploy.sh + units.conf (the rig's unit table)
└── tests/{Unit,Integration}/
```

**Special**: `_resources/` NEVER in git. All docs in `docs/` except README.

---

## Core Architecture (summary — full detail in docs/ARCHITECTURE.md)

### Mesh model — "servers stay, players roam"
Every unit runs a Sendspin **server** (owns local audio ingest via in-process `PushStream`)
**and** a **player** (roamable render endpoint). Cross-routing moves *players*, never bridges
audio between servers (avoids the unmerged `Roles.SOURCE` path). Two tiers:
1. **Intra-server** re-route → live `group.add_client` / `remove_client` (no reconnect).
2. **Cross-server** roam → `reclaim_client_for_playback` + `GoodbyeReason.ANOTHER_SERVER`.
Cross-server roam is inaudible: the player never flushes on a roam, so its ~300 ms jitter buffer
drains through the ~25-55 ms reconnect. **There is no DISCOVERY pre-connect** — a client holds one
websocket, so a playing player can't be warmed on a 2nd server (and a DISCOVERY dial would steal it).
Do not reintroduce it; see ARCHITECTURE §2.

### Audio pipeline
```
Source (AirPlay/BT/Spotify/DLNA/Plexamp) → service → /tmp/<source>-fifo
  → PushStream feeder → in-process SendspinServer (group/stream)
  → Sendspin players (local hw:<card> + roamed remote players)
Metadata/artwork/visualizer → Sendspin roles (out-of-band, NOT on the audio stream)
```

### Key design patterns
- **Sendspin WS** (JSON control + binary media) for sync/transport
- **Flask REST** for settings/integrations/audio; **aiohttp** for the mesh API (in the audio
  event loop). The mesh API keeps parity with the old federation REST surface (`/api/mesh/*`:
  snapshot/view/route/unroute/volume/source-volume/source) so the GUI ports with minimal change
- **FIFO** audio transport from source services (single consumer — no tee needed)
- **Dynamic stream lifecycle**: services run continuously; streams created on activity

---

## Development Workflow

### Git
- Main: `main` (protected). Branches: `feature/*`, `bugfix/*`, `docs/*`, `refactor/*`.
- Conventional Commits. `git pull --rebase`. Atomic commits. Never force-push main.

### Code Quality
- Python: `ruff` + `black` (4-space indent, snake_case modules/files). TS: ESLint + Prettier
  (2-space, PascalCase components, camelCase services). Constants/env: `UPPER_SNAKE_CASE`.
- Max 120 cols.

### Naming (per-ecosystem)
- **Backend Python**: `snake_case.py` modules/functions, `PascalCase` classes (PEP 8 — overrides
  the house PascalCase-files rule; Python demands it).
- **Frontend**: `PascalCase.tsx` components, `camelCase.ts` services, `camelCase` vars/functions.
- **Constants / env vars**: `UPPER_SNAKE_CASE`.

---

## Key Ports (planned)
- Web GUI: **80** (nginx, per unit — serves the built app + proxies the APIs)
- Sendspin server: 8927 (per unit) · player: 8928 — **all audio, sync, metadata and transport**
- Mesh API 5001 (aiohttp) · config API 5002 (Flask) — our own surfaces for what the spec does not
  cover (cross-unit topology/routing, settings/integrations). NOT the old Snapcast/federation
  layer: nothing of Snapcast's 1780/1704 or `federation/*` remains, only the port numbering.
- Per-endpoint daemon control APIs on loopback: go-librespot 3678+ (`3678 + id - 1`)
- AirPlay 5050-5059 (+ UDP blocks from 6001, stride 10), Spotify 5354-5363, DLNA 49494-49503,
  mDNS 5353/udp
- **Requirements**: Layer-2 network for mDNS/Avahi, host networking mode

---

## Docker (Binhex conventions)
- Volumes: `/config` (config+logs+db), `/data` (app data), `/media` (media)
- Env: `PUID`, `PGID`, `UMASK` (prefer `002`), `TZ`
- Logs: supervisord → `/config/supervisord.log` (first place to check)
- Build: debian-slim base, multi-stage, deps-before-code, `.dockerignore`

---

## Coding Conventions

### Principles
1. Keep it simple. 2. Audio reliability first — never compromise the pipeline.
3. Document the *why*. 4. Fail gracefully (supervisord auto-restart). 5. Test on hardware.

### Project-specific rules
- **Pin `aiosendspin`** (6.0.5). On any bump, run `_resources/spike/mesh_smoke.py` first.
- **`SendspinServer` always binds mDNS (UDP 5353)** → collides with our Avahi. Start with
  `start_server(advertise_addresses=[], discover_clients=False)`; drive connections by URL.
- **Sendspin mDNS goes through the system Avahi** (`mesh/avahi.py`, D-Bus), never our own responder:
  players advertise `_sendspin._tcp`, servers `_sendspin-server._tcp`. This is what makes us
  discoverable by Music Assistant and other third-party Sendspin servers — do not disable it.
  A client picks ONE direction: while advertising, we are server-dialed and must not dial out.
- **Ingest via in-process `PushStream`** (`prepare_audio` + `commit_audio` + `set_live_source`),
  never the unmerged `Roles.SOURCE`.
- **Metadata off the audio path** — emit to Sendspin metadata/artwork roles, not the stream.
  The five Snapcast resync guards are obsolete here; do not port them.
- **Announce idle, don't imply it** — a stream exists only while a sender feeds the source. On EOF
  or `PLUM_SOURCE_IDLE_TIMEOUT` silence call `group.stop()` (playback_state=**stopped** via
  `group/update`), never `stop_stream()` (which keeps clients logically PLAYING). The spec has no
  distinct idle/unrouted state — `stopped` is it. Groups/anchors persist, so routing survives.
- **Three volumes, and only two of them are the protocol's.** *Per-player* (one endpoint's output)
  and *group* (all endpoints on a source) are Sendspin: player-role command / controller-role
  `volume`+`mute`, and **the library already does the delta-preserving group redistribution** — do
  not fan out per client. *Source volume* is the level on the SENDING device (the phone's
  AirPlay/Bluetooth slider, the Spotify Connect device volume); the spec has no such concept, so it
  rides `POST /api/mesh/source-volume` + `SourceState.source_volume` and is driven per source
  (shairport's **`SetVolume` method** — its MPRIS `Volume` property is read-only, so a
  Properties.Set is refused and looks like success / `MediaTransport1.Volume` / go-librespot
  `/player/volume`). It stacks with the endpoint levels; never conflate the two in the GUI. The main
  card's slider is **this unit's own endpoint**, not the group — the group is moved deliberately,
  via the group buttons.
- **A player MUST echo its level back** (`client/state`) after every volume/mute command, and
  persist it (`/data/player_state.json`, not `settings.json` — different process owns that file).
  `PlayerV1Role.set_volume()` only *sends*; the server's own view moves solely on `client/state`,
  of which the client library sends exactly one, at connect, carrying `initial_volume`. Skip the
  echo and every level in the mesh reads 100% forever while the audio is demonstrably quieter —
  the failure looks like a GUI bug and is not one. See `sendspin_player._publish_render_state`.
- **WiFi/host concerns** (NetworkManager owns `wlan0`) live on the host, not the container (as Plum-Snapcast).
- **The unit's display name comes from `settings.json` `deviceName`** (`scripts/unit_identity.py`),
  NOT from `PLUM_UNIT_NAME`/`PLUM_PLAYER_NAME` — those are only what an unnamed unit boots with.
  A rename applies live (mesh view + mDNS TXT, via `Neighbourhood.rename`/`AvahiClient.republish`);
  the Sendspin-level `server_name`/client name are fixed at connect and catch up on the next
  restart, deliberately — restarting the audio process to apply a rename would drop playback.
- **mDNS hostname changes go through Avahi's D-Bus `SetHostName`** on the HOST bus, never by
  writing `/etc/avahi` or restarting a service (there is no `avahi` program in our supervisord —
  Avahi is the host's). Two verified behaviours the code must handle: setting the name it already
  has raises *"invalid because redundant"* (a no-op, not an error), and a real change makes Avahi
  reset and **drop the D-Bus connection mid-call**, so a successful set surfaces as "recipient
  disconnected" — always reconnect and read the name back rather than trusting the set. It is
  runtime state: a host reboot reverts it, and we do NOT re-apply on boot (every unit ships with
  the same default hostname, so replaying it would collide all units onto one name).
- **Bluetooth needs a PATCHED host `bluetoothd`** — `backend/config/bluez/` (two DEP-3 patches +
  `install_patched_bluez.sh`, which rebuilds the distro package at `<version>+plumN` and holds it).
  Stock BlueZ registers AVRCP position-changed with a 49.7-day interval and never polls
  `GetPlayStatus`, so a scrub on the phone cannot reach us at all. This is **host provisioning, not
  a container concern** — the host owns the radio and the AVCTP channel, exactly like the rfkill
  unblock and the D-Bus policy. Also `systemctl --user mask obex.service`: a phone serves ONE AVRCP
  cover-art session, and the distro's obexd steals it from ours. Nothing in our Python depends on
  the patches; an unpatched unit just loses scrub reporting. See ARCHITECTURE §8 Phase 3.

---

## Common Tasks

### Adding an audio source
Multi-instance sources follow the **source-manager** pattern (`sources/spotify_manager.py` is the
reference; see ARCHITECTURE §8 Phase 3). Do NOT bring back render-config-then-`supervisorctl` from
the API — that process can't reach the audio loop, and the dev rig has no supervisord.
1. Daemon writes PCM → `/tmp/<source>-<id>-fifo` (one daemon per endpoint).
2. `<source>_config.py`: render the daemon config per endpoint; resolve endpoints → instances.
3. `<source>_manager.py`: poll `settings.json` in the audio loop; reconcile sources + daemon
   processes (source FIRST so the feeder creates the FIFO, then the daemon). Start it in `main()`.
4. `<source>_<proto>.py`: daemon events → metadata/artwork roles; register it as the source's
   transport remote.
5. Integrations API endpoint (persistence only); surface the card via `enabledSources` in
   `Settings.tsx`.
6. Test: deploy to the RPi rig → verify live add/rename/disable/remove → then build the image.

### Building + deploying to the rig
```bash
docker/build.sh                    # arm64 image (native on Apple Silicon) -> dist/*.tar.gz
docker/deploy.sh all               # every unit in docker/units.conf
docker/deploy.sh 192.0.2.10   # one unit
```
`deploy.sh` is re-runnable and stops the pre-container `~/plum-test` stack itself. It imports that
unit's existing `settings.json` + go-librespot auth on FIRST deploy only, then never touches
`/opt/plum-audio/{config,data}` again — so a rebuild is not a re-authorisation. Reverting a unit to
the dev stack is `docker compose down` in `/opt/plum-audio` plus `~/plum-test/run_*.sh`, which is
still on disk.

Two conflicts it clears, both of which fail deceptively — see ARCHITECTURE §8 Phase 3: the **host's
nginx** (it answers :80 while the container's nginx crash-loops, serving a stale GUI that looks
fine), and a **SIGTERM-deaf shairport** that survives `pkill` still holding RAOP 5050.

### Debugging
```bash
docker logs plum-audio                                    # entrypoint: the derived unit identity
docker exec plum-audio supervisorctl -c /app/supervisord/supervisord.conf status
docker exec plum-audio tail -f /config/logs/sendspin_server.log   # + sendspin_player/config_api/nginx
docker exec plum-audio tail -f /data/shairport/1/shairport-sync.log   # per-endpoint daemon logs
docker exec plum-audio aplay -l
```
Source daemons are NOT supervisord programs — the managers own them, so `ps` inside the container
is how you confirm shairport/go-librespot/bluealsa are up. A source missing there but enabled in
settings.json is a manager problem, not a supervisord one.

---

## Porting map (from Plum-Snapcast)
| Plum-Snapcast | Plum-Audio |
|---|---|
| `snapserver` / `snapclient` | in-process `SendspinServer` + Sendspin player |
| `federation/*` | `backend/scripts/mesh/*` (same concepts, Sendspin protocol) |
| `auto-switch-service.py` | `reclaim` (no pre-connect needed — roam is inaudible) |
| `*-stream-lifecycle-manager.py` | PushStream feeders via `sync_engine/` |
| AirPlay resync guards | dropped (metadata is out-of-band) |
| `snapcastService.ts` / `snapcastDataService.ts` | Sendspin controller WS client + engine-agnostic data service |
| Browser audio wire protocol | `sendspin-js` (Phase 3, if kept) |

Reused ~as-is: source services, endpoint APIs, control-script metadata extraction, settings/
integrations/audio Flask layer, most GUI components.

**Changed in the port (don't copy the old shape):** spotifyd → **go-librespot** (0.4.x dropped MPRIS);
per-source supervisord programs + API-driven respool → the **source manager** reconciling from
`settings.json`; one controller WS per unit → **one per source** (`ctrl:<source_id>:` client id);
separate frontend container → **nginx inside the unit container**; Tailwind CDN → compiled in.

---

## Resources
- Sendspin spec: https://www.sendspin-audio.com/spec/ · Org: https://github.com/Sendspin
- `aiosendspin`: https://github.com/Sendspin/aiosendspin
- Predecessor: Plum-Snapcast (`docs/PLUM-AUDIO-ARCHITECTURE.md` = the design this repo implements)

---

## Maintaining This File
Update on: major architecture changes, new sources, new env vars, new workflows. Keep
`docs/ARCHITECTURE.md` in sync. Document *why*, not just *what*. Temporary notes → `_resources/`.
