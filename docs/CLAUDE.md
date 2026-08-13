# CLAUDE.md — Plum-Audio

> **Purpose**: project memory for Claude Code — the rules that stop an agent breaking things.
> **Status**: Phase 3, on `feature/phase3-sources-gui`. Phase 2 (mesh) is merged to `main`.
> AirPlay/Spotify/Bluetooth, the mesh, interop, the container and output selection are all
> hardware-validated on four units. Remaining: DLNA + Plexamp (no backend yet) and the gaps in
> `docs/SPEC-CONFORMANCE.md`. What landed when → `docs/PHASE-HISTORY.md`.
>
> **Silence from an ESP32 client is NOT settled as device-side.** The long-standing "Voice PE renders
> nothing" finding was measured on builds that leaked an immortal dialer per adopt; on the fix, one
> rendered audio. Play from Music Assistant first before blaming us, but do not treat that entry as
> closed — `docs/HARD-WON-LESSONS.md`.

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

**Backend** — Python 3.13 · `aiosendspin` **pinned 9.1.0** · PyAV · numpy · Flask (:5002) + aiohttp
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
    sendspin_server.py · sendspin_player.py   # the two audio processes
    sendspin_identity.py                      # X25519 keypairs + pairing stores (/config/identity)
    lifecycle.py · audio_devices.py · player_state.py · unit_identity.py
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
- **Python**: `ruff` + `black`, 4-space, PEP 8 naming (overrides the house PascalCase-files rule).
  **TS**: ESLint + Prettier, 2-space, `PascalCase.tsx` components, `camelCase.ts` services. 120 cols.
- **Docker**: Binhex conventions (see the global CLAUDE.md). Project deltas: host networking, and
  `/proc/asound` bind-mounted from the host at `/host/asound` because it is masked in the container.
  `/media` is mounted and declared but nothing reads it yet — it is there for Plexamp.

### Principles
1. Keep it simple. 2. **Audio reliability first — never compromise the pipeline.** 3. Document the
*why*. 4. Fail gracefully. 5. Test on hardware.

## Project-specific rules — the ones that break things

The *reasoning* behind these, and the failures that produced them, is in
**`docs/HARD-WON-LESSONS.md`**. Do not re-litigate them from first principles.

- **Pin `aiosendspin`** (9.1.0). On any bump run `tests/Integration/t0_sendspin_protocol.py` first
  (tier 0 — real protocol, no rig; needs a venv on the candidate version), and re-check
  `docs/UPSTREAM-AIOSENDSPIN.md`. Port notes: `docs/AIOSENDSPIN-BUMP-SCOPE.md`.
- **A role is ALWAYS negotiated but only ACTIVATED when the client is PAIRED** (or, with unpaired
  access on, when the client sets `unpaired_access_enabled` AND the server calls `trust_unpaired()`).
  Miss it and the endpoint connects, negotiates, joins the group at the right volume and renders
  **nothing**, with no error at either end — `active_roles` vs `negotiated_role_ids` is the only tell,
  and it is published on `PlayerState` alongside `security`/`paired`. **`security is None` means
  CLEARTEXT**, which is how the GUI tells "needs pairing" from "can never pair". Trust and pairing are
  both per-server AND per-peer. Applies to ENCRYPTED clients only — see the next rule.
- **Pairing is implemented; unpaired access defaults OFF.** A unit pairs with its own speaker via
  `/config/identity/local-pair.psk`, peers pair via `PLUM_FLEET_PSK` when set, and everything else is
  a GUI act. `pairing_psk` needs NO window; the window (300 s, ONE attempt) is for PIN methods and
  later-added units, opened via the `management` role — which is why a unit pairs with its own player
  at startup. `docs/SENDSPIN-PAIRING.md`.
- **A `management` session must be CLOSED, or that player can never roam again.** A declared activity
  is part of what the client's arbitration ranks when a second server dials, so a server still
  holding `management` outranks a peer asking for plain PLAYBACK: the peer's dial is accepted
  provisionally, handshakes, then is rejected — it lands in the peer's registry as `(disconnected)`
  and the reclaim polls 10 s for a client that never comes up. Nothing expires the session, so the
  player stays unroamable for the life of the process and a **restart "fixes" it**, which is what
  makes this look like drifting state. `open_pairing_window` enables it for exactly the length of the
  call and disables it in a `finally`. Reproduction: 12/12 roams, one `/api/mesh/pairing-window`, then
  failure on every attempt after.
