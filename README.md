# Plum-Audio

> Synchronized multi-room audio for Raspberry Pi, built on the Sendspin protocol.

Plum-Audio turns a set of Raspberry Pis into a synchronized multi-room audio system. Each unit is a
**receiver** — it accepts AirPlay, Spotify Connect and Bluetooth from your phone or laptop — and a
**speaker**, and any source can be routed to any set of speakers across the mesh, in any combination,
at the same time.

Send AirPlay to the kitchen from your phone while Spotify plays in the living room, then pull the
kitchen speaker into the living room group without either stream stopping. There is no central
server: every unit runs its own, and they find each other on the LAN.

Album art, metadata and the visualizer travel **out-of-band** on their own protocol roles rather than
riding the audio stream, so artwork changes and track updates cannot disturb playback.

## Features

- **Multi-room sync** — sample-accurate playback across units via [Sendspin](https://www.sendspin-audio.com/spec/),
  an Open Home Foundation protocol.
- **Sources** — AirPlay, Spotify Connect and Bluetooth A2DP, each supporting multiple named
  endpoints per unit.
- **Cross-routing** — move a speaker to any source on any unit. Re-routing within a unit is live;
  moving between units is a reconnect the jitter buffer covers, so it is inaudible.
- **Groups** — multiple concurrent groups, per-speaker and per-group volume, plus source-level
  volume (the sending device's own level).
- **Web GUI per unit** — served from the unit itself on port 80. Now-playing with artwork, an
  audio-reactive spectrum visualizer, optional album-art UI theming, and full settings.
- **Interoperable** — advertises over mDNS as a standard Sendspin server and player, so
  [Music Assistant](https://music-assistant.io/) and other Sendspin controllers can drive it.
- **Headless units** — a unit with no sound card still works as an ingest and routing node.
- **One container per unit** — everything runs inside it.

## Requirements

- **Raspberry Pi 4** (or better) running **Debian 13 "trixie"** — 64-bit Raspberry Pi OS Lite is the
  tested base. Debian 13 specifically: `bluez-alsa` and `shairport-sync` differ meaningfully on 12.
- **Docker** with **host networking**. mDNS needs layer 2, so bridged networking will not work, and
  all units must be on the same subnet to discover each other.
- The **host's** Avahi and D-Bus (the container uses them rather than running its own).
- An audio output — the Pi's 3.5 mm jack, a HAT, or a USB DAC. Optional; see headless units above.
- Images are built for **`linux/arm64` only**. See [Other platforms](#other-platforms).

## Deploying to a fresh Pi

Everything is driven from your workstation. A Pi needs no copy of this repo and no registry
credentials.

**1. Flash and boot** Raspberry Pi OS Lite (64-bit, Debian 13). Enable SSH and create a user — the
scripts default to `plum-admin`. Give every unit a static address or a DHCP reservation.

**2. Tell the tooling about your units.**

```bash
git clone https://github.com/AnotherMike-exe/Plum-Audio.git
cd Plum-Audio

cp docker/units.conf.example docker/units.conf   # one line per unit; the file explains each column
echo 'PLUM_TEST_PW=<your pi password>' > docker/.deploy.env
```

Both files are gitignored. `units.conf` is where unit names, ids and audio devices are decided, and
its comments are worth reading — the ids are what the mesh routes on.

**3. Provision each Pi — once per SD card image, not per deploy.**

```bash
scripts/host-setup/provision.sh all --check   # report what is missing, change nothing
scripts/host-setup/provision.sh all           # rfkill, bluez config, D-Bus policies, host nginx
```

Two steps are opt-in because neither can be inferred:

- `--overlay <name> [--unity]` for an audio HAT. Boards without an ID EEPROM cannot be detected, so
  choosing the overlay is your call. Needs a reboot.
- `--with-bluez` builds a patched `bluetoothd` (~30 min) that polls AVRCP play status, which is what
  makes Bluetooth scrub position report correctly. Skip it if you do not need Bluetooth metadata.

A unit on the Pi's onboard 3.5 mm output needs neither.

**4. Build and deploy.**

```bash
docker/build.sh                # arm64 image -> dist/plum-audio-<tag>.tar.gz
docker/deploy.sh all           # every unit in units.conf
docker/deploy.sh 192.0.2.10    # or just one
```

`build.sh` produces a tarball rather than pushing to a registry; `deploy.sh` copies it over SSH and
`docker load`s it. On Apple Silicon the arm64 build is native — no QEMU.

**5. Open `http://<unit-ip>/`.** Add your sources under Settings → Integrations and pick an output
under Settings → Audio.

Re-run provisioning only after re-flashing a card. Redeploys are just steps 4 and 5, and they leave
`settings.json` and your Spotify authorizations intact.

### Using a prebuilt image

Released images are published to GitHub Container Registry, so you can skip `build.sh`:

```bash
docker pull ghcr.io/anothermike-exe/plum-audio:latest   # or :1.0.0, or :dev
```

`:latest` tracks the newest release, `:dev` the `dev` branch.

## Other platforms

Nothing in the application is Pi-specific, but the deployment tooling is, and **amd64 has never been
built or run**. To deploy Plum-Audio anywhere else, treat `docker/docker-compose.yml` as the
contract rather than using `deploy.sh`:

```bash
PLUM_PLATFORM=linux/amd64 docker/build.sh    # untested — expect to fix things
```

What any host must provide, whatever it is:

| Requirement | Why |
|---|---|
| **Host networking** | mDNS discovery is link-local; a bridge network breaks it. |
| **Debian 13 base** | `bluez-alsa` and `shairport-sync` package layouts are matched to it. |
| **Host D-Bus socket** and **host Avahi** | AirPlay metadata is MPRIS over D-Bus; mDNS goes through the system Avahi so the unit is discoverable by third-party controllers. |
| **`/dev/snd`** | Unless the unit is headless, in which case use the compose `headless` profile — Docker refuses to create a container whose `devices:` names a missing `/dev/snd`. |
| **`/proc/asound` bind-mounted at `/host/asound`** | It is masked inside a container, and card enumeration needs it. |
| **Persistent `/config` and `/data`** | Settings, Spotify credentials and player state. |

Ports used: **80** (GUI), **8927/8928** (Sendspin server/player), **8929** (mesh discovery),
**5001** (mesh API), **5002** (config API), **5050+** (AirPlay RAOP), **5354+/3678+** (Spotify).
The **host's** Avahi owns 5353. Full detail in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

Bluetooth additionally needs the host-side BlueZ configuration from
[docs/HOST-PROVISIONING.md](docs/HOST-PROVISIONING.md); none of it is installed by the container.

## How it works

```
Source (AirPlay / Spotify / Bluetooth)
  -> daemon -> FIFO -> PushStream feeder
  -> in-process Sendspin server (group + stream)
  -> Sendspin players: this unit's speaker, plus any roamed remote players

Metadata / artwork / visualizer -> separate Sendspin roles, off the audio path
```

The mesh model is **"servers stay, players roam."** Every unit runs both a server (ingesting its own
sources) and a player (its speaker). Cross-routing moves the *player* to another unit's server; audio
is never bridged between servers. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Documentation

| Doc | What it is for |
|---|---|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | Mesh model, process model, subsystem design |
| [OPERATIONS.md](docs/OPERATIONS.md) | Build, deploy, debug — including the deceptive failure modes |
| [HOST-PROVISIONING.md](docs/HOST-PROVISIONING.md) | Commissioning a new Pi, step by step |
| [HARD-WON-LESSONS.md](docs/HARD-WON-LESSONS.md) | Why the code is shaped this way. Read before "simplifying" |
| [SPEC-CONFORMANCE.md](docs/SPEC-CONFORMANCE.md) | Where we stand against the Sendspin spec |
| [UPSTREAM-AIOSENDSPIN.md](docs/UPSTREAM-AIOSENDSPIN.md) | Workarounds to delete when the pin bumps |
| [PHASE-HISTORY.md](docs/PHASE-HISTORY.md) | What shipped when, and what hardware proved it |
| [TESTING.md](docs/TESTING.md) | Test tiers, and what is not yet reproducible |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Branches, commits, versioning, style |

## Tech stack

- **Backend** — Python 3.13, [`aiosendspin`](https://github.com/Sendspin/aiosendspin) (pinned 6.0.5),
  PyAV, NumPy, Flask + aiohttp, supervisord
- **Frontend** — React 19, TypeScript 5, Vite 6, served by nginx inside the container
- **Base image** — `python:3.13-slim-trixie`. glibc rather than Alpine, deliberately: PyAV, PortAudio
  and NumPy all have trivial wheels there

## Status

**1.0.0.** AirPlay, Spotify and Bluetooth are hardware-validated across four units, as are the mesh
(discovery, aggregation, roaming, multiple groups, per-player volume), interop against Music
Assistant, the container build, and audio output selection.

Known gaps, stated plainly:

- **DLNA and Plexamp have no backend.** Settings stubs and GUI scaffolding exist; nothing is wired up.
- **amd64 has never been built.**
- **The APIs are unauthenticated.** Both bind `0.0.0.0` with permissive CORS. Any page on your LAN can
  change a unit's settings. Run this on a trusted network.
- Remaining spec gaps are tracked in [SPEC-CONFORMANCE.md](docs/SPEC-CONFORMANCE.md).

## Acknowledgements

Plum-Audio is mostly integration work. It stands on:

- **[Sendspin](https://www.sendspin-audio.com/spec/)** and **[`aiosendspin`](https://github.com/Sendspin/aiosendspin)**
  ([Open Home Foundation](https://github.com/Sendspin)) — the synchronization protocol and server
  library. This project is a consumer of the spec, not affiliated with it.
- **[shairport-sync](https://github.com/mikebrady/shairport-sync)** by Mike Brady — AirPlay
  reception, and the MPRIS interface the metadata comes from.
- **[go-librespot](https://github.com/devgianlu/go-librespot)** by devgianlu — Spotify Connect.
- **[BlueZ](http://www.bluez.org/)** and **[bluez-alsa](https://github.com/arkq/bluez-alsa)** by
  Arkadiusz Bokowy — Bluetooth A2DP and AVRCP.
- **[PyAV](https://github.com/PyAV-Org/PyAV)**, **[PortAudio](https://www.portaudio.com/)** /
  `sounddevice`, **[NumPy](https://numpy.org/)**, **[Pillow](https://python-pillow.org/)** — the audio
  and image pipeline.
- **[ColorThief](https://github.com/lokesh/color-thief)** and
  **[react-colorful](https://github.com/omgovich/react-colorful)** — album-art theming.
- **[Snapcast](https://github.com/badaix/snapcast)** by Johannes Pohl — the sync engine behind
  Plum-Snapcast, this project's predecessor. Plum-Audio replaces it with Sendspin, but the source
  integrations and GUI were ported from that work rather than rewritten, and the architecture it
  taught is throughout this one.

Each of these keeps its own license; they are invoked as separate processes or linked by their own
terms, not relicensed here.

## License

[GNU General Public License v3.0](LICENSE).
