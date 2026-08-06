# Phase History

> **Purpose**: what shipped, when, and — the part worth keeping — **what hardware proved it and what
> was measured**. Read this when you need to know whether something has been validated on real
> units and under what conditions, or when a number in a discussion needs a provenance. The
> measurements below are not in any commit message and would be expensive to re-take.
>
> Newest first. `docs/ARCHITECTURE.md` states the design as it is *now*; `docs/CLAUDE.md` states the
> rules; `docs/HARD-WON-LESSONS.md` explains why the rules exist. This file is the only place the
> dated narrative lives.

---

## The rig

Four Raspberry Pi units. The authoritative table — host, unit id, unit name, player id, player name,
DAC — is **`docker/units.conf`**, which `deploy.sh` consumes; do not maintain a second copy.

- **`192.0.2.10` — Pi4-02**, onboard bcm2835. Primary R&D unit; Phase 1 and most of Phase 2 were
  proven here.
- **`192.0.2.11` — Pi4-01**, onboard bcm2835. Second node of the `.201.x` mesh rig; two-node
  discovery, aggregation and roam need both.
- **`198.51.100.20` — Plum VLAN7 Test**, onboard bcm2835. **The interop unit**: it sits on the
  third-party VLAN beside Music Assistant and a Home Assistant Voice PE, and mDNS is link-local, so
  this is the only segment where interop can be tested at all.
- **`198.51.100.21` — Plum Amp100**, HiFiBerry Amp100 (`snd_rpi_hifiberry_dacplus`). Recommissioned
  from Plum-Snapcast 2026-08-04 as the audio-output-selection and HAT-provisioning unit.

The ids are not cosmetic: the mesh keys routing, group membership and per-player volume off them,
and each unit's `settings.json` already refers to them. They match what the pre-container
`~/plum-test` stack ran with, so a unit moving into the container keeps its identity and its routes
rather than reappearing as a stranger.

Production units `.200` / `.203` are still on Plum-Snapcast; their cutover is Phase 4.

**Image uniformity as of 2026-08-05**: all four units run the same image (`6a64b88`). Verify by the
served bundle hash (`curl -s http://<unit>/ | grep -o 'assets/index-[^"]*\.js'`), **not** by image
id — `.7.204` uses Docker's containerd image store while the other three use the classic one, so
`docker inspect` reports a manifest digest there and a config digest everywhere else, and the same
image compares as different across units.

---

## Phase 3 — remaining sources, GUI, container (`feature/phase3-sources-gui`, in progress)

### Alpha deployment onto bare Raspberry Pi OS Lite — 2026-08-06 (`8e255a9`)

**The first deploy onto units carrying nothing but a stock image.** `.201.133` and `.201.113` were
re-flashed to Raspberry Pi OS Lite 64-bit (Debian 13 trixie, kernel 6.18.34, arm64), then taken to a
working two-node mesh by the documented path alone. This is the run that proved the *documentation* is
executable, not just the code — and it found four places where it was not.

Greenfield state, measured before touching anything (identical on both units): Avahi active, bluez
**5.82-1.1+rpt1** unpatched, Bluetooth **soft-blocked**, `Experimental = false`, no bluealsa D-Bus
policy, no `/etc/dbus-1/system.d` at all, no Docker, no nginx, no `~/plum-test`, `dtparam=audio=on`
already present, `bcm2835 Headphones` as card 0 with its `PCM` control already at **0.00 dB**.

What the documented path could not do, and now can:

- **A re-imaged unit's new SSH host key aborted the deploy on its first connection.**
  `StrictHostKeyChecking=no` does not override a *conflicting* `known_hosts` entry — ssh disables
  password auth in that state. Both scripts now pass `UserKnownHostsFile=/dev/null`.
- **Nothing got the host-setup payload onto a fresh Pi.** Every command in HOST-PROVISIONING.md runs
  on the unit against files in this repo, and a fresh Pi has neither the repo nor a remote to fetch
  it from. Closed by `scripts/host-setup/provision.sh`.
