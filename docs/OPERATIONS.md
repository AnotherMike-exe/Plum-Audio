# Operations — build, deploy, debug

> The daily loop. Read this when a unit is misbehaving, or before pushing a new image to the rig.
> Commissioning a *new* Pi is docs/HOST-PROVISIONING.md — a different, once-per-unit job.
> The reasoning behind the failure modes named here lives in docs/HARD-WON-LESSONS.md.

## Build and deploy

```bash
docker/build.sh                     # linux/arm64 (native on Apple Silicon) -> dist/plum-audio-<tag>-arm64.tar.gz
docker/deploy.sh all                # every unit in docker/units.conf
docker/deploy.sh 192.0.2.10    # one unit
docker/deploy.sh all --tarball dist/plum-audio-6a64b88-arm64.tar.gz   # a specific build
```

There is no registry. `docker save | gzip -1` + scp + `docker load` beats standing one up for four
Pis on two VLANs, and `gzip -1` is the right trade for a LAN copy. The default tag is the short
commit, `-dirty` appended when the tree is not clean — so `docker images` on a unit answers "which
code is this?" without guessing. `PLUM_PLATFORM=linux/amd64` and `PLUM_TAG=` override.

`deploy.sh` runs, per unit: preflight → ensure Docker → stop the pre-container stack → create
`/opt/plum-audio` → import rig state (first deploy only) → load the image → install compose + a
generated per-unit env → `up -d` → verify.

**Host provisioning comes first, and is a separate job.** `scripts/host-setup/provision.sh all` —
once per Pi *image*, not per deploy. `deploy.sh` warns about an unpatched `bluez` and a stopped
`avahi-daemon`/`bluetooth`, but it does not warn about a missing bluealsa D-Bus policy or a
soft-blocked radio, and both of those fail silently later. See docs/HOST-PROVISIONING.md.

