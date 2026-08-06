# CLAUDE.md — Plum-Audio

> **Purpose**: project memory for Claude Code — the rules that stop an agent breaking things.
> **Status**: Phase 3, on `feature/phase3-sources-gui`. Phase 2 (mesh) is merged to `main`.
> AirPlay/Spotify/Bluetooth, the mesh, interop, the container and output selection are all
> hardware-validated on four units. Remaining: DLNA + Plexamp (no backend yet) and the gaps in
> `docs/SPEC-CONFORMANCE.md`. What landed when → `docs/PHASE-HISTORY.md`.
>
> **Not ours, do not re-investigate:** a Home Assistant Voice PE joins a group, ACKs our
> `stream/start` codec header, reports PLAYING — and renders nothing. It does the same from **Music
> Assistant**, under FLAC and PCM. Device-side. Play from MA first before blaming us.

## What this is

Multi-room audio streaming, successor to **Plum-Snapcast**. Replaces the Snapcast server + custom
federation backbone **entirely** with **Sendspin** (Open Home Foundation sync protocol, WebSocket
transport, `aiosendspin` server library) as the **sole** sync engine. Source integrations and the
React/TS GUI were **ported** from Plum-Snapcast, not rewritten.

- **Mesh multi-room**: every unit runs a Sendspin server (local ingest) *and* a roamable player.
  Cross-route any source to any set of endpoints; multiple concurrent groups.
- **Metadata/artwork/visualizer out-of-band** via Sendspin roles — this structurally eliminates the
  Snapcast `onResync()` storm that Plum-Snapcast fought with five control-script guards.
- Multi-instance sources, per-client volume, React GUI with visualizer and album-art theming.

Solo developer + AI assistance. Priority: correct mesh + audio reliability first.

## Working preferences

- **Model**: Sonnet (daily) / Opus (architecture). **Planning**: complex multi-step tasks only.
- **Communication**: concise — explain major changes, skip obvious details.
- **Testing**: manual integration testing on the Pi rig; headless protocol/xrun probes where possible.

## Stack and ports

**Backend** — Python 3.13 · `aiosendspin` **pinned 6.0.5** · PyAV · numpy · Flask (:5002) + aiohttp
(:5001) · supervisord · Avahi + D-Bus + host networking.
Base image **`python:3.13-slim-trixie`** — glibc, not Alpine (deliberate: trivial PyAV/PortAudio/
numpy wheels). **Trixie specifically** to match the units' Debian 13: bluez-alsa still names its
daemon `bluealsa`, and shairport-sync is the MPRIS build. The Dockerfile asserts the build-time
dependencies because each failure is invisible at runtime. Built arm64 only; amd64 never built.

**Frontend** — React 19, TypeScript 5, Vite 6. Sendspin controller-role WS client + engine-agnostic
data service. ColorThief, react-colorful. Tailwind compiled in, no CDN.

| Port | What |
|---|---|
| 80 | nginx per unit — serves the built app, proxies both APIs same-origin |
| 8927 / 8928 | Sendspin server / player — all audio, sync, metadata, transport |
| 8929 | mesh discovery beacon (UDP broadcast) |
| 5001 | mesh API (aiohttp, **in the audio event loop** — topology, route, volume) |
| 5002 | config API (Flask — settings, integrations, audio) |
| 5050+ | AirPlay RAOP (UDP blocks from 6001, stride 10) · Spotify zeroconf 5354+, control 3678+ |
| 5353 | mDNS — the **host's** Avahi, not ours |

**DLNA/Plexamp have no backend.** Only a settings stub and a GUI card exist; no ports are in use.
**Requirements**: layer-2 network for mDNS, host networking mode.

## Repo map

