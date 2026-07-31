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
| `frontend/tests/unit/**` (vitest) | `npx vitest run` | view→model mapping, local-player-by-host, progress extrapolation, `TimeFilter` clock sync, settings service (17 in the data-service suite) |
| `tests/Unit/**` (pytest, 25) | `PYTHONPATH=backend/scripts pytest tests/Unit` | router path selection · loopback URL rewrite · snapshot wire round trip · **source_config** (port/UDP allocation, filtering, template substitution) · **source_manager** reconcile (start order, rename-respools-keeps-source, dead/empty respawn, render-on-change, one-bad-endpoint isolation) |

**Done 2026-07-23:** `test_source_config.py` (9) and `test_source_manager.py` (8) replace the
scratch smoke script. `SourceFeeder` idle transitions can't be unit-tested (they need a real FIFO +
group, and sendspin_server imports aiosendspin) — they are covered by `t2_source_lifecycle.sh`
instead. `integrations_api` CRUD is covered live by `t2_endpoint_crud.sh`.

**Still open (tier 1):** a pytest for `settings_api` atomic write + version bump + deep-merge, and
for the `integrations_api` EndpointsManager port allocation with a temp settings file (both pure).

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

## Tier 3 — two-unit mesh (the 192.0.2.0/24 rig)

- Discovery: each unit sees the other; `host` comes from the beacon.
- Routing: intra-server re-group; cross-server reclaim; delegate-to-owning-unit.
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
| We release it | us → HA Voice PE | leaves the group **and the socket closes** (all four steps) |
| We reclaim our own speaker from MA | us → us | source picker pulls it back (adopt by URL) |
| Controller into MA | us → MA | group/metadata/controller state arrives; note `supported_commands` |
| **MA discovery sweep during our playback** | MA → us | ⚠️ known to fail — the arbitration gap. Track until fixed. |

**Re-probe with MA actually playing** to settle whether `supported_commands` gains transport.

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