**First deploy vs every later one.** The import step is gated on `~/plum-test` existing *and*
`/opt/plum-audio/data/settings.json` being absent. On that one run it copies the unit's
`settings.json` (endpoints, device name, theme, audio output — the unit's whole identity) and the
`go-librespot`/`shairport`/`bluetooth` state directories, then deletes stale `*.socket`, `lockfile`
and `*.pid` artefacts that would make a fresh `dbus-daemon` refuse to bind. Afterwards it never
touches `/opt/plum-audio/{config,data}` again. **A rebuild is not a re-authorisation** — Spotify
endpoints keep their authorisation across any number of deploys. `--no-migrate` skips the import.

### A greenfield unit — no `~/plum-test` to import from

A re-imaged Pi has no dev stack, so the import is skipped and the container writes
`DEFAULT_SETTINGS`. What that means in practice, measured on the `.201` units on 2026-08-06:

- **One AirPlay endpoint, enabled**, named "Plum Audio" on RAOP 5050. Spotify and Bluetooth endpoints
  exist but are `enabled: false` — so a fresh unit is an AirPlay receiver and nothing else until
  someone opens Settings → Integrations.
- **`audio.output.device` is `null`**, which deliberately means "whatever `PLUM_DAC_DEVICE` says", so
  the player opens the DAC column from `units.conf` (`bcm2835` → PortAudio 0 → `hw:0,0`) and echoes
  the resolved card back as `Headphones:0`. Nothing needs choosing in the GUI for audio to work.
- **`deviceName` comes from `PLUM_UNIT_NAME`** (i.e. the unit-name column of `units.conf`) — but only
  since `1cd8701`. Before that every fresh unit came up as "Plum Sendspin", so a two-unit greenfield
  mesh showed one name twice in the mesh view, the unit cards and mDNS. On an image built before that
  commit, rename each unit in Settings → General.

Everything else about a greenfield deploy is identical, and `deploy.sh`'s own verify pass is
sufficient: four RUNNING programs, three APIs answering, 8927 and 8928 listening.

**A re-imaged unit presents a new SSH host key.** `deploy.sh` and `provision.sh` both pass
`UserKnownHostsFile=/dev/null`, because `StrictHostKeyChecking=no` alone does *not* override a
*conflicting* `known_hosts` entry — ssh disables password auth in that state, so every unit fails on
its first connection with a man-in-the-middle warning. If you reach a unit with your own `ssh`
instead, `ssh-keygen -R <host>` first.

**Reverting a unit to the dev stack** is `docker compose down` in `/opt/plum-audio` plus
`~/plum-test/run_*.sh`. The dev tree is left on disk; only its processes are stopped, so there is no
restore step.

Compose is reached two ways and that is deliberate: `.7.122` runs Docker CE from Docker's own repo
(`docker compose`, the CLI plugin), the Debian-packaged units get trixie's standalone
`docker-compose` 2.26. Same compose file; `deploy.sh` detects the invocation. Trixie has no
`docker-compose-v2` package — the name is `docker-compose` and it *is* v2.

### The two conflicts that fail deceptively

1. **The host's nginx.** It served the pre-container GUI from `/var/www/plum-audio` with the same
   proxy config the image now ships. Under host networking the container's nginx crash-loops on
   `bind()` while the host keeps answering :80 — so the GUI looks completely fine and serves a stale
   build. `deploy.sh` disables the host unit and treats any of 80/5001/5002/8927/8928/8929 still
   bound as a **hard failure**, not a warning.
2. **A SIGTERM-deaf shairport.** Once its private session bus is killed, shairport-sync traps
   SIGTERM and hangs in shutdown: `pkill` reports success, the process survives, and it still holds
   RAOP 5050. Endpoint ports are configurable so the port sweep cannot enumerate them — the deploy
   escalates every dev-stack pattern to `SIGKILL` unconditionally, then treats a survivor as fatal.

Readiness is checked on **supervisord's own view**, not on a port: under host networking a port can
be answered by something that is not this container, which is exactly how a stale host nginx passed
a GUI check.

## Debugging cookbook

```bash
docker logs plum-audio                                   # entrypoint: the DERIVED unit identity
docker exec plum-audio supervisorctl -c /app/supervisord/supervisord.conf status
docker exec plum-audio tail -f /config/logs/sendspin_server.log
docker exec plum-audio tail -f /config/logs/sendspin_player.log
docker exec plum-audio tail -f /config/logs/config_api.log
docker exec plum-audio tail -f /config/logs/nginx.log
docker exec plum-audio aplay -l
docker exec plum-audio python3 /app/scripts/audio_devices.py   # id / hw_id / availability / active
```

`/config/supervisord.log` is supervisord's own log and the first place to look when a program will
not stay up. Per-endpoint daemon logs live under `/data`, one directory per endpoint id:

```
/data/shairport/<id>/shairport-sync.log     /data/shairport/<id>/dbus.log
/data/go-librespot/<id>/go-librespot.log
/data/bluetooth/<id>/bluealsa.log           /data/bluetooth/<id>/obexd.log  obex-dbus.log
```

### supervisord runs four programs — or three on a unit with no output

`sendspin_server` (priority 10), `sendspin_player` (20), `config_api` (15) and `nginx` (20). Those
are the templates in `backend/supervisord/conf.d/`.

**The set that actually runs is composed at start-up**, into `/run/plum-supervisor.d`, which is what
supervisord's `[include]` reads. `entrypoint.sh` runs `output_gate.py` first and DELETES
`sendspin_player.ini` when this unit has no audio output — so such a unit has **three** programs and
that is correct, not a failed deploy. Deliberately an absent file rather than `autostart=false`:
supervisord does not expand `%(ENV_x)s` in `autostart`, and a STOPPED program is exactly what
`deploy.sh` treats as a failure.

To see what a unit decided, and why, without changing anything:

```bash
docker exec plum-audio python3 /app/scripts/output_gate.py --dry-run   # prints none|device
docker exec plum-audio cat /config/logs/output_gate.log                # and the reason
ls /run/plum-supervisor.d                                              # the composed program set
```

**Changing the output to or from "No output" needs a container restart.** Nothing applies it live —
that is the whole design, since the player either exists or does not. The GUI says so with an amber
banner rather than a spinner; if you see "Restart to apply", `docker compose restart` is the answer.
A device-to-device switch is unaffected and still applies live.

**Deploy the image to EVERY unit before making any unit playerless.** A peer running an older image
sends no `has_player` in its snapshot, which defaults to True — it would read the playerless leader as
idle and unroute its own followers, which is precisely the bug this feature fixes.

**The source daemons are NOT supervisord programs.** shairport-sync, go-librespot, bluealsa, obexd
and their private `dbus-daemon`s are spawned and reconciled by the source managers
(`sources/*_manager.py`) from `settings.json`, inside the audio event loop — that is what makes an
endpoint edit apply live instead of needing a `supervisorctl` round trip. So `ps` inside the
container is how you confirm they are up:

```bash
docker exec plum-audio ps -ef | grep -E 'shairport|go-librespot|bluealsa|obexd'
```

A source that is enabled in `settings.json` and missing from that list is a **manager** problem, not
a supervisord one — look in `sendspin_server.log`, not at `supervisorctl status`.

### Protocol-level debugging

Set `PLUM_LOG_LEVEL=DEBUG` in `/opt/plum-audio/plum-audio.env`, then
`docker compose up -d --force-recreate`. That turns on the aiosendspin handshake trace: `client/hello`
with the client's `supported_formats`, the `stream/start` we answer with, and the periodic
`Send summary role=player ... buf_ms(...)`. It is the only way to see what a third-party device
actually negotiated.

**Revert it afterwards.** It is verbose, and the force-recreate drops every connection — doing that
mid-test once produced a "bug" that was purely the restart.

### Watch what you conclude from a `tail`

A crash-looping source writes fast enough to push the lines you need thousands back, and a filtered
`tail` then reads as "this never happened". **Two wrong diagnoses on 2026-08-04 came from exactly
that** — the trigger was a bluealsa missing its D-Bus policy, respawning every ~9.5 s (see
docs/HOST-PROVISIONING.md). Check the restart count before trusting an absence.

### Comparing image versions across units

`.7.204` uses Docker's containerd image store; the other three use the classic one (`docker info` →
`driver=overlayfs` + `io.containerd.snapshotter.v1` vs `overlay2`). So
`docker inspect <container> --format '{{.Image}}'` reports the **manifest** digest there and the
**config** digest everywhere else, and the same image compares as two different ids across units —
which reads as a failed deploy. Either compare a unit's ids against *its own*
`docker image inspect plum-audio:<tag>`, or skip it and diff the served bundle:

```bash
curl -s http://<unit>/ | grep -o 'assets/index-[^"]*\.js'
```

## Ports

Every number below is from the code, not from the plan.

| Port | Proto | Who | Defined in |
|---|---|---|---|
| 80 | TCP | nginx — the built GUI, proxying `/api/{mesh,settings,integrations,audio}` | `nginx/plum-audio.conf` |
| 5001 | TCP | mesh API (aiohttp, in the audio loop) | `mesh/api.py` |
| 5002 | TCP | config API (Flask; settings/integrations/audio) | `apis/server.py` |
| 8927 | TCP | Sendspin **server** websocket | `sendspin_server.py` |
| 8928 | TCP | Sendspin **player** websocket | `sendspin_player.py` |
| 8929 | **UDP** | mesh beacon — broadcast to 255.255.255.255 every 2 s, peer TTL 8 s | `mesh/discovery.py` |
| 5050+ | TCP | AirPlay RAOP, `5050 + (id − 1)`; 10 endpoints max → 5050-5059 | `airplay_config.py` |
| 6001+ | UDP | AirPlay UDP block per endpoint, stride 10 → 6001, 6011, … | `airplay_config.py` |
| 5354+ | TCP | Spotify zeroconf, allocated `max(existing)+1`; 10 max → 5354-5363 | `integrations_api.py` |
| 3678+ | TCP | go-librespot control API, **loopback only**, `3678 + (id − 1)` | `spotify_config.py` |
| 5353 | UDP | mDNS — the **host's** Avahi, never ours | `mesh/avahi.py` |

Bluetooth (4 endpoints max) binds no TCP port; it rides the host's `bluetoothd` over the system
D-Bus and `bluealsa` over `org.bluealsa`.

**DLNA is not implemented, and 49494-49503 is not reserved by anything.** `grep` the backend and
what exists is a settings stub (`apis/settings_api.py:79`, `"dlna": {"endpoints": []}`), a mention
in the integrations API docstring, a `DLNAEndpoint` TypeScript interface, mocked
`/api/integrations/dlna/*` handlers in the frontend test suite, and a GUI card in
`IntegrationsTab.tsx` that calls `dlnaService` — which reaches endpoints **the backend never
registers** (`create_integrations_blueprint` wires airplay, spotify and bluetooth only). The card is
inert. gmrender-resurrect is not in the image. Plexamp is the same story: `PLEXAMP_ENABLED=0`, no
sidecar shipped.

Host networking is non-negotiable — mDNS/Avahi, the broadcast beacon and AirPlay's UDP blocks all
need L2 visibility, and the mesh discovers peers by real LAN address. `EXPOSE` in the Dockerfile is
documentation only.

## The Dockerfile's build-time assertions

```dockerfile
RUN shairport-sync -V | grep -q -- '-mpris' \
    && test -x /usr/bin/bluealsa \
    && test -x /usr/libexec/bluetooth/obexd
```

The base is `python:3.13-slim-**trixie**`, matching the units' Debian 13, because two integrations
depend on exactly what trixie ships: bluez-alsa 4.3.1-3 still installs its daemon as `bluealsa`
(upstream renamed it `bluealsad` in 4.0), and shairport-sync **4.3.7** is the MPRIS build
multi-endpoint AirPlay was verified against. Bookworm would silently hand over shairport 4.1 and a
different bluez-alsa.

The assertion exists because **every one of those failures is invisible at runtime**: no MPRIS means
AirPlay transport buttons that silently do nothing; a renamed `bluealsa` or a moved `obexd` means a
Bluetooth source that never appears, or cover art that never loads — with nothing in any log either
way. The build is the only cheap place to catch them. Do not switch the base to Alpine either: glibc
is what makes the PyAV / PortAudio / numpy wheels trivial.