- **Pair by STAGING before the dial, never by `initiate_pairing` after it — and never at all over
  cleartext.** Both halves cost a working rig on 2026-08-13. `initiate_pairing` on a connected client
  whose PSK is the sentinel forces a mid-connection Noise re-handshake; a peer's player is contended
  (its own server dials it too, and it holds ONE websocket) so the re-handshake finds the socket gone
  — `expected Noise message 2 (TEXT), got CLOSE`, then that player's reclaim times out forever.
  `stage_shared_psk` puts the PSK in front of the handshake (`_psk_provider` reads it while
  *choosing*), so the connection arrives already paired and nothing renegotiates. Stage only ids we
  already share a secret with — our own player, and a peer's from a snapshot. A pairing handshake
  against a **cleartext** client is aborted outright by the library, so staging or pairing an ESP32
  takes it offline: it connects, the doomed attempt runs, and adopt reports "never connected" about a
  device whose MAC is in the log one line up. Signature: the NEXT adopt succeeds.
- **Cleartext clients skip the trust gate entirely, and our own player can never be one.** A legacy
  `client/hello` is activated straight from the negotiated set, so ESP32 speakers, Music Assistant
  and our hand-rolled GUI controller need no pairing — that is what `PLUM_ALLOW_UNENCRYPTED=1` buys.
  But there is **no client-side legacy mode**, so a foreign server dialing OUR player must speak
  Noise too; measured, MA 2.9.x cannot. `docs/SENDSPIN-PAIRING.md`.
- **A Sendspin id is a public key, and a unit now has THREE ids.** `unit_id` keys the mesh;
  `server_id`/`player_id` are X25519 peer ids from `/config/identity` and are what the protocol
  uses; the player also keeps a **listener id** (`PLUM_PLAYER_ID`) for mDNS and for a server to dial.
  Anything joining across those namespaces must be explicit — `MeshView.unit_by_server_id`, and the
  player's self-report publishes its **peer** id (publishing the listener id instead duplicated every
  speaker in the GUI and made idle speakers unroutable). `/config/identity` is a device certificate:
  losing it makes a unit a stranger to every peer.
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
- **Never re-dial a foreign speaker you already hold — and when you must, wait for the old dial to
  DIE.** Both halves are load-bearing: `connect_to_client(url)` is a no-op while a registration for
  that URL exists, so a stale one must be torn down; but redialing unconditionally leaks a dialer per
  adopt, and they fight over the single websocket a client allows. Go through `_stop_dialing`, fast-
  path when `_connected_player_at(url)` answers, and identify a speaker by its **registered URL** —
  never by "a client id that was not in the set before". Evidence: HARD-WON-LESSONS.
- **Never hand a joining client the stream you are about to replace.** `attach_player` stops, changes
  membership, then re-acquires (`SourceFeeder.membership_change`, lock-serialised against the pump).
  The other order puts `stream/start`→`stream/end`→`stream/start` on the wire in ~110 ms. Keep it,
  but it is **belt-and-braces, not a proven fix** — do not cite it as a cause without an A/B, and note
  an ESP32 that does wedge is unrecoverable over the protocol (only a power cycle clears it).
- **A routed player must never have a registry eviction pending.** `_schedule_cleanup` overwrites
  `_cleanup_handle` without cancelling it, so a release orphans a timer that later evicts whichever
  client holds that id — a random dropout, now on a **180 s** fuse. `attach_player` and
  `release_foreign_client` defuse it via `_cancel_pending_cleanup`; UPSTREAM §5, HARD-WON-LESSONS.
- **Codec choice belongs to the CLIENT.** `supported_formats` is in priority order and the server
  takes the first match it implements. A player that cannot sustain its own choice renegotiates with
  `stream/request-format`. Do not add a server-side override without a live, proven case — one was
  written and reverted (`0d7c6ab`). A heterogeneous group is normal, not a problem.
- **Announce idle, don't imply it.** On EOF or `PLUM_SOURCE_IDLE_TIMEOUT` silence call
  `group.stop()` (playback_state=**stopped**), never `stop_stream()` (which keeps clients logically
  PLAYING). The spec has no distinct idle state — `stopped` is it. The group and its anchor persist,
  so the **source** stays routable — but every attached **player** does not. "None" is a true none
  (`docs/ROUTING-MODEL.md` rule 1): going idle detaches every player-role client uniformly, and only
  `autoSwitch.localActivity` (own player, rising edge) or `follow` brings one back. A **reversal** —
  the old silent auto-resume was the bug.
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
- **`client/state` carries `available: bool`, and it is NOT the old `state` enum renamed.** The
  server ends an active stream before honouring `available=False`, so a struggling renderer reports
  `available=True` and its health rides `PlayerHealth` → the log and `player_state.json` instead.
  9.x deleted the wire field that used to carry it.
