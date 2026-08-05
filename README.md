# Plum-Audio

> Mesh multi-room audio streaming, built on Sendspin. Successor to Plum-Snapcast.

Plum-Audio streams synchronized audio to any number of rooms. Each unit ingests local sources
(AirPlay, Spotify, Bluetooth) and can cross-route them across a mesh of units, with metadata, album
art and a visualizer delivered out-of-band. It replaces the Snapcast + custom federation backbone of
Plum-Snapcast with **Sendspin** as the sole sync engine.

A unit is **one container per Raspberry Pi**: the Sendspin server and player, the mesh orchestrator,
the source managers, the config API, and the nginx that serves the GUI all run inside it.

---

## Quick start

```bash
docker/build.sh                    # arm64 image -> dist/*.tar.gz
docker/deploy.sh all               # every unit in docker/units.conf
docker/deploy.sh 192.0.2.10   # or just one
```

A new Pi needs host provisioning first — the audio HAT overlay and mixer, the patched `bluetoothd`,
and a D-Bus policy that nothing installs automatically. Do that before the first deploy; after
that, `docs/OPERATIONS.md` is the daily loop.

## Documentation

| Doc | What it is for |
|---|---|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | What the system is — mesh model, process model, subsystem design |
| [OPERATIONS.md](docs/OPERATIONS.md) | Build, deploy, debug |
| [HOST-PROVISIONING.md](docs/HOST-PROVISIONING.md) | Commissioning a new Pi — the once-per-unit checklist |
| [SPEC-CONFORMANCE.md](docs/SPEC-CONFORMANCE.md) | Where we stand against the Sendspin spec |
| [UPSTREAM-AIOSENDSPIN.md](docs/UPSTREAM-AIOSENDSPIN.md) | Workarounds to delete when the pin bumps |
| [HARD-WON-LESSONS.md](docs/HARD-WON-LESSONS.md) | Why the code is shaped this way. Read before "simplifying" |
| [PHASE-HISTORY.md](docs/PHASE-HISTORY.md) | What shipped when, and what hardware proved it |
| [TESTING.md](docs/TESTING.md) | Test tiers, and what is not yet reproducible |
| [CLAUDE.md](CLAUDE.md) | Claude Code project memory |

## Tech stack

- **Backend**: Python 3.13, `aiosendspin` (pinned 6.0.5), PyAV, numpy, Flask + aiohttp, supervisord
- **Frontend**: React 19, TypeScript 5, Vite 6 — served by nginx inside the unit container
- **Base image**: `python:3.13-slim-trixie`. glibc, and **trixie specifically**, to match the units'
  Debian 13 for bluez-alsa and shairport-sync parity. Not Alpine — see ARCHITECTURE
- **Infra**: host networking (mDNS needs L2), host Avahi and D-Bus
- **Target**: Raspberry Pi 4 / Debian 13, arm64. amd64 has never been built

## Status

**Phase 3**, on `feature/phase3-sources-gui`. Phase 2 (the mesh) is merged to `main`.

AirPlay, Spotify and Bluetooth are hardware-validated across four units, as are the mesh (discovery,
aggregation, roam, multi-group, per-player volume), third-party interop against Music Assistant, the
container build, and audio output selection. Output is chosen in **Settings → Audio**.

Remaining: DLNA and Plexamp — neither has a backend yet — and the gaps in
[SPEC-CONFORMANCE.md](docs/SPEC-CONFORMANCE.md). See [PHASE-HISTORY.md](docs/PHASE-HISTORY.md) for
what landed when and what measured it.

---

**Maintainer**: Plum Solutions
