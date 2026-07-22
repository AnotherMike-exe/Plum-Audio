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
| `frontend/tests/unit/**` (vitest) | `npx vitest run` | view→model mapping, local-player-by-host, progress extrapolation, settings service |
| `tests/Unit/**` (pytest) | `PYTHONPATH=backend/scripts pytest tests/Unit` | router path selection, loopback URL rewrite, snapshot wire round trip |

**Gaps to close (highest value first):**
1. `SourceManagerBase` reconcile — desired/running diffing, signature respool, empty-proc respawn,
   daemon start order. Currently proven only by a scratch script; make it a pytest with a fake
   server + fake daemons.
2. `spotify_config` / `airplay_config` rendering — port/UDP-block allocation, endpoint filtering.
3. `SourceFeeder` idle transitions — first audio → `playing`, EOF → `stopped`, idle timeout →
   `stopped` (a fake group recording `start_stream`/`stop` calls; no ALSA needed).
4. `integrations_api` CRUD — already exercised by a scratch script; promote to pytest with a temp
   settings file.

## Tier 2 — single unit on hardware

Everything that needs a real FIFO, a real daemon, or a real DAC. Target: one Pi, ~2 minutes.

- Source lifecycle: feed the source FIFO → `active=true`, `playback_state=playing`; stop → `stopped`.
- Endpoint CRUD live: add / rename / disable / remove an AirPlay and a Spotify endpoint; assert the
  daemon set respools and the other endpoints are untouched.
- AirPlay per-endpoint MPRIS: each endpoint owns `org.mpris.MediaPlayer2.ShairportSync` on its own
  private bus.
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

Untested: **the image has never been built.** Before any deployment claim —

- multi-arch build (amd64 + arm64); frontend build stage; go-librespot fetched for the target arch.
- supervisord brings up server, player, config API, nginx — and NOT shairport (the manager owns it).
- Host Avahi + system D-Bus sockets mounted; host networking; mDNS still works from inside.
- `/config`, `/data`, `/media` volumes; PUID/PGID/UMASK honoured; settings survive recreation.

---

## The immediate task: make tiers 2–4 reproducible

Every hardware check so far has been an ad-hoc script. Promote them to `tests/Integration/` as
runnable, self-describing scripts with a tiny harness:

```
tests/Integration/
  README.md              which rig each needs, and how to point at it
  lib.sh                 ssh/curl helpers, assert_json, wait_for
  t2_source_lifecycle.sh t2_endpoint_crud.sh t2_airplay_mpris.sh
  t3_mesh_roam.sh        t3_multigroup.sh
  t4_interop_ma.sh       t4_adopt_release.sh   t4_controller_probe.py
```

Each script: take the target host(s) as arguments, print PASS/FAIL per assertion, exit non-zero on
failure, and leave the rig in the state it found it. That last property is not optional — tier 4
runs against live devices in someone's home.

**Suggested order:** (1) the harness + `t4_adopt_release.sh` and `t4_interop_ma.sh`, since those
cover the newest and least-proven code and currently exist only as scratch files; (2) tier 2
scripts; (3) the tier-1 pytest gaps above; (4) tier 6 once the image builds.
