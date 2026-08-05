# Host provisioning — commissioning a new unit

> Everything that must be true on the **host** before the container can work. Done once per Pi, in
> this order. Read it when adding a unit to the rig, or when a working feature is inexplicably dead
> on one unit only. The daily build/deploy/debug loop is docs/OPERATIONS.md.

## Why any of this is on the host

The container is one image per unit and deliberately runs **no** Avahi, **no** system D-Bus daemon
and **no** `bluetoothd`. Those live on the host and are reached through mounted sockets
(`/var/run/dbus`, `/var/run/avahi-daemon`) under host networking. The split is not stylistic —
nothing in the container can substitute for any of it:

- **The kernel owns the device tree.** `config.txt` is read by the bootloader long before Docker
  exists, so an undeclared audio HAT does not exist to `aplay -l`, to PortAudio, or to the picker.
- **The host owns the radio and the AVCTP channel**, so the AVRCP patches have to be applied there;
  a second `bluetoothd` would only fight the host's for `hci0`.
- **D-Bus policy is read by the system bus**, which is the host's.
- **The host owns the mDNS responder.** A second one is the exact UDP 5353 collision
  `start_server(advertise_addresses=[])` exists to avoid.
- **NetworkManager owns `wlan0`** — WiFi was a host concern in Plum-Snapcast and stays one.

## 1. Audio HAT — `scripts/host-setup/configure-audio-hat.sh`

Raspberry Pi OS does not auto-detect audio HATs, and the boards on this rig expose no ID EEPROM
(`/proc/device-tree/hat` does not exist on the Amp100), so there is no auto-detect to fall back on —
choosing the overlay is the operator's job.

```bash
sudo ./configure-audio-hat.sh --list          # supported overlays
sudo ./configure-audio-hat.sh --detect        # what is fitted / configured right now
sudo ./configure-audio-hat.sh --overlay hifiberry-amp100
# reboot
sudo ./configure-audio-hat.sh --unity         # mixer only; no reboot needed
docker restart plum-audio                     # so the picker re-enumerates
```

It writes an idempotent, marker-delimited block into `/boot/firmware/config.txt` (or
`/boot/config.txt`), comments out a conflicting `dtparam=audio=on` and any curated audio overlay it
did not write — the adoption case, since Plum-Snapcast's README told people to add
`dtoverlay=hifiberry-amp100` by hand — and keeps a `.plum.bak`. `--revert` is exact.

**Position in config.txt matters — the block goes BEFORE the first existing `dtoverlay=` line.**
Appending it after `dtoverlay=vc4-kms-v3d` costs an HDMI audio output: measured on `.7.204`, the
overlay before `vc4-kms-v3d` enumerates `vc4hdmi0`, `vc4hdmi1` and the HAT (3 boots), while after it
enumerates only `vc4hdmi1` and the HAT (2 boots) — `vc4hdmi0` never appears. The HAT works either
way, which is exactly why this would have shipped unnoticed on a unit nobody plugs a display into.

### `--unity`, and why it is not optional on a HAT

A HAT's hardware mixer does not default to unity, and `alsa-restore` reinstates it from
`/var/lib/alsa/asound.state` at **every** boot. A HiFiBerry Amp100 measured on 2026-08-04 came up
with `Digital` at 163/207 — **−22 dB**. Nothing in Plum-Audio can see or correct that: the player
applies volume as software gain in the PortAudio callback, so the hardware control is never touched
and the attenuation just sits there while every slider in the mesh reads correct. `--unity` pins the
control (first match of `Digital`, `PCM`, `Master`, `Playback`, `Speaker`, `Analogue`) and persists
it with `alsactl store`. Plum-Snapcast never hit this because `snapclient` was launched with
`--mixer hardware:${AUDIO_MIXER_NAME}` and therefore **owned** the control — there is no `amixer` or
`alsactl` call anywhere in that repo.

**On a power amplifier `--unity` makes the unit louder, potentially a lot louder, the instant it is
applied.** Do it before setting listening levels.