```
_resources/            # dev references, NEVER in git; spike/ holds mesh probes
docs/                  # everything except README.md — see the table in README
backend/
  Dockerfile           # multi-stage, python:3.13-slim-trixie, build-time dep assertions
  entrypoint.sh        # derives the unit identity
  nginx/               # the per-unit GUI server config
  config/              # daemon config templates + bluez/ patches + D-Bus policies
  scripts/
    sendspin_server.py # in-process SendspinServer + PushStream feeders
    sendspin_player.py # the roamable render endpoint
    lifecycle.py       # SIGTERM handling for both audio processes
    audio_devices.py · player_state.py · unit_identity.py
    sync_engine/       # engine seam (base + sendspin impl)
    mesh/              # orchestrator, discovery, aggregator, router, follow, neighbourhood, avahi, api
    sources/           # per-integration config/manager/metadata + shared config_render, artwork
    apis/              # settings/integrations/audio Flask blueprints (mesh API is mesh/api.py)
  supervisord/         # four programs: sendspin_server, sendspin_player, config-api, nginx
scripts/host-setup/    # configure-audio-hat.sh — runs on the HOST
docker/                # compose + build.sh/deploy.sh + units.conf (the rig's unit table)
tests/{Unit,Integration}/
```

## Core architecture (detail: `docs/ARCHITECTURE.md`)

**Mesh model — "servers stay, players roam."** Cross-routing moves *players*, never bridges audio
between servers (that would need the unmerged `Roles.SOURCE`). Two tiers:
1. **Intra-server** re-route → live `group.add_client`/`remove_client`, **plus
   `feeder.refresh_stream()`** — see the stream-membership rule below.
2. **Cross-server** roam → `reclaim_client_for_playback` + `GoodbyeReason.ANOTHER_SERVER`.

A roam is inaudible: the player never flushes, so its ~300 ms jitter buffer drains through the
~25-55 ms reconnect. **There is no DISCOVERY pre-connect** — a client holds one websocket, so a
playing player cannot be warmed on a second server, and a DISCOVERY dial would steal it. Refuted on
hardware; do not reintroduce it.

```
Source (AirPlay/BT/Spotify) → daemon → /tmp/<source>-<id>-fifo
  → PushStream feeder → in-process SendspinServer (group/stream)
  → Sendspin players (local hw:<card> + roamed remote players)
Metadata/artwork/visualizer → Sendspin roles (out-of-band, NOT on the audio stream)
```

## Conventions

- **Git**: `main` protected. Branches `feature/*`, `bugfix/*`, `docs/*`, `refactor/*`. Conventional
  Commits, atomic, `git pull --rebase`. Never force-push main.
- **Python**: `ruff` + `black`, 4-space, `snake_case.py` modules/functions, `PascalCase` classes
  (PEP 8 overrides the house PascalCase-files rule). **TS**: ESLint + Prettier, 2-space,
  `PascalCase.tsx` components, `camelCase.ts` services. Constants/env `UPPER_SNAKE_CASE`. 120 cols.
- **Docker**: Binhex conventions (see the global CLAUDE.md). Project deltas: host networking, and
  `/proc/asound` bind-mounted from the host at `/host/asound` because it is masked in the container.
  `/media` is mounted and declared but nothing reads it yet — it is there for Plexamp.

### Principles
1. Keep it simple. 2. **Audio reliability first — never compromise the pipeline.** 3. Document the
*why*. 4. Fail gracefully. 5. Test on hardware.

## Project-specific rules — the ones that break things

The *reasoning* behind these, and the failures that produced them, is in
**`docs/HARD-WON-LESSONS.md`**. Do not re-litigate them from first principles.

- **Pin `aiosendspin`** (6.0.5). On any bump run `_resources/spike/mesh_smoke.py` first, and re-check
  `docs/UPSTREAM-AIOSENDSPIN.md` — several shipped workarounds should be deleted when it moves.
- **`SendspinServer` always binds mDNS (5353)** → collides with the host Avahi. Start with
  `start_server(advertise_addresses=[], discover_clients=False)` and drive connections by URL.