- **`deploy.sh --tarball dist/...`, exactly as OPERATIONS.md documented it, always failed** — the
  script `cd`s to its own directory first, so it looked under `docker/dist/`.
- **Both units came up named "Plum Sendspin", and both advertised an AirPlay receiver called "Plum
  Audio"** (`1cd8701`, then the endpoint half on Michael's call). `DEFAULT_SETTINGS` is written to
  settings.json on first read, so its literals outranked `PLUM_UNIT_NAME` permanently — making the env
  tier that `unit_identity.py` documents unreachable. One name for two units in the mesh view, the
  unit cards and mDNS reads exactly like a discovery bug; one AirPlay name for two receivers is
  indistinguishable to a sender. All three source endpoints share the fallback, so Spotify and
  Bluetooth were collision-bound identically once enabled. Both env-derived defaults are sanitized at
  import — `_sanitize_device_names` runs on the write path only, so a default otherwise reaches disk
  and the config renderers unscrubbed.

Verified after: 4/4 supervisord programs RUNNING on both, all three APIs answering, 8927/8928
listening, `bcm2835` → PortAudio index 0 → `hw:0,0` with `Headphones:0` echoed back to
`player_state.json`, **0 ERROR lines in either `sendspin_server.log`**, and a symmetric two-node mesh
— each unit lists both, under distinct names, `has_player=True`, both players attached. Sendspin
server and player mDNS records published under the corrected names.

**Not verified from the workstation**: that the AirPlay receivers actually appear to a sender. mDNS is
link-local and the workstation is on a routed segment (`192.168.197.x`), and Pi OS Lite ships no
`avahi-utils`. shairport-sync is running and errorless on both; confirming the receiver needs a device
on VLAN 201.

Sections 1 (audio HAT) and 4 (mask the user obexd) of the checklist were correctly **no-ops** here —
no HAT fitted, and Pi OS Lite does not ship `obex.service`. Both are now documented as such, because
"not-found" and "no HAT card found" read like failures otherwise.

### GUI polish pass — 2026-08-05 (`8dfaca2`..`6a64b88`)

The first real visual review in a browser on a live unit. Four defects, all in the GUI, none of them
visible from the API or the bundle:

- **Ghost sources** — per-device pickers listed every configured endpoint forever, minutes after the
  sender left. Device lists now take the same active-only truth the top picker uses.
- **Idle devices could not be routed from the GUI at all** — they had only "Join Stream", which needs
  a stream the current page is on, so an idle unit's page controlled nothing. The router had
  supported it the whole time; every device row now carries `StreamPickerButton`.
- **A speaker renamed itself on every join and leave** — handshake name when attached, mDNS instance
  name when idle. Memoised against the listener URL and persisted to localStorage.
- **Scrollbars were the browser's, not the theme's.**

Also: the output picker moved to its own **Audio** tab (Playback is where audio comes *from*, Audio
is where it comes *out* — the Plum-Snapcast split, restored), and Settings opens on `tabs[0]` rather
than a hardcoded default that had drifted. Reasoning for all four in `docs/HARD-WON-LESSONS.md`.

All four units brought onto this image.

### Audio output selection — DONE 2026-08-04

Device discovery, `/api/audio/*`, **live apply without a restart**, the picker in Settings → Audio,
and `scripts/host-setup/configure-audio-hat.sh` for HAT provisioning.

**Validated on `.7.204` (HiFiBerry Amp100), recommissioned from Plum-Snapcast for this.** Measured:

- The HAT enumerated as **card 2, then 1, then 2, then 0 across four reboots** with the config
  unchanged — which is why identity is the ALSA card name, not `hw:C,D`.
- A **failed** output switch restores the previous device in **42 ms, still playing**, rather than
  leaving silence.
