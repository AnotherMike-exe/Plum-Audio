# Plum-Audio

> Mesh multi-room audio streaming, built on Sendspin. Successor to Plum-Snapcast.

Plum-Audio streams synchronized audio to any number of rooms. Each unit ingests local
sources (AirPlay, Spotify, Bluetooth, DLNA, Plexamp) and can cross-route them across a mesh
of units, with metadata, album art, and a visualizer delivered out-of-band. It replaces the
Snapcast + custom federation backbone of Plum-Snapcast with **Sendspin** as the sole sync engine.

---

## Quick Start

```bash
# Containerized (Binhex standards)
cd docker && docker compose up -d

# Local backend dev — see docs/DEV-SETUP.md
```

## Documentation

- [Architecture & Plan](docs/ARCHITECTURE.md) — system design, mesh model, phased roadmap
- [Development Guide](docs/DEV-SETUP.md) — environment setup and workflows
- [Quick Reference](docs/QUICK-REFERENCE.md) — standards cheat sheet
- [CLAUDE.md](CLAUDE.md) — Claude Code project memory

## Tech Stack

- **Backend**: Python 3.13, `aiosendspin` (pinned 6.0.5), PyAV, Flask, supervisord
- **Frontend**: React 19, TypeScript 5, Vite 6
- **Infra**: Docker (multi-arch amd64/arm64), **Debian-slim base**, host networking
- **Target**: Raspberry Pi 4, RPi OS / Debian

## Status

Greenfield rework, Phase 0 (scaffold) complete. Mesh feasibility verified against
`aiosendspin` 6.0.5 on hardware (see `docs/ARCHITECTURE.md` §7).

---

**Maintainer**: Plum Solutions