- **Sendspin mDNS goes through the system Avahi** (`mesh/avahi.py`, D-Bus), never our own responder.
  This is what makes us discoverable by Music Assistant — do not disable it. A client picks ONE
  direction: while advertising we are server-dialed and must not dial out.
- **Ingest via in-process `PushStream`** (`prepare_audio` + `commit_audio` + `set_live_source`),
  never the unmerged `Roles.SOURCE`.
- **Metadata off the audio path** — emit to metadata/artwork roles. The five Snapcast resync guards
  are obsolete here; do not port them.
- **Adding a player to a live stream does NOT put it in that stream.** Membership is fixed at
  `start_stream()`. A client that connects while one is live gets it at handshake; one already
  connected and then added to the group does not — it sits in the group, in the GUI, at the right
  volume, and silent, with nothing in any log. `attach_player` therefore calls
  `SourceFeeder.refresh_stream()` after `add_client`. The cost is a brief discontinuity for everyone
  already listening; that is the deliberate trade. **Roaming hides this** (a reconnect gets the
  stream free), so do not "optimise" the refresh away because a roam test passes.
- **Re-dial before adopting a foreign speaker.** `connect_to_client(url)` is a NO-OP when a dial
  registration for that URL already exists, so a second `adopt` silently does nothing and then times
  out reporting "never connected" about a device whose port is plainly open. Identify a speaker by
  its **registered URL**, never by "a client id that was not in the set before".
- **Codec choice belongs to the CLIENT.** `supported_formats` is in priority order and the server
  takes the first match it implements. A player that cannot sustain its own choice renegotiates with
  `stream/request-format`. Do not add a server-side override without a live, proven case — one was
  written and reverted (`f19a428`). A heterogeneous group is normal, not a problem.
- **Announce idle, don't imply it.** On EOF or `PLUM_SOURCE_IDLE_TIMEOUT` silence call
  `group.stop()` (playback_state=**stopped**), never `stop_stream()` (which keeps clients logically
  PLAYING). The spec has no distinct idle state — `stopped` is it. Groups/anchors persist, so
  routing survives.