> **Unverified paths — flag before relying on them.** `--keep-onboard` and the no-`dtoverlay`
> fallback (a `config.txt` with no `dtoverlay=` line at all, where the block goes at EOF) have
> **never run on real hardware**. Both are unit-tested against `config.txt` fixtures in
> `tests/Unit/test_configure_audio_hat.py`; the main path is verified across four reboots on
> `.7.204`. A unit with no HAT has never been through this script. Related: `.7.204` has no 3.5 mm
> jack because the block sets `dtparam=audio=off` — correct and deliberate; re-run with
> `--keep-onboard` if the jack should be listed alongside the HAT.

Card **numbers** are not stable and nothing depends on them — Plum-Audio persists the ALSA card
*name*. On `.7.204` the HiFiBerry was card 2, then 1, then 2, then 0 across four reboots with the
config unchanged. Do not "fix" anything that references `hw:N,M`.

## 2. rfkill

```bash
sudo rfkill unblock bluetooth
```

BlueZ **will not clear a soft block itself**. While the adapter is blocked, setting `Powered = true`
fails with a bare `"Failed"` and no explanation, and we cannot fix it from the container (it would
need `/dev/rfkill` plus `CAP_NET_ADMIN`). `bluetooth_adapter.py` names rfkill in the error it logs,
which is the only hint you get.

## 3. Patched `bluetoothd` — `backend/config/bluez/install_patched_bluez.sh`

```bash
sudo ./install_patched_bluez.sh          # build + install + apt-mark hold
sudo ./install_patched_bluez.sh --revert # unhold + restore the distro package
```

Rebuilds the **distro** source package for the version already installed (keeping Raspberry Pi's own
`+rptN` patches) at `<version>+plumN`, installs the `bluez` binary package and holds it so an apt
upgrade cannot quietly restore the unpatched daemon. Idempotent — re-running with the same patches
and base version exits early unless `--force`.

On stock BlueZ a mid-track scrub on a connected phone **can never reach us**, and not because of
anything in our code. Both AVRCP mechanisms for reporting position are switched off:

- `0001-avrcp-poll-getplaystatus-while-playing.patch` — upstream issues `GetPlayStatus` (PDU 0x30,
  the only *measured* position) only from the GetCapabilities response, a status change, a track
  change and the media-player-list parse, and exposes no D-Bus method that triggers one. The patch
  adds a 2 s poll while the controller player reports playing. It works even against a target that
  never advertises `EVENT_PLAYBACK_POS_CHANGED` (0x05) — which iOS commonly does not — and it also
  corrects `MediaPlayer1.Position`'s unclamped wall-clock interpolation drift (observed: 454520 ms
  reported on a 400346 ms track).
- `0002-avrcp-register-position-with-1s-interval.patch` — upstream registers position-changed with
  an interval of `UINT32_MAX / 1000`, i.e. **49.7 days**. That also disables *seek* detection,
  because targets size their jump-detection window from the same interval (AOSP notifies when
  position leaves `[pos ± interval]`), so an in-track scrub inside a 49-day window looks like no
  change at all. 1 s restores both. Last in series because it is the droppable half — it only helps
  targets that do advertise 0x05.

**Nothing in our Python depends on the patches.** `_apply_position_signal` compares an incoming
position against our own anchor and discards a re-read that merely confirms it, so an unpatched unit
behaves exactly as before — it just loses scrub reporting. Verified: 8 polls / 12 s, 4 Position
signals / 9 s, scrubs landing within ~2 s (`t2_bt_avrcp_position.sh`, 2026-07-29).

Two Pi-specific traps, both paid for on 2026-07-29: **do not build at `-j$(nproc)`** (a 4-core LTO
build browned out the 5 V rail on `.7.122` and the board reset ~10 min in — hence 2 jobs by
default), and **do not log to `/tmp`** (a 1.9 GB tmpfs on Debian 13, so the reset takes the log with
it, exactly when you need it). Budget ~30 min on a Pi 4 and ~1.5 GB, both reclaimed on success.

### Cover art needs BlueZ ≥ 5.81 *and* `Experimental = true`

Two preconditions for cover art that live nowhere but a code comment
(`sources/bluetooth_coverart.py:5-12`), and each fails by producing no art and no error:

- **BlueZ ≥ 5.81.** `MediaPlayer1.ObexPort` simply does not exist before it — Plum-Snapcast ran 5.70,
  which is why its cover-art path never worked once. Debian 13 ships new enough; check anyway with
  `bluetoothd --version`.
- **`Experimental = true` in `/etc/bluetooth/main.conf`**, or `ObexPort` stays hidden even on 5.81.

```bash
bluetoothd --version                                     # must be >= 5.81
grep -q '^Experimental = true' /etc/bluetooth/main.conf || \
  sudo sed -i 's/^#*Experimental *=.*/Experimental = true/' /etc/bluetooth/main.conf
sudo systemctl restart bluetooth
```

Without either, everything else about Bluetooth works — audio, metadata, transport, scrub — and only
the cover never appears. There is no log line, because we never get as far as asking.

## 4. Mask the distro's user obexd

```bash
systemctl --user mask obex.service
```

A phone serves **one** AVRCP BIP (cover art) session at a time. The distro's D-Bus-activated user
`obexd` steals the channel and ours is refused with `ECONNREFUSED` — with nothing in the log,
because we never got to ask. Same class as disabling `bluealsa-aplay.service`.

## 5. Install the bluealsa D-Bus policy

```bash
sudo cp backend/config/bluealsa-plum-dbus.conf /etc/dbus-1/system.d/
sudo systemctl reload dbus     # or reboot
```

**Nothing installs this automatically.** It is in the repo and a new unit needs it copied by hand,
alongside the bluez work above.

Its absence is catastrophic but silent. Debian's own policy
(`/usr/share/dbus-1/system.d/bluealsa.conf`) grants `own_prefix="org.bluealsa"` to `user="root"`
only — group `audio` may *send* to it but not own it — and `bluetooth_manager.py` spawns the daemon
as our user. So `bluealsa` cannot acquire `org.bluealsa`, exits `rc=1` about 3 s after every start,
and the source manager respawns it forever. **On `.7.204` that was 178 restarts and a new
`dbus-daemon` every 9.5 s**, and enough log spam to bury unrelated diagnosis — see the `tail`
warning in docs/OPERATIONS.md. The one line that names it:

```
E: main.c:137: Couldn't acquire D-Bus name. Please check D-Bus configuration.
               Requested name: org.bluealsa
```

Scoped to `group="audio"` rather than a username on purpose: that group covers `plum-admin` on the
Pi rigs *and* the container's PUID user, which already has to be in `audio` to open `/dev/snd`. The
`user="root"` block covers the container running the daemon as root.

`backend/config/shairport-sync-dbus.conf` is the AirPlay-MPRIS equivalent on the **system** bus, and
is **not** needed for the container: each AirPlay endpoint gets a private session bus instead,
because shairport's MPRIS name is fixed and endpoints would collide on a shared one. Install it only
on a host still running the `~/plum-test` dev stack, and match its `user=` to the user shairport
runs as or D-Bus denies name ownership.

## 6. Stand down the host nginx

```bash
sudo systemctl disable --now nginx
```

`deploy.sh` does this itself, but do it before a first deploy if the unit ever served the
pre-container GUI. Under host networking the container's nginx crash-loops on `bind()` while the
host keeps answering :80 — a GUI that looks perfect and is a stale build. Config and webroot are
left on disk.

## Verify it took

```bash
# Audio
aplay -l                                  # the HAT is listed
amixer -c <n> sget Digital                # 100% / 0 dB, not 163/207
cat /proc/asound/cards                    # card numbers move — the name is what matters

# Bluetooth
dpkg-query -W -f='${Version}\n' bluez     # ends in +plumN
apt-mark showhold                         # bluez
rfkill list bluetooth                     # Soft blocked: no
busctl --system list | grep bluealsa      # org.bluealsa is owned
systemctl --user is-enabled obex.service  # masked

# Host services the container reaches over mounted sockets
systemctl is-active avahi-daemon bluetooth
systemctl is-enabled nginx                # disabled (or not installed)

# Then deploy and let it check the rest
docker/deploy.sh <host>
```

`deploy.sh`'s own preflight already warns on a stopped `avahi-daemon` or `bluetooth`, and on a
`bluez` without `+plum` in its version — but only as notes, since an unpatched unit still plays
audio.
