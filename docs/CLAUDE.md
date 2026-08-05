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
> disk. **Audio output selection is DONE (2026-08-04)** — discovery, `/api/audio/*`, live apply
> without a restart, and the picker in Settings → Playback, plus
> `scripts/host-setup/configure-audio-hat.sh` for HAT provisioning. Validated on a **fourth unit,
> `.7.204` (HiFiBerry Amp100)**, recommissioned from Plum-Snapcast. Remaining: DLNA/Plexamp and the
> conformance gaps in docs/SPEC-CONFORMANCE.md.
>
> **Not ours, do not re-investigate:** a Home Assistant Voice PE joins a group, acknowledges our
> `stream/start` codec header, reports PLAYING — and plays nothing. It does not play from **Music
> Assistant** either, under FLAC or PCM. Device-side. Chased at length on 2026-08-04.

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
1. **Intra-server** re-route → live `group.add_client` / `remove_client` (no reconnect), **plus
   `feeder.refresh_stream()`** — see the stream-membership rule below.
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
- **Adding a player to a live stream does NOT put it in that stream.** A stream's membership is
  fixed at `start_stream()`. A client that CONNECTS while one is live gets it at handshake; one
  already connected and then added to the group does not — it sits in the group, in the GUI, at the
  right volume, and silent, with nothing in any log. `attach_player` therefore calls
  `SourceFeeder.refresh_stream()` after `add_client`. The cost is a brief discontinuity for
  everyone already listening, which is the deliberate trade. Roaming hides this (a reconnect gets
  the stream free), so only the intra-server path was ever affected — do not "optimise" the
  refresh away because a roam test passes.
- **Re-dial before adopting a foreign speaker.** `connect_to_client(url)` is a NO-OP when the
  server already holds a dial registration for that URL, so a second `adopt` of a speaker that
  went away (rebooted, reclaimed by its own server) silently does nothing and times out reporting
  "never connected" about a device whose port is plainly open. `adopt_foreign_client` cancels the
  existing dial first. Identify the speaker by its **registered URL**, not by "a client id that
  was not in the set before" — that only holds the first time, and the GUI passes an mDNS name
  while the handshake id is a MAC.
- **Codec choice belongs to the CLIENT.** The spec says `supported_formats` is in priority order,
  first preferred, and the server takes the first match it implements — aiosendspin does exactly
  that, and a player that cannot sustain its own choice is meant to renegotiate with
  `stream/request-format`. Do not add a server-side override without a live, proven case: one was
  written and reverted (`f19a428`) after the device it was for turned out not to play from Music
  Assistant either. Per-client encoding means a heterogeneous group is normal, not a problem.
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
- **The output device's identity is the ALSA CARD NAME, never `hw:C,D`.** Card numbers move: the
  HiFiBerry on `.7.204` was card 2, then 1, then 2, then 0 across four reboots with config
  unchanged. `settings.json` stores `<card_name>:<device>` (`sndrpihifiberry:0`) and `hw:C,D` is
  re-derived every scan for display and `speaker-test` only. Snapcast persisted the number and got
  away with it because `get-settings.py` translated to `default:CARD=<name>` at launch — but from
  the STALE number, so a reboot that renumbered would have resolved to the wrong card.
- **PortAudio is not ALSA, and availability cannot be probed by opening.** `sounddevice`'s
  `device=` matches a substring of PortAudio's OWN name list, so an arbitrary ALSA PCM string is
  rejected; the `(hw:C,D)` suffix PortAudio embeds is the join between the two. And PortAudio
  ENUMERATES BY OPENING, so a card held exclusively vanishes from `query_devices()` — with our
  player holding the Amp100's single-subdevice pcm512x the output list came back EMPTY.
  Availability therefore rests on three signals that fail in different places: `is_active` (what
  we are configured to render to — survives everywhere), `in_use` (`/proc/asound/.../sub*/status`),
  and PortAudio exposure. Never reduce it to the probe.
- **Serialise every PortAudio re-init.** `sd._terminate()`/`sd._initialize()` rebuild PROCESS-GLOBAL
  state; two threads doing it at once SIGSEGVs the interpreter — no exception, no traceback. The
  GUI fetches the device list and current output in one `Promise.all`, Flask is `threaded=True`,
  and the config API crash-looped on exactly that. `_portaudio_outputs` holds a module lock and a
  2 s TTL cache; the player passes `force=True` because it re-reads right after closing its stream.
  Sequential curl cannot reproduce this — test concurrently.
- **`/proc/asound` is masked in the container** and runc refuses to bind anything back into `/proc`,
  so compose mounts the host copy at `/host/asound` (`PLUM_PROC_ASOUND`). Read from in-container,
  `owner_pid` is 0 (different PID namespace) — only `closed` vs a state block is trustworthy — and
  every subdevice must be checked, not `sub0` (bcm2835 has eight).
- **A player echoes the output it ACTUALLY opened** into `/data/player_state.json` (`output_device`),
  the same contract as the volume echo. The config API compares it against the choice in
  `settings.json` to report `pending`; without it the GUI marks a switch applied the moment it is
  saved, including switches that never opened. A failed switch RESTORES the previous device rather
  than leaving silence (measured: 42 ms, still playing).
