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
- **The player is a PROCESS, not a setting.** `audio.output.device = "none"` (`audio_devices.NO_OUTPUT`)
  means a unit renders nothing and runs **no `sendspin_player` at all** — `output_gate.py` decides
  before supervisord and omits the program file. It cannot be a running player with nothing open:
  `AlsaRenderer.start()` raises when PortAudio can't open a device and `SendspinPlayer.start()` calls
  it *before* the listener and the mDNS publish, so a card-less host crash-loops forever. Hence the
  restart requirement, and hence `has_player` on the snapshot — **defaulting True**, or a peer on an
  older image reads as playerless. A playerless unit **leads** follow but never follows; a leader with
  no `local_player` used to read as "session ended" and unroute its own followers. `find_device` must
  short-circuit the sentinel *before* its substring pass, and the compose `headless` profile exists
  because Docker refuses to create a container whose `devices:` names a missing `/dev/snd`.
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

1. **DLNA and Plexamp have no backend.** Established by *watching the running GUI* on `.7.204`
   (2026-08-06) after two wrong descriptions here — a truncated `grep | head -20` produced the
   second, so **read the whole grep**:
   - `IntegrationsTab.tsx` **does** contain full DLNA (`:1250`) and Plexamp (`:1433`) sections,
     ~330 lines with handlers and CRUD.
   - They do not RENDER: `Settings.tsx:43` passes `enabledSources={['airplay','spotify','bluetooth']}`
     and `show()` gates both out.
   - But `loadDlnaEndpoints()` ran on mount regardless of that gate, so every open of the
     Integrations tab logged two console errors against `/api/integrations/dlna/endpoints` — a route
     `create_integrations_blueprint` does not register. **Fixed 2026-08-06**: the effect now returns
     early when the section is hidden.
   Remaining scaffolding: the two card bodies, `types.ts`'s `DLNAEndpoint`, and `settings_api.py`'s
   `integrations.dlna`/`.plexamp` defaults (Plexamp's gated on `PLEXAMP_ENABLED`).
2. **Four frontend test suites assert nothing about production code** (`NowPlaying`,
   `PlayerControls`, `integrationsService`, `settingsService`). `PlayerControls` now has a real
   counterpart beside it (`PlayerControlsSourceVolume`); the other three do not. See TESTING.md.
3. ~~`sendspin_server.py` has no unit coverage~~ — **done 2026-08-06**,
   `tests/Unit/test_sendspin_server.py` (29 tests). `refresh_stream` and its `attach_player` caller
   are pinned in call order against fakes; deleting the refresh fails two tests. Also covers the
   `_primary_source` handoff, controller grouping and source lifecycle.
4. **`configure-audio-hat.sh`'s no-`dtoverlay` fallback has never run on real hardware** — unit-tested
   against fixtures only. `--keep-onboard` HAS now run, on `.7.204` (2026-08-05), and had two
   independent bugs that fixtures could not have caught: it left an out-of-block `dtparam=audio=off`
   armed, and omitting `audio=off` is not the same as asking for `audio=on` (the firmware default is
   off). Both fixed and verified across a reboot.
5. ~~Visualizer, About and Integrations have no visual review under a live stream~~ — **DONE
   2026-08-06**, in-browser on `.7.204` (idle) and `.201.133` (live Spotify). Everything renders:
   artwork, metadata, progress, both volume sliders, shuffle/repeat shown only because Spotify
   advertises them, and a track change updating metadata + artwork live. The **visualizer is
   audio-reactive under a live stream** — successive frames show different spectra — and **album-art
   theming works**, re-colouring the whole UI from the artwork with contrast preserved. It is
   opt-in: Settings → Theme → *Album Art Colors*, off by default, per-browser. Left OFF as found.
   Two real defects were found and fixed: the About tab was unported from Plum-Snapcast wholesale,
   and the DLNA console errors in item 1. Console is otherwise clean.
6. **Multi-server arbitration** is a spec MUST we only half-implement — we persist the last playing
   `server_id` but cannot yet decide, pending UPSTREAM §1.
7. **amd64 has never been built.**
8. **The APIs are unauthenticated with blanket CORS** (`CORS(app)`, `Access-Control-Allow-Origin: *`,
   both bound to `0.0.0.0`). The injection chain behind it is closed at three layers, but any page on
   the LAN can still change a unit's settings. Deliberately deferred 2026-08-05: restricting CORS
   needs a rig test, because peers and the GUI both call peer `:5001` cross-origin.
9. ~~`_primary_source` is set but never cleared~~ — **fixed 2026-08-06**. Confirmed real by reading:
   `stop_source` popped `sources` and left the id behind, so `_maybe_group_controller` resolved a
   dead source and returned early — a controller with no `ctrl:<source>:` hint silently stopped
   being grouped. It now hands the fallback to the oldest surviving source (`None` when the last one
   goes). Regression-guarded in `test_sendspin_server.py`. Never reproduced on hardware, but the
   read is unambiguous.
10. **A volume change emits two identical `client/state` frames ~2ms apart.** Harmless (it is a full
    report, not a delta) but it means `_publish_render_state` runs twice per command. Seen on the rig
    2026-08-05, not chased.
11. ~~`_is_audio_source` returns True for `A2DP_SINK_UUID`~~ — **resolved 2026-08-06: the comment was
    right, the code was wrong.** 110d (AudioSink) is what a *speaker* advertises; a device offering
    only it cannot send us audio, so adopting it started an `arecord` that could never produce a
    sample and — most-recently-connected wins — took the capture slot from a phone that was already
    playing. The both-match test came in with the original Bluetooth commit (`5d1beb2`), carried
    from Plum-Snapcast. Now requires 110a; a device that advertises both (phones that can also be a
    speaker) is unaffected, and a skipped sink-only device is logged rather than dropped silently.
12. **Three duplications worth real lines**, from the 2026-08-05 audit: `integrationsService.ts`
    (944 → ~250 with the helper that already exists in `audioService.ts`), `IntegrationsTab.tsx`
    (**1490** as of 2026-08-06, not the ~880 first recorded → ~350 with one endpoint-CRUD card), and
    the three `*_config.py` (431 → ~190 on a shared base). None touch the audio path. Also a shared
    progress/metadata helper for the three source handlers — the Spotify timestamp bug was the third
    implementation of the same plumbing getting it wrong, which is the argument for it.
13. **A follower stops following when its leader switches source.** Found 2026-08-06 while building
    headless mode, and **pre-existing** — it is not about playerless units, it happens identically to
    a leader with a speaker (verified directly). When the leader moves to a second source, the
    follower's old source goes quiet, so its `current_target` becomes `None`; the override guard in
    `follow.tick()` reads that as "the user moved us" and sets `_overridden`, so it never follows to
    the new source. Distinguishing "went idle because the source stopped" from "was deliberately
    moved" needs a real decision, so it was pinned by a parity test
    (`test_a_playerless_leader_switching_source_behaves_like_any_other_leader`) rather than
    quietly changed under a feature branch.
14. **A playerless leader cannot nominate which source it leads with.** With several concurrent
    active sources, `follow._leader_status` picks the one with the most endpoints attached,
    tie-broken by `source_id`. Deterministic and self-reinforcing — the first follower to join raises
    that source's count — and it has to be, because every follower computes it independently with no
    coordination. But the leader has no say, and there is no GUI for it.
15. **A playerless unit's main card has NO endpoint slider** (`hideEndpointVolume`, 2026-08-06).
    It previously rendered a phantom 100% whose `onChange` found no client, did nothing, and snapped
    back on the next poll. Hiding it is rule-conformant — *"the main card's slider is this unit's own
    endpoint, not the group"* — and the group control still exists one panel down in `SyncedDevices`.
    Repurposing that slider to group volume on playerless units would be more useful, but it needs
    that rule **amended explicitly**, not silently excepted. Awaiting a call.

16. **Card-identity hardening — what is still open** (audit 2026-08-06; the confirmed-dangerous ones
    are fixed, see HARD-WON-LESSONS). Ranked:
    - ~~A failed output switch is never retried~~ — **fixed 2026-08-06.** `watch_output_device` now
      holds its baseline until `on_change` reports success (False or a raise = retry), so a card
      that is merely late is picked up on the next tick instead of stranding the unit until a human
      toggles the setting. Logging throttles after the first few attempts. Returning None still
      counts as success.
    - ~~`renderer.device` records the REQUEST, not the card actually opened~~ — **fixed 2026-08-06.**
      `AlsaRenderer.open_device` carries the RESOLVED `<card_name>:<device>` and is what is echoed to
      `player_state.json`; `device` still holds the requested spec, so `reopen`'s no-op check is
      unchanged. `pending` can now detect "opened, but on a different card than intended" — exactly
      what a stale `hw:C,D` produces after a renumber. None when resolution found nothing and
      PortAudio name-matched the raw spec: unknown beats invented.
    - **`_open`'s raw-spec fallback can open the wrong card.** When `aplay -l` fails, resolution
      returns nothing and the raw spec goes to PortAudio, whose names embed `(hw:C,D)` — so an
      `hw:2,0` substring-matches whatever is at that address now and opens it, with one warning.
    - **USB card names are enumeration-order-derived.** Two identical DACs give `Device` and
      `Device_1`, and which is which is decided by the same probe race that moves card numbers, so
      `card_name` is NOT stable for exactly the device class where hot-plugging is normal. Passes
      1–3 of `find_device` have no ambiguity guard at all (only the substring pass does).
    - **`_portaudio_outputs` is last-write-wins** on a duplicate `(card, device)` key, and its 2 s
      cache is keyed on that volatile pair — a hotplug inside the window can hand back an index for
      a device that no longer exists. `resolve_portaudio_index` forces a refresh; no `audio_api`
      caller does.
    - **`parse_aplay_output` silently drops any line the regex misses** — the device then vanishes
      everywhere downstream with nothing logged.

17. **Spotify Connect's first transfer after a go-librespot (re)start can fail.** Seen on
    `.201.133` 2026-08-06 — the first attempt drops immediately, the retry works. It is go-librespot
    internal, NOT our pipeline: `/data/go-librespot/<n>/go-librespot.log` shows
    `failed handling dealer request ... failed creating stream ... failed seeking stream: failed
    reading page: EOF`, i.e. it could not fetch the track from Spotify's CDN. The observed instance
    was ~30 s after a container restart. That log also carries a `panic: send on closed channel`
    from an earlier date — a real go-librespot crash, which our source manager respawns. Worth
    watching for a pattern away from restarts before treating it as ours.

18. **The visualizer's periodic drop-to-zero is CONTROLLER-WS CHURN, not the audio path.** Measured
    in the running GUI on `.201.133` (2026-08-06) by hooking `WebSocket` and timestamping every
    binary frame: spectrum arrives at **31 Hz with a 2000 ms gap every 3 s**, like clockwork
    (1.1 s, 4.1 s, 7.1 s, 10.1 s …). In the same window, **48 controller sockets were created AND
    closed in 22 s** — six (one per source across both units) every ~3 s, all close code 1000.
    Ruled OUT: `refresh_stream`. The server re-acquired the stream only 3 times in the whole log,
    each right after a container restart, so steady playback is not churning the group. The player
    logs no xruns and no starvation.
    ~~The amplifier is `sendspinControllerClient.open()`~~ — **fixed 2026-08-06.** `reconnectAttempts`
    was reset in `onopen`, the instant the socket opened rather than once it had proven stable, so a
    socket dying shortly after connecting reset the counter every cycle and retried at a flat 1 s
    forever. Now forgiven only after `RECONNECT_STABLE_MS` (10 s) of survival. This removes the
    AMPLIFIER, not the cause — a real trigger will now present as a visibly SLOWING retry rather
    than a fixed 3-second sawtooth, which is more diagnosable, not less.
    **STATUS 2026-08-06: not reproducing on `e5f1bfc`.** Michael reports it looks good with that
    build deployed, after the localActivity/slave ping-pong fix (#13) and the layout fixes landed.
    That is an observation from watching, NOT a measurement, and the sawtooth above WAS measured on
    the same build — so treat this as "intermittent / trigger-dependent", not "fixed". If it returns,
    start from the WebSocket hook rather than from theory; the recipe is in this entry.
    **The TRIGGER — what closes the socket ~1 s after open — is NOT yet identified.** One strong
    candidate not yet excluded: the measurement tab was backgrounded (confirmed — a 100 ms sampler
    was throttled to ~1 Hz), and `client/time` is sent on an adaptive `setTimeout` (0.2–3 s) which
    background throttling would stretch, possibly past whatever the server tolerates. Re-measure in
    a FOCUSED, foreground tab before concluding this is user-visible rather than an artifact.

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
