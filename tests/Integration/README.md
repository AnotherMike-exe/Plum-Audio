# Integration tests — Plum-Audio

Hardware tests that were previously ad-hoc scratch scripts, now repeatable. Each is a bash script
that takes its target host(s) as arguments, prints `PASS`/`FAIL` per assertion, and exits non-zero
if anything failed. All share `lib.sh` (ssh/curl helpers, assertions, `wait_for`, `defer` teardown).

**They leave the rig as they found it** — added probe endpoints are removed, adopted speakers are
released, roamed players are sent home, via `defer` that runs even on failure. This matters most for
tier 4, which runs against live third-party devices in someone's home.

## Requirements

- `sshpass` on the machine running the tests (the scripts ssh into the Pis).
- The Pis reachable, with the Plum stack running (server + player + config API + mesh API).
- Creds default to `plum-admin` / `REDACTED-USE-PLUM_TEST_PW`; override with `PLUM_TEST_USER` / `PLUM_TEST_PW`.

## What each tier needs, and how to run it

| Script | Rig | Run |
|---|---|---|
| `t2_source_lifecycle.sh` | any one unit | `./t2_source_lifecycle.sh <host> [source-id]` |
| `t2_endpoint_crud.sh` | any one unit (config API on :5002) | `./t2_endpoint_crud.sh <host> [spotify\|airplay]` |
| `t3_mesh_roam.sh` | **two** Plum units on one segment | `./t3_mesh_roam.sh <unit-a> <unit-b>` |
| `t3_autofollow.sh` | **two** Plum units on one segment | `./t3_autofollow.sh <unit-a> <unit-b>` |
| `t4_interop_ma.sh` | a unit **on Music Assistant's L2 segment** | `./t4_interop_ma.sh <host>` |
| `t4_adopt_release.sh` | a unit + a foreign Sendspin speaker on-segment | `./t4_adopt_release.sh <host> [speaker-url]` |

Rigs as of this writing (see the memory note `vlan7-interop-rig`):
- **Two-unit mesh**: `192.0.2.10` + `192.0.2.11`.
- **Third-party interop**: `198.51.100.20`, alongside Music Assistant (`.226`) and a Home Assistant
  Voice PE (`.214`). mDNS is link-local, so tier 4 can ONLY run on the third party's segment.

Run everything for one rig via `./run.sh`:
```
./run.sh mesh    192.0.2.10 192.0.2.11   # tier 2 + tier 3
./run.sh interop 198.51.100.20                      # tier 2 + tier 4
```

## Notes

- Tier 4's `t4_interop_ma.sh` reports `SKIP` for the "MA claimed our speaker" check unless MA is
  actively playing to the unit — start playback in MA to exercise that path.
- The pure-logic tests live in `tests/Unit` (pytest, no hardware) and run in CI; see `docs/TESTING.md`
  for the full tier map and what is still not covered (tier 5 soak, tier 6 container).
