# CLAUDE.md - Plum-Audio

> **Purpose**: Project memory for Claude Code. Defines rules, workflows, and preferences.
> **Status**: **Phase 2 complete + merged to `main` (2026-07-14)** — single-unit AirPlay and the
> full mesh (discovery/aggregation/roam/multi-group/per-player volume) are hardware-validated on
> two Pis, incl. live AirPlay + metadata/artwork + multi-room. **Phase 3 in progress**: remaining
> sources (Spotify/DLNA/Bluetooth/Plexamp) + the React GUI port.

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
- **Base image**: **Debian-slim (glibc)** — NOT Alpine/musl. Deliberate: glibc makes PyAV /
  PortAudio / numpy wheels trivial and removes the Alpine packaging pain Plum-Snapcast had.
  **Do not "optimize" this back to Alpine.** Multi-arch amd64 + arm64.
- **Language**: Python 3.13
- **Sync engine**: `aiosendspin` (**pinned 6.0.5**; fast-moving — pin + smoke-test on bump), PyAV, numpy
- **APIs**: Flask REST (settings, integrations, audio); **mesh API is aiohttp** — it must call
  the async router/aggregator inside the audio event loop, so WSGI Flask (2nd process) doesn't fit
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
├── docker/                # docker-compose.yml + build-and-push.sh
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
  snapshot/view/route/unroute/volume/source) so the GUI ports with minimal change
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
- Sendspin server: 8927 (per unit) · player: 8928 · mesh/HTTP APIs: 5001+
- AirPlay 5050-5059, Spotify 5354-5363, DLNA 49494-49503, mDNS 5353/udp
- Frontend: 3000
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
- **Ingest via in-process `PushStream`** (`prepare_audio` + `commit_audio` + `set_live_source`),
  never the unmerged `Roles.SOURCE`.
- **Metadata off the audio path** — emit to Sendspin metadata/artwork roles, not the stream.
  The five Snapcast resync guards are obsolete here; do not port them.
- **WiFi/host concerns** (NetworkManager owns `wlan0`) live on the host, not the container (as Plum-Snapcast).

---

## Common Tasks

### Adding an audio source
1. Source service writes PCM → `/tmp/<source>-fifo`.
2. Register a `PushStream` feeder in `sendspin_server.py`.
3. Control script emits metadata → Sendspin roles.
4. supervisord config; integrations API endpoint; frontend Settings.
5. Test: build → deploy to RPi → verify.

### Debugging
```bash
docker logs plum-audio
docker exec plum-audio supervisorctl -c /app/supervisord/supervisord.conf tail -f <service>
docker exec plum-audio aplay -l
```

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

---

## Resources
- Sendspin spec: https://www.sendspin-audio.com/spec/ · Org: https://github.com/Sendspin
- `aiosendspin`: https://github.com/Sendspin/aiosendspin
- Predecessor: Plum-Snapcast (`docs/PLUM-AUDIO-ARCHITECTURE.md` = the design this repo implements)

---

## Maintaining This File
Update on: major architecture changes, new sources, new env vars, new workflows. Keep
`docs/ARCHITECTURE.md` in sync. Document *why*, not just *what*. Temporary notes → `_resources/`.
