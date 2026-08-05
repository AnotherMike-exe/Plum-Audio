# Testing plan — Plum-Audio

> Written 2026-07-21, after the Phase-3 interop slice. The honest starting position: the pure logic
> is tested, the hardware behaviour is **not reproducible** — nearly every hardware finding so far
> came from a one-off script that no longer exists. This plan's first job is to fix that.
>
> Read with docs/SPEC-CONFORMANCE.md: tier 4 exists to keep that file true.

## Why the hardware tiers carry the weight

Every serious bug in this project has been hardware-only, and none of them would have failed a unit
test: the loopback reclaim URL, the per-interface mDNS resolution, the `ServiceBrowserPrepare` race,
the stale progress anchor, a released speaker staying ESTABLISHed, `attach_websocket` refusing a
second socket. Protocol code fails at the edges of *other people's* implementations. So the plan
weights integration and interop over breadth of unit coverage.

---

## Tier 1 — pure logic (fast, CI-able, no hardware)

| Suite | Runs | Covers |
|---|---|---|
| `tests/Unit/**` (pytest, **217**) | `PYTHONPATH=backend/scripts pytest tests/Unit` | see the breakdown below |
| `frontend/tests/unit/**` (vitest, **112**) | `cd frontend && npx vitest run` | view→model mapping, local-player-by-host, progress extrapolation, `TimeFilter` clock sync, audio + settings + integrations services |

Backend, by file: `audio_devices` 31 · `bluetooth_avrcp` 27 · `audio_api` 21 · `follow_reconciler`
21 · `configure_audio_hat` 17 · `audio_output_apply` 14 · `settings_store` 14 · `volume` 12 ·
`bluetooth_config` 11 · `mesh_routing` 11 · `mesh_discovery` 10 · `source_manager` 10 ·
`client_state_conformance` 9 · `source_config` 9.

`SourceFeeder` idle transitions can't be unit-tested (they need a real FIFO + group, and
`sendspin_server` imports aiosendspin) — `t2_source_lifecycle.sh` covers them. `integrations_api`
CRUD is covered live by `t2_endpoint_crud.sh`.

> **Read the frontend number with suspicion.** Of the six vitest files, only `audioService` (12) and
> `sendspinDataService` (23) import a production module at all. The other four — `NowPlaying` (13),
> `PlayerControls` (18), `integrationsService` (27), `settingsService` (19) — assert against MSW
> handlers or against mock components declared inside the test file, so **77 of the 112 would stay
> green if the code they name were deleted**. That is not hypothetical: four more files of the same
> shape were removed with the dead Snapcast tree, and two of them (`federationService`,
> `playbackService`) never imported the service in their own filename. Treat the frontend suite as
> covering two modules, not six, until those are rewritten against real imports.

**Still open (tier 1):** the four hollow frontend suites above · `sendspin_server.py` has no unit
coverage at all, including `SourceFeeder.refresh_stream` (the fix for the silent-speaker bug) ·
`AlsaRenderer._callback` and `_publish_render_state` are untested · `source_manager`'s real
`_spawn_daemons`/`_kill_daemons` are stubbed out by the suite that appears to cover them.

## Tier 2 — single unit on hardware

Everything that needs a real FIFO, a real daemon, or a real DAC. Target: one Pi, ~2 minutes.

- Source lifecycle: feed the source FIFO → `active=true`, `playback_state=playing`; stop → `stopped`.
- Endpoint CRUD live: add / rename / disable / remove an AirPlay and a Spotify endpoint; assert the
  daemon set respools and the other endpoints are untouched.
- AirPlay per-endpoint MPRIS: each endpoint owns `org.mpris.MediaPlayer2.ShairportSync` on its own
  private bus.
- Bluetooth AVRCP position: on a unit carrying the `backend/config/bluez/` patches, `GetPlayStatus`
  is polled every ~2 s while the phone is playing and `Position` reaches D-Bus clients on that beat,
  so a scrub lands within ~2 s. Needs a phone connected and playing; `t2_bt_avrcp_position.sh`.
- Config API + GUI served by nginx; settings persist across a restart.
- Output selection: `/api/audio/devices/output` lists every card `aplay -l` sees; the ACTIVE one is
  selectable even though our own player holds it (PortAudio cannot enumerate a busy exclusive card
  — assert the active device is never greyed out); an unopenable card (HDMI with no display) is
  refused with a 409 rather than persisted; switching writes `settings.json` and the player applies
  it within its poll, with `pending` true until the echo in `player_state.json` agrees.
- **Concurrently**, not sequentially: fetch `/devices/output` and `/output/current` in parallel, in
  a loop. Sequential calls cannot reproduce the PortAudio re-init SIGSEGV that crash-looped the
  config API, and three rounds of sequential hardware testing missed it.