- The Amp100's `Digital` mixer boots at **163/207, i.e. -22 dB**, reinstated by `alsa-restore` every
  boot and invisible to every level in the GUI.
- The `dtoverlay` block must precede the first existing `dtoverlay=`; appending after `vc4-kms-v3d`
  costs an HDMI audio output, **measured over 5 boots**.
- The config API crash-looped on concurrent PortAudio re-init (the GUI's single `Promise.all` against
  a `threaded=True` Flask). SIGSEGV, no traceback. Not reproducible with sequential `curl`.

Also on this unit: the missing `bluealsa` D-Bus policy produced **178 restarts and a new dbus-daemon
every 9.5 s**, whose log volume caused two wrong diagnoses the same day.

**Not yet exercised on hardware**: `configure-audio-hat.sh --keep-onboard` and the no-`dtoverlay`
fallback (unit-tested against config.txt fixtures in `tests/Unit/test_configure_audio_hat.py` only).

### Container build — DONE 2026-07-31

`backend/Dockerfile` + `docker/{build,deploy}.sh` + `docker/units.conf`. All R&D Pis run the unit as
a container; the `~/plum-test` dev stack is stopped on each but left on disk, so reverting is
`docker compose down` plus `run_*.sh`.

The base is pinned to the units' Debian release, not "some slim base": bluez-alsa 4.3.1-3 on
**trixie** still installs its daemon as `bluealsa` (upstream renamed it `bluealsad` in 4.0), and
shairport-sync **4.3.7** is the build whose MPRIS behaviour multi-endpoint AirPlay was verified
against. Bookworm would silently hand us shairport 4.1 and a different bluez-alsa. The Dockerfile
asserts `shairport-sync -V | grep -- -mpris` and the `bluealsa`/`obexd` paths **at build time**,
because each of those failures is invisible at runtime.

**Verified on `.201.133`**: shairport inside the container advertises `_raop._tcp` through the *host*
Avahi (`6E1713303D0B@Plum Audio` on :5050), both go-librespot endpoints register Spotify Connect, and
the mesh publishes `_sendspin-server._tcp` while discovering its peer.

Three cutover conflicts, all of which fail deceptively (details in `docs/OPERATIONS.md`): the host's
nginx keeps answering :80 while the container's crash-loops on `bind()`, serving a stale GUI that
looks fine; a SIGTERM-deaf shairport survives `pkill` still holding RAOP 5050; and the identity
defaults (`unit-local`, a `127.0.0.1` player URL) advertise an endpoint no peer can reach, so roams
silently never land.

Compose reaches the units two ways and that is fine: `.7.122` runs Docker CE from Docker's repo
(compose as a CLI plugin), the Debian-packaged units run trixie's standalone `docker-compose` 2.26.
Trixie has **no** `docker-compose-v2` package — the name is `docker-compose`, and it is v2.

Still outside the image: DLNA (gmrender) and Plexamp.

### Bluetooth album art — 2026-07-30

Cover art over OBEX/BIP with a private per-endpoint obexd. Three silent failure modes found and
fixed, one measured on hardware: **our obexd started 10 s after the player bind on `.201.113`**, and
losing that race is permanent rather than transient, so `prepare()` retries for ~30 s. Full account
in `docs/HARD-WON-LESSONS.md`.

Known gap that remains: art cannot appear for the track already playing when a session opens; it
lands on the first track change.

### Bluetooth AVRCP — the ceiling is in `bluetoothd`, and we patch it — 2026-07-29

The A2DP slice relays BlueZ's `org.bluez.MediaPlayer1` into the metadata/artwork roles — A2DP itself
carries no metadata or position, so everything but the audio comes over AVRCP. Three separate hunts
went looking in our relay, the GUI and the metadata role before the source settled it: stock BlueZ
registers position-changed with a **49.7-day** interval and never polls `GetPlayStatus`. The tell was
a position of **454520 ms reported on a 400346 ms track** — an unclamped wall-clock interpolation
running past the end of its own track.