- **A HAT's hardware mixer is not at unity** and `alsa-restore` reinstates it every boot — an Amp100
  comes up at `Digital` 163/207, i.e. **-22 dB**, which nothing in Plum-Audio can see because volume
  is software gain in the PortAudio callback. `scripts/host-setup/configure-audio-hat.sh --unity`
  pins and persists it. Snapcast never hit this: snapclient owned the control via
  `--mixer hardware:`. Also: the overlay block must go BEFORE the first existing `dtoverlay=` —
  appending it after `vc4-kms-v3d` costs an HDMI audio output (measured over 5 boots).
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
- **`backend/config/bluealsa-plum-dbus.conf` must be installed on the host** at
  `/etc/dbus-1/system.d/`, or `bluealsa` cannot acquire `org.bluealsa`, exits `rc=1` ~3 s after
  every start, and the source manager respawns it forever — on `.7.204` that was 178 restarts, a new
  dbus-daemon every 9.5 s, and enough log spam to bury unrelated diagnosis. It is in the repo but
  nothing installs it automatically; a new unit needs it copied by hand alongside the bluez patches.

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

**Protocol-level debugging.** `PLUM_LOG_LEVEL=DEBUG` in `/opt/plum-audio/plum-audio.env` +
`docker compose up -d --force-recreate` turns on the aiosendspin handshake trace — `client/hello`
with the client's `supported_formats`, the `stream/start` we answer with, and periodic
`Send summary role=player ... buf_ms(...)`. That is the only way to see what a third-party device
actually negotiated. **Revert it afterwards**: it is verbose, and the force-recreate drops every
connection — doing that mid-test once produced a "bug" that was purely the restart.

**Watch what you conclude from a `tail`.** A crash-looping source (see the bluealsa D-Bus policy
above) writes fast enough to push the lines you need thousands back, and a filtered tail then reads
as "this never happened". Two wrong diagnoses on 2026-08-04 came from exactly that.

**Checking output devices from outside the container:**
```bash
docker exec plum-audio python3 /app/scripts/audio_devices.py    # id / hw_id / availability / active
cat /proc/asound/cards                                          # on the HOST — card numbers move
amixer -c <n> sget Digital                                      # a HAT at -22 dB is the default
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

**Changed in the port (don't copy the old shape):** spotifyd → **go-librespot** (0.4.x dropped MPRIS);
per-source supervisord programs + API-driven respool → the **source manager** reconciling from
`settings.json`; one controller WS per unit → **one per source** (`ctrl:<source_id>:` client id);
separate frontend container → **nginx inside the unit container**; Tailwind CDN → compiled in.

---

## Open — carried into the next session (as of 2026-08-04)

Ordered by what bites first. Delete an entry when it is genuinely done, not when it is started.

1. **`.201.133` and `.113` are running a build WITHOUT the silent-join fix.** They will put a player
   in a group and play nothing whenever the source is already streaming — the bug that cost most of
   2026-08-04. Only the two VLAN-7 units (`.7.204`, `.7.122`) are current. Fix: rebuild from HEAD
   (do NOT reuse `dist/plum-audio-266e5fe-*` — that tag names a commit since reverted, and while its
   code is equivalent apart from an inert override, the tag lies) and
   `docker/deploy.sh 192.0.2.10 && docker/deploy.sh 192.0.2.11`. **Needs a machine that
   can route to 192.0.2.0/24** — it was unreachable from the dev laptop's network position on
   2026-08-04, which is why it was not done then.
2. **`configure-audio-hat.sh --keep-onboard` and the no-`dtoverlay` fallback have never run on real
   hardware.** Both are unit-tested against config.txt fixtures
   (`tests/Unit/test_configure_audio_hat.py`) and the main path is verified across four reboots on
   `.7.204`, but a unit with no HAT has never been through the script. Exercise before relying on it.
3. **`.7.204` has no 3.5 mm jack** because the HAT block sets `dtparam=audio=off`. That is correct
   and deliberate; re-run with `--keep-onboard` if the jack should be listed alongside the HAT.
4. **`PLUM_SOURCE_IDLE_TIMEOUT` is 300 s**, so a paused AirPlay session keeps its stream for five
   minutes before announcing `stopped`. Reported as "the stream did not die"; it is the configured
   default, not a hang. Lower it if that is the wrong feel.
5. **The GUI has never been visually reviewed beyond the output picker.** The Playback tab was
   confirmed in a browser on 2026-08-04; the rest of Settings has only ever been exercised through
   the API and the bundle.

---

## Resources
- Sendspin spec: https://www.sendspin-audio.com/spec/ · Org: https://github.com/Sendspin
- `aiosendspin`: https://github.com/Sendspin/aiosendspin
- Predecessor: Plum-Snapcast (`docs/PLUM-AUDIO-ARCHITECTURE.md` = the design this repo implements)

---

## Maintaining This File
Update on: major architecture changes, new sources, new env vars, new workflows. Keep
`docs/ARCHITECTURE.md` in sync. Document *why*, not just *what*. Temporary notes → `_resources/`.