- A failed switch keeps playing: write an unopenable device straight into `settings.json` (bypassing
  the API's validation) and assert the renderer restores the previous device and `pending` stays
  true, rather than the unit going silent with a correct-looking settings file.
- On a HAT unit: the card is addressed by NAME across a reboot (`sndrpihifiberry:0` survives; the
  `hw:C,D` behind it may not), and the hardware mixer is at 0 dB after `alsa-restore`.

## Tier 3 — two-unit mesh (the 192.0.2.0/24 rig)

- Discovery: each unit sees the other; `host` comes from the beacon.
- Routing: intra-server re-group; cross-server reclaim; delegate-to-owning-unit.
- **Re-route onto an ALREADY-STREAMING source, and assert audio actually arrives** — not just that
  the player appears in the group. This is the gap that let the silent-join bug live: every earlier
  test routed first and started the source after, which works. Route a connected player into a live
  stream and assert a fresh `Stream started` on the client plus a renderer buffer that leaves 0 ms.
  Membership in the group is not evidence of audio.
- **Roam is inaudible**: `pad_ms` (emitted silence) unchanged across a handoff — the Phase-2
  measurement, worth re-running whenever the audio path changes.
- Per-player volume; multiple concurrent groups; 0 xruns under concurrent load.
- GUI: each unit's page features itself; idle peers read as idle; picker moves the group.

## Tier 4 — third-party interop (the 198.51.100.0/24 rig, with MA + HA Voice PE)

The tier that justifies the platform choice. **mDNS is link-local — this can only run on the segment
the third parties are on.**

| Case | Direction | Expected |
|---|---|---|
| MA discovers our player | MA → us | appears in MA within seconds of advertising |
| MA plays to our speaker | MA → us | audio renders; our GUI shows "→ Music Assistant" + track |
| We adopt a foreign speaker | us → HA Voice PE | joins our source group; audio renders |
| **Adopt the SAME speaker repeatedly, without releasing** | us → HA Voice PE | every attempt succeeds. A second `adopt` used to be a silent no-op (the dial was still registered) reporting "never connected" about a device whose port was open |
| We release it | us → HA Voice PE | leaves the group **and the socket closes** (all four steps) |
| We reclaim our own speaker from MA | us → us | source picker pulls it back (adopt by URL) |
| Controller into MA | us → MA | group/metadata/controller state arrives; note `supported_commands` |
| **MA discovery sweep during our playback** | MA → us | ⚠️ known to fail — the arbitration gap. Track until fixed. |

**Re-probe with MA actually playing** to settle whether `supported_commands` gains transport.

**Before blaming ourselves for a third-party speaker, play to it from MA.** The HA Voice PE
negotiates cleanly with us (handshake, group, `stream/start`, it acknowledges the codec header,
reports PLAYING, tracks volume) and renders nothing — and it does the same from **Music Assistant**,
under both FLAC and PCM. That one comparison is the cheapest way to split "our bug" from "their
device", and running it earlier would have saved a long chase down sample-rate and codec theories
that were both wrong. Treat it as the first step of any foreign-speaker investigation, not the last.

## Tier 5 — soak and failure modes

Not yet run; the reliability work Phase 4 gates on.

- ≥3 units, ≥8 h continuous: xruns, `pad_ms`, RSS, FIFO backpressure, daemon respawns.
- Kill and restart each process in turn (server, player, daemon, config API) and assert self-heal.
- Network faults: unplug a unit mid-roam; drop the segment; restart Avahi under load.
- Endpoint churn under playback: rename/disable while a sender is mid-session.

## Tier 6 — container

**Built and deployed to all three units, 2026-07-31** (`docker/build.sh` → `docker/deploy.sh`).
Verified on the rig:

- arm64 build (native on an Apple Silicon host, no qemu); frontend build stage; go-librespot
  fetched per arch. Image ~549 MB, ~208 MB compressed for transfer.
- supervisord brings up server, player, config API and nginx — and **not** the source daemons: the
  managers own those. Confirmed by `supervisorctl status` (4 programs) plus `ps` inside the
  container showing shairport, its private `dbus-daemon` and the go-librespot instances.
- Host Avahi + system D-Bus mounted, host networking: the container's shairport advertises
  `_raop._tcp`, Spotify endpoints register Connect, and the mesh both publishes
  `_sendspin-server._tcp` and discovers its peer — all through the **host** responder.
- `/config`, `/data`, `/media` volumes; PUID/PGID/UMASK honoured; `deploy.sh` imports the unit's
  existing `settings.json` and go-librespot auth state on first deploy, and leaves both alone on
  every later one, so a rebuild is not a re-authorisation.

Still unverified at this tier: an **amd64** build (only arm64 has been built), container restart
under live playback, and behaviour across a host reboot.

---

## Integration harness — built 2026-07-23

`tests/Integration/` now holds the promoted scripts + a shared harness (`lib.sh`: ssh/curl helpers,
assertions, `wait_for`, and `defer` teardown that always runs). Each takes its host(s) as arguments,
prints PASS/FAIL per assertion, exits non-zero on failure, and **leaves the rig as it found it** —
non-negotiable for tier 4's live third-party devices. `run.sh` drives a whole rig's suite; see
`tests/Integration/README.md`.

| Script | Status |
|---|---|
| `t2_source_lifecycle.sh` | ✅ passing (VLAN-7 unit) — the feeder idle contract's home |
| `t2_endpoint_crud.sh` | ✅ passing — live add/rename/remove, others untouched |
| `t4_interop_ma.sh` | ✅ passing — MA discovered, we advertise both ways, claim self-reported |
| `t4_adopt_release.sh` | ✅ passing — adopt an HA Voice PE, release incl. the socket-closed check |
| `t2_bt_avrcp_position.sh` | ✅ passing (VLAN-7 unit + iPhone, 2026-07-29) — 8 polls / 12 s, 4 Position signals / 9 s |
| `t3_mesh_roam.sh` | ✅ passing (`.201` rig, 2026-07-24) |
| `t3_autofollow.sh` | written, syntax-checked; **pending** a run on the `.201` rig |

**Remaining:** run `t3_autofollow.sh` on the mesh rig; add `t2_airplay_mpris.sh` (per-endpoint
private-bus MPRIS ownership) and a `t3_multigroup.sh`; the tier-5 soak and tier-6 container tiers.