`backend/config/bluez/` carries two DEP-3 patches and `install_patched_bluez.sh`. **Scrubs land in
~2 s, verified.** This is host provisioning, in the same class as the rfkill unblock and the D-Bus
policy; an unpatched unit simply loses scrub reporting. See `docs/HOST-PROVISIONING.md`.

### Consume, observe and control foreign playback — 2026-07-23 (`6148204`)

Our player negotiates PLAYER + METADATA + CONTROLLER + VISUALIZER + ARTWORK, so wherever it plays —
our source, a peer's, or a foreign server — it observes that group's controller state and visualizer
as a spec member and can drive transport. The player is **invariant to audio origin**, so this is
uniform. Relayed to the GUI over a `/api/mesh/consume` WS on the mesh API (no new player port); the
GUI synthesizes a `foreign::` stream from the player's self-report so the existing
now-playing/transport/visualizer components render on it, album art included.

**Hardware-verified against real Music Assistant**: featured now-playing *with album art*, live
visualizer off MA's spectrum, and pause/play/next driving MA.

The key finding: a freshly connected controller lands in MA's **own solo group** (`d1c40416`,
stopped, `[volume, mute, switch]` only) — the wrong bridge. Our **player** is a member of the playing
group, and to a member MA emits the full command set plus metadata plus the visualizer role.

### Visualizer on the native Sendspin role — 2026-07-23 (`9114149`)

aiosendspin's `visualizer@v1` role auto-computes `spectrum` and `loudness` from the source's
PushStream audio, in-library. **The server side is therefore free** — no browser audio, no "Listen in
Browser" dependency, no server code from us.

Measured: with noise feeding a source, a visualizer client received **270 spectrum + 270 loudness
frames over ~9 s**. Two hardware-only GUI bugs fixed (both in `docs/HARD-WON-LESSONS.md`).

Boundary, and it is the model rather than a limitation to fix: the role is per-**source**, computed
by the group's server. Audio a foreign server renders to our player has no source on any Sendspin
server we can read, so it cannot be visualized that way — but the player's own member-view relay
(above) supersedes this for the local speaker's session.

### Interop proven on a third party — 2026-07-21 (VLAN-7 rig)

Music Assistant 2.9.9 and a Home Assistant Voice PE — real foreign implementations on their own
segment, not stand-ins.

- **MA discovered our player over mDNS and dialed it 1.0 s after it began advertising.**
- A speaker claimed by another server stays visible: the player self-reports
  `{attached, server_id/name, group, playback_state, title/artist}` to its unit's mesh API
  (`local_player`), because a unit's server can only see clients attached to *itself*. The GUI renders
  "→ Music Assistant · &lt;track&gt;" rather than losing the device.
- `POST /api/mesh/adopt` pulls a foreign speaker onto one of our sources; `POST /api/mesh/release`
  hands it back. **Release needs four steps and hardware settled it** — the first three each looked
  sufficient and left the speaker ESTABLISHed to us, out of reach of its own server.
- The GUI matches foreign speakers **by URL**: the Voice PE advertises `home-assistant-voice-a1b2c3`
  and connects as its MAC.

The Voice PE's non-rendering was chased here and later (2026-08-04) on both FLAC and PCM, and against
MA itself. Device-side — see `docs/HARD-WON-LESSONS.md`, "Do not re-investigate".

### Interop: we are a peer on a standard network — 2026-07

Sendspin discovery is mDNS with two service types in opposite directions: players advertise
`_sendspin._tcp` (8928), servers `_sendspin-server._tcp` (8927). We had **both switched off**, because
aiosendspin's python-zeroconf binds UDP 5353 and the host Avahi owns it — so no third-party server
could find our speakers and we saw nothing but ourselves. `mesh/avahi.py` registers and browses the
same records through the system Avahi over D-Bus; `mesh/neighbourhood.py` publishes our server record,
watches both types and serves `GET /api/mesh/neighbourhood`.