- **Three volumes, and only two are the protocol's.** *Per-player* and *group* are Sendspin, and the
  library already does the delta-preserving group redistribution — do not fan out per client.
  *Source volume* is the level on the **sending** device (the phone's slider, Spotify Connect); the
  spec has no such concept, so it rides `POST /api/mesh/source-volume` and is driven per source. It
  stacks with the endpoint levels; never conflate them in the GUI. The main card's slider is **this
  unit's own endpoint**, not the group.
- **A player MUST echo back both its level and the output it actually opened** into
  `/data/player_state.json` — not `settings.json`, which a different process owns. `set_volume()`
  only *sends*; the server's view moves solely on `client/state`, of which the library sends exactly
  one, at connect. Skip the echo and every level in the mesh reads 100% forever while the audio is
  demonstrably quieter — it looks like a GUI bug and is not one. The output echo is what lets the
  API report `pending` rather than claiming a switch that never opened.
- **`client/state` must carry `state` at the TOP level**, via
  `sendspin_player.build_client_state_message`. The library's own `send_player_state()` puts it only
  in the deprecated nested `player` object and drops the required field — see UPSTREAM §0. There is
  a canary test; when it fails, delete the workaround.
- **The output device's identity is the ALSA CARD NAME, never `hw:C,D`.** Card numbers move — the
  HiFiBerry on `.7.204` was card 2, then 1, then 2, then 0 across four reboots with config
  unchanged. `settings.json` stores `<card_name>:<device>`; `hw:C,D` is re-derived every scan.
- **Serialise every PortAudio re-init.** `sd._terminate()`/`sd._initialize()` rebuild PROCESS-GLOBAL
  state; two threads doing it at once SIGSEGVs the interpreter with no exception and no traceback.
  Sequential curl cannot reproduce it — **test concurrently**.
- **PortAudio is not ALSA, and availability cannot be probed by opening** — it enumerates *by*
  opening, so a card we hold exclusively vanishes from `query_devices()`. Availability rests on
  three signals that fail in different places; never reduce it to the probe.
- **`settings.json` access must go through `SettingsManager`**, which holds a lock across the whole
  read-modify-write. Flask is `threaded=True`, and the read path answers a damaged file with
  defaults — building a *write* on that reply resets the unit's entire configuration.
- **A device name is not free text.** It is interpolated into shairport's libconfig and
  go-librespot's YAML, then a daemon is respooled — and shairport's `sessioncontrol` runs shell
  commands. Validated at the CRUD boundary, sanitized in `SettingsManager`, escaped at render.
- **A speaker has TWO names**, and which you see depends on where it is: the handshake name while
  attached, the bare mDNS instance name while idle. The **listener URL** is the only identifier both
  views share — the client id is not (mDNS names by instance, the handshake by MAC).
- **Device pickers list ACTIVE sources only.** A source exists for every configured endpoint whether
  or not a sender is feeding it, so the unfiltered list shows ghosts long after the phone
  disconnected. Idle devices are still routable — every device row gets `StreamPickerButton`.
- **The unit's display name comes from `settings.json` `deviceName`**, not `PLUM_UNIT_NAME` — those
  are only what an unnamed unit boots with. A rename applies live to the mesh view and mDNS TXT; the
  Sendspin-level names are fixed at connect and catch up on the next restart, deliberately, because
  restarting the audio process to apply a rename would drop playback.
- **mDNS hostname changes go through Avahi's D-Bus `SetHostName`** on the HOST bus — never by writing
  `/etc/avahi` or restarting a service. Setting the name it already has raises "invalid because
  redundant" (a no-op), and a real change drops the D-Bus connection mid-call, so success surfaces as
  failure. Always reconnect and read the name back.
- **Host provisioning is not optional.** The bluez patches, `bluealsa-plum-dbus.conf` and the HAT
  mixer must be installed on the host by hand — nothing does it automatically, and each absence
  fails silently or catastrophically. See `docs/HOST-PROVISIONING.md`.
- **WiFi/host concerns** (NetworkManager owns `wlan0`) live on the host, not the container.

## Common tasks

### Adding an audio source
Multi-instance sources follow the **source-manager** pattern (`sources/spotify_manager.py` is the
reference). Do NOT bring back render-config-then-`supervisorctl` from the API — that process cannot
reach the audio loop, and the dev rig has no supervisord.
1. Daemon writes PCM → `/tmp/<source>-<id>-fifo`, one daemon per endpoint.
2. `<source>_config.py`: render the daemon config per endpoint; resolve endpoints → instances. Use
   `sources/config_render.py` for escaping and atomic writes.
3. `<source>_manager.py`: poll `settings.json` in the audio loop; reconcile sources + daemon
   processes (**source first**, so the feeder creates the FIFO, then the daemon). Start it in `main()`.
4. `<source>_<proto>.py`: daemon events → metadata/artwork roles; register as the source's transport
   remote. Decode artwork via `sources/artwork.py` — never on the loop.
5. Integrations API endpoint (persistence only); surface the card via `enabledSources`.
6. Deploy to the rig → verify live add/rename/disable/remove → then build the image.

### Build, deploy, debug
`docker/build.sh` then `docker/deploy.sh all`. Full loop, the deceptive failure modes, and the
debugging cookbook are in **`docs/OPERATIONS.md`**.

## Open

1. **DLNA and Plexamp have no backend at all.** Worse than unimplemented: the Integrations tab
   renders a **live DLNA card** that calls `/api/integrations/dlna/*`, and
   `create_integrations_blueprint` registers only airplay/spotify/bluetooth — so every control on
   that card 404s. Either build the slice or hide the card; do not leave it inert.
2. **Four frontend test suites assert nothing about production code** (`NowPlaying`,
   `PlayerControls`, `integrationsService`, `settingsService`). `PlayerControls` now has a real
   counterpart beside it (`PlayerControlsSourceVolume`); the other three do not. See TESTING.md.
3. **`sendspin_server.py` has no unit coverage**, including `refresh_stream` — the regression guard
   for the highest-profile bug in the repo does not exist.
4. **`configure-audio-hat.sh`'s no-`dtoverlay` fallback has never run on real hardware** — unit-tested
   against fixtures only. `--keep-onboard` HAS now run, on `.7.204` (2026-08-05), and had two
   independent bugs that fixtures could not have caught: it left an out-of-block `dtparam=audio=off`
   armed, and omitting `audio=off` is not the same as asking for `audio=on` (the firmware default is
   off). Both fixed and verified across a reboot.
5. **Visualizer, About and the Integrations tab still have no visual review under a live stream.**
   Confirmed live on 2026-08-05: AirPlay/Spotify/Bluetooth end to end, cross-routing in both
   directions with two concurrent groups, the source-volume slider, rename propagation, adopt/release
   of a third-party speaker, and switching output between the HAT and the 3.5 mm jack.
6. **Multi-server arbitration** is a spec MUST we only half-implement — we persist the last playing
   `server_id` but cannot yet decide, pending UPSTREAM §1.
7. **amd64 has never been built.**
8. **The APIs are unauthenticated with blanket CORS** (`CORS(app)`, `Access-Control-Allow-Origin: *`,
   both bound to `0.0.0.0`). The injection chain behind it is closed at three layers, but any page on
   the LAN can still change a unit's settings. Deliberately deferred 2026-08-05: restricting CORS
   needs a rig test, because peers and the GUI both call peer `:5001` cross-origin.
9. **`_primary_source` is set but never cleared** (`sendspin_server.stop_source`). Disable the
   first-created endpoint and `_maybe_group_controller` resolves a dead source id, so a controller
   with no `ctrl:<source>:` hint silently stops being grouped. Found by audit, never reproduced.
10. **A volume change emits two identical `client/state` frames ~2ms apart.** Harmless (it is a full
    report, not a delta) but it means `_publish_render_state` runs twice per command. Seen on the rig
    2026-08-05, not chased.
11. **`bluetooth_adapter._is_audio_source` returns True for `A2DP_SINK_UUID`**, which contradicts the
    comment above it ("we are the sink — a device offering only 110d has nothing to send"). One of
    the two is wrong. Investigate; do not blind-fix.
12. **Three duplications worth real lines**, from the 2026-08-05 audit: `integrationsService.ts`
    (944 → ~250 with the helper that already exists in `audioService.ts`), `IntegrationsTab.tsx`
    (~880 → ~280 with one endpoint-CRUD card), and the three `*_config.py` (312 → ~190 on a shared
    base). None touch the audio path. Also a shared progress/metadata helper for the three source
    handlers — the Spotify timestamp bug was the third implementation of the same plumbing getting it
    wrong, which is the argument for it.

## Resources
- Sendspin spec: <https://www.sendspin-audio.com/spec/> · Org: <https://github.com/Sendspin>
- `aiosendspin`: <https://github.com/Sendspin/aiosendspin>
- Predecessor: the **Plum-Snapcast** repo — this repo implements the design its architecture doc set out.

## Maintaining this file
Update on: architecture changes, new sources, new env vars, new workflows. **Keep it under ~280
lines** — it loads into every session, and it reached 465 by absorbing things that belong elsewhere.
War stories → `docs/HARD-WON-LESSONS.md`. Dated narrative → `docs/PHASE-HISTORY.md`. Procedures →
`docs/OPERATIONS.md`. A rule earns its place here only if an agent would break something without it.
Document *why*, not just *what*.