- **The output device's identity is the ALSA CARD NAME, never `hw:C,D`.** Card numbers move — the
  HiFiBerry on `.100.21` was card 2, then 1, then 2, then 0 across four reboots with config
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
- **Every default name must be unique per unit, and a clash NEVER blocks a deploy.** A literal in
  `DEFAULT_SETTINGS` reaches `settings.json` on first read and then outranks the env permanently, on
  every unit identically. Unit and endpoint names derive from `PLUM_UNIT_NAME`, else
  `unit_identity.default_device_name()`, which appends the **Pi's SoC serial** (not a MAC — a MAC
  moves with the interface). No token → bare name; unknown beats invented. `deploy.sh` warns and
  disambiguates, never refuses. Env-derived defaults are `sanitize_device_name`'d **at import**.
- **mDNS hostname changes go through Avahi's D-Bus `SetHostName`** on the HOST bus — never by writing
  `/etc/avahi` or restarting a service. Setting the name it already has raises "invalid because
  redundant" (a no-op), and a real change drops the D-Bus connection mid-call, so success surfaces as
  failure. Always reconnect and read the name back.
- **The player is a PROCESS, not a setting.** `audio.output.device = "none"` means no
  `sendspin_player` runs at all — `output_gate.py` omits the program file before supervisord. It
  cannot be a running player with nothing open (`AlsaRenderer.start()` raises, and it runs *before*
  the listener), hence the restart requirement and `has_player` on the snapshot — **defaulting
  True**, or a peer on an older image reads as playerless. A playerless unit leads follow, never
  follows. `find_device` short-circuits the sentinel before its substring pass.
- **Host provisioning is not optional, and is once per IMAGE.** The bluez patches, the D-Bus policy,
  the rfkill unblock and the HAT mixer are installed by nothing the container does, and each absence
  fails silently or catastrophically. Run `scripts/host-setup/provision.sh` from the workstation — a
  freshly imaged Pi has no copy of this repo. `docs/HOST-PROVISIONING.md`.
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
`scripts/host-setup/provision.sh all` once per Pi image, then `docker/build.sh` and
`docker/deploy.sh all` per deploy. Full loop, the deceptive failure modes, and the debugging cookbook
are in **`docs/OPERATIONS.md`**; commissioning in **`docs/HOST-PROVISIONING.md`**.

## Open

Full list — known gaps, deferred calls, resolved-with-history — is **`docs/OPEN-ITEMS.md`**. Only the
items that change how you would write code *today* are repeated here:

- **DLNA and Plexamp have no backend.** GUI cards and settings stubs exist and are gated off; no
  ports are in use. Do not treat their scaffolding as a working integration.
- **`@sendspin/sendspin-js` is deliberately held at 3.2.1.** 5.0.0 is Noise-only and drops the
  caller-chosen `playerId` that `MeshApp`'s browser-route reconciler joins on. It becomes forced the
  day `PLUM_ALLOW_UNENCRYPTED` goes off.
- **A follower stops following when its leader switches source** (`follow.tick()` reads the
  resulting `current_target = None` as "the user moved us"). Pinned by a parity test rather than
  changed; distinguishing "went idle" from "was moved" needs a real decision.
- **The APIs are unauthenticated with blanket CORS**, both bound to `0.0.0.0`. Deliberately deferred
  — peers and the GUI both call peer `:5001` cross-origin, so restricting it needs a rig test.
- **amd64 has never been run** (it has been built twice; `deploy.sh` hard-refuses non-arm64).
- **Frontend test suites are thin** and several assert nothing about production code; `MeshApp`,
  the controller client and the browser player have no coverage at all.

## Resources
- Sendspin spec: <https://www.sendspin-audio.com/spec/> · Org: <https://github.com/Sendspin>
- `aiosendspin`: <https://github.com/Sendspin/aiosendspin>
- Predecessor: the **Plum-Snapcast** repo — this repo implements the design its architecture doc set out.

## Maintaining this file
Update on: architecture changes, new sources, new env vars, new workflows. **Keep it under ~280
lines** — it loads into every session. It reached 498 by absorbing things that belong elsewhere and
was cut back on 2026-08-13; the overflow went to `docs/OPEN-ITEMS.md` and `docs/HARD-WON-LESSONS.md`
rather than being deleted. War stories → HARD-WON-LESSONS. Dated narrative → PHASE-HISTORY.
Procedures → OPERATIONS. Known gaps → OPEN-ITEMS. **A rule earns its place here only if an agent
would break something without it** — and it should be the RULE, with the evidence linked, not
retold. Document *why*, not just *what*.