Two hardware-only lessons came out of this (Avahi resolving per interface *and* family; the
`ServiceBrowserNew` announce race) — see `docs/HARD-WON-LESSONS.md`.

### Multi-endpoint AirPlay + per-unit nginx GUI — 2026-07

AirPlay became config-driven and multi-instance on the source-manager pattern: N endpoints, each with
its own shairport-sync process, RAOP/UDP port block, FIFO, metadata pipe and Sendspin source. The
non-obvious part is transport — shairport's MPRIS bus name is **fixed**, so N instances on the system
bus fight over it and only the first gets play/pause/next/previous. Each endpoint therefore runs a
private `dbus-daemon --session`. **Hardware-verified: both endpoints own MPRIS unopposed.**

`sources/source_manager.py` now holds the shared machinery; Spotify and AirPlay are thin subclasses.

The per-unit GUI is nginx **inside** the unit container (one container per unit, not app + frontend),
serving `/app/www` and proxying `/api/mesh` → :5001 and `/api/{settings,integrations,audio,playback}`
→ :5002. Same-origin, so no CORS, no dev proxy, and no `VITE_*` host baked in: **one build artifact
serves every unit**. The controller WS is deliberately not proxied — the GUI opens one per source
directly at `ws://<unit>:8927`, peers included.

### Settings core + Spotify slice — DONE 2026-07

Flask config host on **:5002** (`settings_api.py` with atomic, versioned writes — the file is a
cross-process contract; `integrations_api.py` for endpoint CRUD), the real Settings overlay wired
into `MeshApp`, and `useThemeSettings`.

Spotify is **go-librespot**, not spotifyd. `zeroconf_backend: avahi` registers Connect discovery
through the system Avahi so nothing binds 5353 behind our back.

**Hardware-validated on both `.201.x` Pis**: Spotify audio, metadata, artwork, transport, timeline,
live endpoint CRUD (add/rename/enable/disable/remove apply **live, in seconds**), and cross-server
roam of a Spotify stream.

Two latent Phase-2 bugs a second source exposed and fixed here: the stale progress anchor on a bare
`playback_speed` flip, and a peer advertising `ws://127.0.0.1:8928` making cross-server reclaim dial
our own loopback.

---

## Phase 2 — the mesh (merged to `main` 2026-07-14)

### Reclaim-gap + live AirPlay validation — 2026-07-14

**The roam is inaudible, and this is the measurement that settles it.** The renderer was
instrumented with unconditional silence accounting (`pad_ms`). Across a cross-server roam the player
fires no `stream_clear`/`stream_end`, its jitter buffer is never flushed, and the DAC drains straight
through the reconnect — **`pad_ms` is unchanged across detach→attach**. ~300 ms of buffer against a
~25-55 ms reconnect: **~6× headroom**. The DISCOVERY pre-connect idea was refuted and removed the same
day (`de50035`).

- **Live AirPlay end-to-end**: real shairport-sync PCM (not a tone); title/artist/album +
  **512×512 JPEG artwork** to the metadata/artwork roles; **0 xruns**.
- **Multi-room**: player-133 and player-113 both joined `.133`'s live-AirPlay group, in sync.
- **Roam off live AirPlay**: player-113 detached holding **435 ms of real AirPlay audio**, reattached
  with the buffer intact, **`pad_ms` unchanged** — zero audible dropout. The tone-based finding holds
  for real bursty content.

### Two-node hardware validation — 2026-07-13 (`.201.133` + `.201.113`)

- **Discovery**: mutual UDP beacon over the real LAN — each unit discovers the other. Loopback could
  not test this at all.
- **Aggregation**: each unit's `/api/mesh/view` shows both units; peer `host` taken from the beacon
  source IP; group ids consistent across the HTTP snapshot poll between hosts.
