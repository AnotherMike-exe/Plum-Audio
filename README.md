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
- **Each Pi needs a route to the internet for its first deploy** — `deploy.sh` installs Docker over
  `apt` on a fresh card. After that the units only need each other and your LAN.

### On your workstation

Only for the fleet path. Commissioning a single Pi with `scripts/plum-init.sh` needs none of this —
it runs on the Pi and pulls a published image.

| Need | Why |
|---|---|
| Docker | builds the image. On Apple Silicon the arm64 build is native; on an Intel Mac or x86 Linux you must register QEMU/binfmt first or `build.sh` fails with an exec-format error |
| `git`, `bash`, `ssh`/`scp` | the tooling is shell scripts driven over SSH |
| **`sshpass`** | every script authenticates non-interactively and refuses to start without it |

`sshpass` is no longer in homebrew-core (it was removed as a security risk), so the obvious
`brew install sshpass` fails. On macOS use:

```bash
brew tap hudochenkov/sshpass
brew install sshpass
```

## Deploying

Two paths, and they produce the same unit. Pick by how many Pis you have in front of you.

| | [One Pi](#one-pi-from-the-pi-itself) | [A fleet](#a-fleet-from-your-workstation) |
|---|---|---|
| Runs on | the Pi | your workstation, over SSH |
| You supply | the device name | a table of every unit |
| Needs a checkout of this repo | no | yes |
| Needs `sshpass` | no | yes |
| Image comes from | the public registry | your own `build.sh`, or the registry |

### One Pi, from the Pi itself

The device name is the only thing you choose. The unit derives its own mesh id from its hostname,
detects its own sound card, and writes its own config.

**1. Flash and boot** Raspberry Pi OS Lite (64-bit, Debian 13).

- **Enable SSH and create a user** in Pi Imager, so you can get a shell.
- **Set a unique hostname per unit.** The unit derives its mesh id from it. Two Pis both called
  `raspberrypi` claim one id, and two units on one id corrupt each other's routing.
- **Configure WiFi in Imager if the unit has no ethernet** — a headless Pi with neither never
  appears on the network.
- Give the unit a **static address or a DHCP reservation**.

**2. Run the installer on the Pi.** It needs internet for this run, to install Docker and pull the
image.

```bash
curl -fsSLO https://raw.githubusercontent.com/AnotherMike-exe/Plum-Audio/main/scripts/plum-init.sh
chmod +x plum-init.sh
sudo ./plum-init.sh "Kitchen"
```

Allow about 10 minutes on a fresh card. Most of that is the Docker install and the image pull. Add
`--check` first if you want a report of the host and no changes.

> **Testing an unreleased branch.** The `curl` line above and the default `:latest` image both track
> `main`, which is the last release. Do not mix a branch script with a `main` image: they can differ
> by an `aiosendspin` major, and two units across such a split cannot mesh at all.
>
> Every push to `dev` publishes `:dev` from that same commit, so pull it rather than building:
>
> ```bash
> scp scripts/plum-init.sh plum-admin@<pi>:          # the script is not on main yet
> ssh plum-admin@<pi>
> sudo ./plum-init.sh "Kitchen" --image ghcr.io/anothermike-exe/plum-audio:dev
> ```
>
> That image carries the host-setup payload, so nothing is fetched from GitHub. Use `--tarball` with
> a local `docker/build.sh` only for work you have not pushed, or for a unit with no internet.

**3. Write down the fleet pairing secret it prints.** The first unit mints one. Every later unit
must get the same value, or the units cannot pair with each other's speakers:

```bash
sudo ./plum-init.sh "Living Room" --fleet-psk <the value the first unit printed>
```

Adding a unit later and no longer have the value? Any running unit still holds it. Point the new
unit at one and it copies the secret over ssh:

```bash
sudo ./plum-init.sh "Bedroom" --fleet-psk-from 192.0.2.10
```

**4. Open `http://<unit-ip>/`.** Add your sources under Settings → Integrations. The audio output is
already picked, and Settings → Audio is where you change it.

What it decides for you, and how to override each one:

| Decision | How | Override |
|---|---|---|
| Mesh unit id | from the hostname, or kept as-is on a re-run | set the hostname in Imager |
| Audio output | reads the Pi's cards, preferring a HAT or USB DAC over the onboard jack over HDMI | `--output <PortAudio name fragment>` |
| Audio or headless | headless when the Pi has no sound card | `--output none` forces headless |
| Which image | `ghcr.io/anothermike-exe/plum-audio:latest` | `--image <ref>` or `--tarball <file>` |
| Fleet secret | mints one on the first unit | `--fleet-psk <value>` |

Two things stay opt-in, because neither can be inferred:

- **An audio HAT** needs the overlay and, separately, unity gain. These are **two passes with a
  reboot between them** — the second needs the card to exist, and the card does not exist until the
  overlay has been applied and the Pi rebooted:

  ```bash
  sudo ./plum-init.sh "Kitchen" --overlay hifiberry-amp100
  sudo reboot                              # the script does NOT reboot for you
  sudo ./plum-init.sh "Kitchen" --unity
  ```

  Run together they silently do nothing useful: `--unity` finds no card, and the HAT is left ~22 dB
  quiet with every volume slider reading correctly.
- `--with-bluez` builds a patched `bluetoothd` (~30 min) that polls AVRCP play status, which is what
  makes Bluetooth scrub position report correctly. Skip it if you do not need Bluetooth metadata.

A unit on the Pi's onboard 3.5 mm output needs neither.

The script is re-runnable. A second run keeps `/opt/plum-audio/{config,data}`, so `settings.json`,
your Spotify authorizations and this unit's mesh identity all survive. Re-run it to move a unit to
a new image.

> The script reads its host-setup files out of the image it pulls, so a Pi needs no copy of this
> repo. An image built before the installer existed does not carry them, and it falls back to
> fetching them from GitHub. If the Pi can reach neither, point it at a checkout with
> `--payload-dir <path>`.

### A fleet, from your workstation

Use this to deploy several units together, or to run an image you built yourself. Everything is
driven from your workstation over SSH. A Pi needs no copy of this repo, but it does need internet
access the first time, to install Docker.

**1. Flash and boot** every Pi, exactly as in step 1 above. The scripts default to the user
`plum-admin`; if you use another name, set `PLUM_TEST_USER` in step 2 — nothing else will tell you
why every connection is refused. Put every unit on **one subnet**: mDNS is link-local, so units on
different VLANs cannot discover each other.

**2. Describe your fleet — on your workstation, once for all units.**

Nothing in this step touches a Pi. You are writing two files on the machine you deploy *from*, and
you write them one time, not once per unit.

```bash
git clone https://github.com/AnotherMike-exe/Plum-Audio.git
cd Plum-Audio

cp docker/units.conf.example docker/units.conf   # then edit: one line per unit

printf 'PLUM_TEST_USER=%s\nPLUM_TEST_PW=%s\n' 'plum-admin' '<your pi password>' > docker/.deploy.env
chmod 600 docker/.deploy.env
```

Both files are gitignored. `units.conf` is two columns, and only one of them is a choice:

```
192.0.2.10  | Kitchen
192.0.2.11  | Living Room
192.0.2.12  | Office      | snd_rpi_hifiberry_dacplus
```

| Column | What |
|---|---|
| host | The unit's IP address or hostname. |
| name | What the unit is called. Keep names unique — a duplicate is suffixed with the Pi's SoC serial and warned about, not refused. |
| audio output | **Optional.** Left out, `deploy.sh` reads the unit's cards and picks one. It is a PortAudio name fragment, not an ALSA address. `none` makes the unit headless. |

The ids this table used to carry are gone. The container derives the unit id from the Pi's hostname
and the player id from the unit id, and `deploy.sh` keeps whatever id a unit is already running
under — so a redeploy never renames a live unit. A six-column file still works, with a warning.

> **Write `.deploy.env` once.** The first `deploy.sh` run mints a fleet pairing secret and
> **appends** `PLUM_FLEET_PSK=…` to this file. Every unit must share it. To fix a typo, edit the
> file — never re-run the `>` redirect above, which would truncate the secret away.
>
> Losing this file does not lose the secret: every unit holds a copy, and `deploy.sh` asks them for
> it before it will mint a new one. If any unit is unreachable at that moment it refuses to mint at
> all, because a second secret splits the fleet without ever reporting an error. Full detail:
> [docs/SENDSPIN-PAIRING.md](docs/SENDSPIN-PAIRING.md#losing-the-fleet-secret).
>
> If your password contains a space, `$`, `'` or `#`, quote it: the file is `source`d.

**3. Provision each Pi — once per SD card image, not per deploy.**
Full detail and the by-hand equivalent of every step: [docs/HOST-PROVISIONING.md](docs/HOST-PROVISIONING.md).

```bash
scripts/host-setup/provision.sh all --check   # report what is missing, change nothing
scripts/host-setup/provision.sh all           # rfkill, bluez config, D-Bus policies, host nginx
```

`all` means every row in *your* `units.conf`. The audio HAT and the patched `bluetoothd` are opt-in
here for the same reasons, and with the same two-pass reboot rule, as the single-Pi path above:

```bash
scripts/host-setup/provision.sh <ip> --overlay hifiberry-amp100
ssh plum-admin@<ip> sudo reboot          # the script does NOT reboot for you
scripts/host-setup/provision.sh <ip> --unity
scripts/host-setup/provision.sh all --with-bluez     # ~30 min per unit
```

**4. Build and deploy.**

```bash
docker/build.sh                # arm64 image -> dist/plum-audio-<tag>-arm64.tar.gz
docker/deploy.sh all           # every unit in units.conf
docker/deploy.sh 192.0.2.10    # or just one
```

`build.sh` produces a tarball rather than pushing to a registry. `deploy.sh` copies it over SSH and
`docker load`s it. On Apple Silicon the arm64 build is native — no QEMU. To deploy a published
image instead of building one, use `docker/deploy.sh all --pull`.

**5. Open `http://<unit-ip>/`.** Add your sources under Settings → Integrations and check the output
under Settings → Audio. A freshly deployed unit offers **AirPlay only**, with Spotify and Bluetooth
switched off until you configure them — that is correct, not a fault. What a greenfield unit does
and does not start with is in
[docs/OPERATIONS.md](docs/OPERATIONS.md#what-a-greenfield-unit-actually-offers).

Re-run provisioning only after re-flashing a card. Redeploys are just steps 4 and 5, and they leave
`settings.json` and your Spotify authorizations intact.

### Confirm the units can see each other

Do this whichever path you took. It is the part neither script can verify: each one checks a unit in
isolation, so a unit can pass every check and still be alone on the network.

```bash
curl -s http://<unit-a-ip>/api/mesh/view \
  | python3 -c 'import json,sys; print([u["unit_id"] for u in json.load(sys.stdin)["units"]])'
```

Every unit should be listed, and asking another unit should give the same answer. Allow ~10 seconds:
peers announce on a 2 s beacon with an 8 s expiry, so a unit that has just booted takes a moment to
appear. If only one is listed, they are not on the same layer-2 segment. mDNS will not cross a VLAN
boundary.

### Which image you get

Released images are published to GitHub Container Registry:

```bash
docker pull ghcr.io/anothermike-exe/plum-audio:latest   # or :1.0.0, or :dev
```

`:latest` tracks the newest release, `:dev` the `dev` branch. `plum-init.sh` pulls `:latest` unless
`--image` says otherwise. `deploy.sh` deploys your own local build unless you pass `--pull` or
`--image`.

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
| [OPEN-ITEMS.md](docs/OPEN-ITEMS.md) | Known gaps, deferred calls, and resolved-with-history |
| [SPEC-CONFORMANCE.md](docs/SPEC-CONFORMANCE.md) | Where we stand against the Sendspin spec |
| [SENDSPIN-PAIRING.md](docs/SENDSPIN-PAIRING.md) | Encryption/pairing: what we do instead, and what it costs |
| [ROUTING-MODEL.md](docs/ROUTING-MODEL.md) | **Proposal** — unified attach/detach and the true-none rule |
| [VOLUME-CALIBRATION.md](docs/VOLUME-CALIBRATION.md) | Per-endpoint loudness curves and matched multi-room volume |
| [UPSTREAM-AIOSENDSPIN.md](docs/UPSTREAM-AIOSENDSPIN.md) | Workarounds to delete when the pin bumps |
| [AIOSENDSPIN-BUMP-SCOPE.md](docs/AIOSENDSPIN-BUMP-SCOPE.md) | 6.0.5 → 9.1.0 scoped, and why the pin is on hold |
| [PHASE-HISTORY.md](docs/PHASE-HISTORY.md) | What shipped when, and what hardware proved it |
| [TESTING.md](docs/TESTING.md) | Test tiers, and what is not yet reproducible |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Branches, commits, versioning, style |

## Tech stack

- **Backend** — Python 3.13, [`aiosendspin`](https://github.com/Sendspin/aiosendspin) (pinned 9.1.0),
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