- **Cross-server roam**: a player ping-ponged **4 hops** between units — **~54 ms route API latency,
  0 xruns, 0 starvations** through every handoff. (The ~54 ms is the *protocol* gap; it is inaudible,
  per the `pad_ms` result above.)
- **Four bugs fixed, only reproducible on hardware** (`70ff0ed`): `reclaim_client_for_playback` is
  synchronous, not awaitable; disconnected clients left routing stubs (snapshot is now
  connected-only); the reclaim URL must be the player's *own* listener URL, since roamed players keep
  their origin host; and the Phase-1 auto-attach supervisor fought roams.

### Multi-concurrent-group validation — 2026-07-13 (`77ff59b`)

Two sources active at once on one unit (`airplay` plus a runtime-created `spotify` via
`POST /api/mesh/source`), each anchoring its own group; two players split across them — one local,
one roamed cross-server in **~54 ms** — with an idle group coexisting on the other unit.
**0 xruns under concurrent load.**

A fifth hardware-only bug (`7e39c47`): the player read volume from the wrong payload level
(`payload.player.{volume,mute}`).

Contention policy fixed here: routes idempotent, last-writer-wins player placement, source groups
persist with 0 players (the anchor keeps the feeder alive for instant re-route).

### Backbone built + single-node-validated — 2026-07

`backend/scripts/mesh/` (discovery, model, aggregator, router, aiohttp api, client, orchestrator) plus
the `sync_engine/` seam, all logic- and HTTP-tested, booting in-process inside `sendspin_server` — no
new supervisord program. Discovery is a UDP broadcast beacon on :8929 rather than mDNS, because
python-zeroconf would bind 5353 and collide with Avahi.

### Spike — protocol timing (`_resources/spike/handoff_probe.py`)

Loopback numbers, before hardware:

- Intra-server re-route is a **true live re-route**: WS stays connected (**0 disconnects**), sub-ms
  control gap, absorbed entirely by the ~250 ms player buffer lead → **0 xruns**.
- Cross-server reclaim is **reconnect-class** (WS drops, `ANOTHER_SERVER`), **~80 ms** control gap in
  loopback.

The probe measured re-route against an *idle-then-started* source, which is exactly why it reported a
clean live re-route and missed the stream-membership rule that surfaced on hardware a month later
(2026-08-04, on `.7.204`). Worth remembering when writing the next probe.

---

## Phase 1 — single-unit core playback (2026-07, `.201.133`)

**Milestone fully achieved on hardware, in Docker**: real AirPlay from an iPhone → shairport → FIFO →
in-process SendspinServer → player → onboard DAC → speaker, **with live metadata and 512×512 album
art**, **0 xruns, no resync storm**. Ran as one supervisord container; the image builds arm64 and the
onboard DAC opens in-container via `--device /dev/snd`.

Measured at the time: live re-route **~0.1 ms / 0 xruns**; cross-server reclaim reconnect-class at a
**~85 ms** protocol gap.

**Later corrected (2026-07-14)**: that reconnect is *inaudible*. The "~200 ms audible silence"
estimate and the DISCOVERY-pre-connect mitigation it motivated were both wrong.

---

## Phase 0 — scaffold (2026-07)

Repo scaffolded to Plum standards. Base-image decision taken here: R&D units are Debian 13 (glibc),
so Debian-slim, not Alpine — the Alpine PyAV pain in the old Plum-Snapcast notes disappears.
`aiosendspin` pinned at 6.0.5 with a vendored smoke test (`_resources/spike/mesh_smoke.py`), and the
spike ported into a real `sendspin_server.py` skeleton.

---

## Phase 4 — cutover (not started)

Migrate the two production units (`.200` / `.203`) once a ≥3-unit soak passes; freeze the
Plum-Snapcast codebase on a tag for rollback.

Remaining before then: DLNA/Plexamp slices, and the conformance gaps in `docs/SPEC-CONFORMANCE.md`.
