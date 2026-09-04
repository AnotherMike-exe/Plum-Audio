#!/usr/bin/env bash
# Plum-Audio — commission ONE unit, from the unit itself.
#
#   sudo ./plum-init.sh "Kitchen"                 # everything: host setup, image, config, start
#   sudo ./plum-init.sh "Kitchen" --check         # report what is missing, change nothing
#   sudo ./plum-init.sh "Kitchen" --fleet-psk XYZ # join a fleet that already exists
#
# The device name is the ONLY thing this needs. Everything else is derived from the unit:
# the unit id from the hostname, the player id from the unit id, the audio output from the cards
# the Pi actually has, and the compose profile from whether it has any.
#
# WHY THIS EXISTS, next to scripts/host-setup/provision.sh + docker/deploy.sh
#   Those two run from a WORKSTATION, over SSH, against docker/units.conf — a fleet table with a
#   password file beside it. That is the right shape for four units deployed together, and the
#   wrong shape for one Pi: it makes a single unit pay for sshpass, a units.conf row, a fleet
#   table it is the only member of, and a build of the image on another machine.
#
#   This script is the same work in the other direction. It runs ON the Pi, needs no workstation,
#   no repo checkout, no SSH and no units.conf, and pulls a published image instead of building
#   one. The host checklist it runs is provision.sh's, step for step, and the container it starts
#   is deploy.sh's. Where the two must agree, the comment says so.
#
# WHAT IT DOES, in order
#   1. preflight            — arm64, Debian 13, sudo, avahi + bluetoothd
#   2. docker + compose     — installs them if the image is fresh (needs internet, once)
#   3. the image            — pulls it, or loads a tarball named with --tarball
#   4. host setup           — rfkill, Experimental=true, obexd, the bluealsa D-Bus policy, nginx
#   5. output detection     — reads /proc/asound/cards and picks one, unless --output says otherwise
#   6. config               — /opt/plum-audio/{docker-compose.yml,plum-audio.env,.env}
#   7. up + verify          — supervisord, all three APIs, and that the player renders
#
# It is re-runnable. A second run keeps /opt/plum-audio/{config,data}, so settings.json and the
# Spotify authorizations survive, and it keeps this unit's identity — see PRESERVED below.
set -euo pipefail

REMOTE_ROOT="/opt/plum-audio"
DEFAULT_IMAGE="ghcr.io/anothermike-exe/plum-audio:latest"
# Where the payload falls back to when the image does not carry it — an image built before this
# script existed has no /app/host-setup. Pinned to a branch, not to `main`, only because a unit
# commissioned today must get the files that match the image it is running.
RAW_BASE="${PLUM_RAW_BASE:-https://raw.githubusercontent.com/AnotherMike-exe/Plum-Audio/main}"

NAME=""
IMAGE_REF="$DEFAULT_IMAGE"
TARBALL=""
FLEET_PSK="${PLUM_FLEET_PSK:-}"
FLEET_PSK_FROM=""
OUTPUT_SPEC=""
OVERLAY=""
DO_UNITY=0
WITH_BLUEZ=0
CHECK_ONLY=0
PAYLOAD_DIR=""

usage() {
    sed -n '2,30p' "$0"
    cat <<'USAGE'

Options
  --image REF        image to run (default: ghcr.io/anothermike-exe/plum-audio:latest)
  --tarball FILE     load this local image tarball instead of pulling
  --fleet-psk VALUE  the fleet pairing secret. Omit on the FIRST unit — one is minted and
                     printed. Give that printed value to every later unit.
  --fleet-psk-from HOST
                     copy the fleet pairing secret off a unit you already run, over ssh
                     ([user@]host). Use this when you no longer have the value on hand.
  --output SPEC      override the detected audio output. `none` makes this a headless unit
                     (it ingests and routes, and renders nothing itself).
  --overlay NAME     apply an audio HAT overlay, then REBOOT and run again with --unity
  --unity            pin the HAT mixer to unity gain. Only after the --overlay reboot.
  --with-bluez       rebuild bluetoothd for AVRCP scrub position (~30 minutes)
  --payload-dir DIR  a checkout of this repo, if the unit cannot reach GitHub
  --check            report only. Changes nothing.
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --image)       IMAGE_REF="$2"; shift 2 ;;
        --tarball)     TARBALL="$2"; shift 2 ;;
        --fleet-psk)   FLEET_PSK="$2"; shift 2 ;;
        --fleet-psk-from) FLEET_PSK_FROM="$2"; shift 2 ;;
        --output)      OUTPUT_SPEC="$2"; shift 2 ;;
        --overlay)     OVERLAY="$2"; shift 2 ;;
        --unity)       DO_UNITY=1; shift ;;
        --with-bluez)  WITH_BLUEZ=1; shift ;;
        --payload-dir) PAYLOAD_DIR="$2"; shift 2 ;;
        --check)       CHECK_ONLY=1; shift ;;
        -h|--help)     usage; exit 0 ;;
        -*)            echo "unknown flag $1" >&2; exit 2 ;;
        *)             [[ -z "$NAME" ]] && NAME="$1" || { echo "unexpected argument: $1" >&2; exit 2; }
                       shift ;;
    esac
done

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32mOK\033[0m   %-26s %s\n' "$1" "$2"; }
bad()  { printf '    \033[31mTODO\033[0m %-26s %s\n' "$1" "$2"; }
warn() { printf '\033[33m    !! %s\033[0m\n' "$*"; }
note() { printf '    \033[36m--\033[0m   %-26s %s\n' "$1" "$2"; }

# Root by sudo, not by demanding `sudo ./plum-init.sh`: the script writes /opt/plum-audio as the
# INVOKING user (PUID/PGID in plum-audio.env come from that account), so it has to know who that is.
# Running the whole script as root would make every file under /opt/plum-audio root-owned, which is
# exactly the Binhex contract this project keeps.
if [[ "$(id -u)" == 0 && -z "${SUDO_USER:-}" ]]; then
    warn "running as root with no SUDO_USER — /opt/plum-audio will be owned by root"
fi
# Prime the sudo credential ONCE, here, rather than per call. The obvious
# `sudo -n "$@" || sudo "$@"` runs the command a SECOND time whenever the first attempt fails for
# any reason at all — including a genuine failure of something that is not idempotent, like
# `docker load` or `docker rm`.
if [[ "$(id -u)" != 0 ]]; then
    sudo -v || { echo "sudo is required" >&2; exit 1; }
fi
s() { if [[ "$(id -u)" == 0 ]]; then "$@"; else sudo "$@"; fi; }

# `systemctl --user` talks to the CALLER's user manager, and under sudo that is root's — so masking
# the user obexd would mask it for root and leave the operator's own copy running, which is the one
# that steals the AVRCP cover-art channel. Target the invoking account explicitly.
user_systemctl() {
    if [[ -n "${SUDO_USER:-}" && "$(id -u)" == 0 ]]; then
        local uid; uid="$(id -u "$SUDO_USER")"
        sudo -u "$SUDO_USER" XDG_RUNTIME_DIR="/run/user/${uid}" systemctl --user "$@"
    else
        systemctl --user "$@"
    fi
}

[[ -n "$NAME" || "$CHECK_ONLY" == 1 ]] || { usage >&2; echo; echo "the device name is required" >&2; exit 2; }

# The name reaches shairport's libconfig and go-librespot's YAML, and shairport runs shell commands
# from `sessioncontrol` — so it is validated at every boundary in the backend. Refuse the obviously
# dangerous shapes HERE too, rather than writing them into plum-audio.env and letting the container
# be the first thing that objects.
if [[ -n "$NAME" ]] && [[ "$NAME" =~ [\`\$\"\\] || ${#NAME} -gt 48 ]]; then
    echo "device name: no backticks, \$, quotes or backslashes, and 48 characters at most" >&2
    exit 2
fi

# --- 1. preflight ---------------------------------------------------------------------------------

say "preflight"
ARCH="$(dpkg --print-architecture 2>/dev/null || echo unknown)"
note "host" "$(hostname) / ${ARCH} / $(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-unknown}")"
if [[ "$ARCH" != arm64 ]]; then
    echo "    !! this image is built for arm64 only, and this host is ${ARCH}" >&2
    exit 1
fi
s true || { echo "    !! sudo failed" >&2; exit 1; }

# The container talks to BOTH of these over the host's system D-Bus. Without them AirPlay and
# Spotify never advertise, and Bluetooth never sees the radio.
systemctl is-active --quiet avahi-daemon && ok "avahi-daemon" "active" || bad "avahi-daemon" "NOT active"
systemctl is-active --quiet bluetooth    && ok "bluetoothd"   "active" || bad "bluetoothd"   "NOT active"

# --- output detection ------------------------------------------------------------------------------
#
# The ONE value the operator used to have to supply, and it is derivable. It is also the cheapest
# thing to get wrong: PLUM_DAC_DEVICE is only what a unit BOOTS with, and Settings -> Audio
# overrides it permanently on first use (audio_devices.configured_output_spec). So a wrong guess
# costs two clicks in the GUI, not a redeploy — which is what makes detection the right default.
#
# The spec is a PortAudio name fragment, matched as a substring against PortAudio's own device
# names. It is NOT an ALSA address: card NUMBERS move between reboots and `hw:C,D` goes stale.
# The long name at the end of a /proc/asound/cards line is exactly the form already proven on this
# rig (`bcm2835`, `snd_rpi_hifiberry_dacplus`).
detect_output() {
    local line trimmed id longname rank best_rank=99 best=""
    # Two lines per card. The index line carries ` N [id  ]: driver - long name`; take the long name.
    # A glob, not a regex: a bracket inside [[ =~ ]] is a portability trap, and this has to survive
    # whatever /bin/sh-ism the operator pastes it into.
    while IFS= read -r line; do
        trimmed="${line#"${line%%[![:space:]]*}"}"
        case "$trimmed" in [0-9]*"["*) ;; *) continue ;; esac
        id="${trimmed#*[}"; id="${id%%]*}"; id="$(printf '%s' "$id" | tr -d ' ')"
        longname="${line##* - }"
        [[ -n "$longname" && "$longname" != "$line" ]] || longname="$id"
        # Rank, lowest wins. A HAT or USB DAC is what someone fitted ON PURPOSE, so it outranks the
        # onboard jack. HDMI is last: it is present on every Pi, it is almost never the intended
        # output for this, and PortAudio's own default frequently lands on it.
        case "$id$longname" in
            *vc4hdmi*|*HDMI*|*hdmi*)             rank=3 ;;
            *bcm2835*|*Headphones*|*headphones*) rank=2 ;;
            *)                                   rank=1 ;;
        esac
        if [[ "$rank" -lt "$best_rank" ]]; then best_rank="$rank"; best="$longname"; fi
        printf '      card %-18s %s\n' "$id" "$longname" >&2
    done < /proc/asound/cards
    printf '%s' "$best"
}

DETECTED=""
if [[ -r /proc/asound/cards ]]; then
    DETECTED="$(detect_output 2>/tmp/plum-cards.$$ || true)"
    [[ -s /tmp/plum-cards.$$ ]] && { echo "    cards:"; cat /tmp/plum-cards.$$; }
    rm -f /tmp/plum-cards.$$
fi

if [[ -n "$OUTPUT_SPEC" ]]; then
    note "audio output" "$OUTPUT_SPEC (from --output)"
elif [[ -n "$DETECTED" ]]; then
    OUTPUT_SPEC="$DETECTED"
    ok "audio output" "$OUTPUT_SPEC (detected — change it in Settings -> Audio)"
else
    OUTPUT_SPEC="none"
    note "audio output" "no sound cards — this unit will ingest and route only"
fi

# `none` is the one spec that is not a PortAudio fragment. It selects the headless compose profile,
# which has no /dev/snd — and the audio profile cannot even be CREATED without /dev/snd, so this is
# a hard fork in the deploy, not a runtime setting.
if [[ "$(printf '%s' "$OUTPUT_SPEC" | tr '[:upper:]' '[:lower:]')" == "none" ]]; then
    PROFILE="headless"; PLAYER_ENABLED=0; WANT_PROGRAMS=3
else
    PROFILE="audio";    PLAYER_ENABLED=1; WANT_PROGRAMS=4
fi

# --- the host report, which is also --check ---------------------------------------------------------
#
# Mirrors provision.sh's report_one. Keep the two in step: this is what an operator on the unit
# sees, and that is what an operator on the workstation sees, and they must not disagree.
host_report() {
    v="$(dpkg-query -W -f='${Version}' bluez 2>/dev/null || echo none)"
    case "$v" in
        *+plum*) ok  "bluez patches" "$v" ;;
        none)    bad "bluez" "not installed" ;;
        *)       bad "bluez patches" "$v is unpatched — no AVRCP position (--with-bluez)" ;;
    esac
    case "$v" in none) ;; *)
        if [[ "$(printf '5.81\n%s\n' "${v%%-*}" | sort -V | head -1)" == "5.81" ]]; then
            ok "bluez >= 5.81" "${v%%-*} (cover art possible)"
        else
            bad "bluez >= 5.81" "${v%%-*} has no MediaPlayer1.ObexPort — cover art can never work"
        fi ;;
    esac
    grep -qE '^Experimental *= *true' /etc/bluetooth/main.conf 2>/dev/null \
        && ok "Experimental" "true (ObexPort visible)" \
        || bad "Experimental" "not true — cover art fails silently"
    if s rfkill list bluetooth 2>/dev/null | grep -q 'Soft blocked: yes'; then
        bad "rfkill" "SOFT BLOCKED — BlueZ cannot power the adapter and will not clear it itself"
    else
        ok "rfkill" "not soft blocked"
    fi
    case "$(user_systemctl is-enabled obex.service 2>&1)" in
        masked)    ok   "user obexd" "masked" ;;
        not-found) note "user obexd" "not installed (Pi OS Lite) — nothing to mask" ;;
        *)         bad  "user obexd" "present and not masked — it steals the AVRCP cover-art channel" ;;
    esac
    [[ -f /etc/dbus-1/system.d/bluealsa-plum-dbus.conf ]] \
        && ok "bluealsa D-Bus policy" "installed" \
        || bad "bluealsa D-Bus policy" "MISSING — bluealsa cannot own org.bluealsa and respawns forever"
    case "$(systemctl is-enabled nginx 2>&1)" in
        not-found) note "host nginx" "not installed" ;;
        enabled)   bad  "host nginx" "enabled — it will hold :80 and serve a stale GUI" ;;
        *)         ok   "host nginx" "$(systemctl is-enabled nginx 2>&1)" ;;
    esac
    command -v docker >/dev/null && ok "docker" "$(docker --version 2>&1)" || bad "docker" "not installed"
    [[ -f "$REMOTE_ROOT/plum-audio.env" ]] \
        && note "existing unit" "$(grep -E '^PLUM_UNIT_(ID|NAME)=' "$REMOTE_ROOT/plum-audio.env" | tr '\n' ' ')" \
        || note "existing unit" "none — this is a first install"
}

say "host state"
host_report

if [[ "$CHECK_ONLY" == 1 ]]; then
    say "check only — nothing was changed"
    exit 0
fi

# --- 2. docker ------------------------------------------------------------------------------------
#
# Same detection deploy.sh uses: Debian trixie packages compose v2 as the standalone `docker-compose`
# binary, while Docker CE ships it as the `docker compose` CLI plugin. Same compose file either way,
# so detect the invocation rather than forcing one repo layout onto the unit.
say "docker"
need=()
command -v docker >/dev/null || need+=(docker.io)
if ! s docker compose version >/dev/null 2>&1 && ! command -v docker-compose >/dev/null 2>&1; then
    need+=(docker-compose)
fi
if [[ ${#need[@]} -gt 0 ]]; then
    echo "    installing: ${need[*]}  (this is the step that needs internet)"
    s apt-get update -qq
    s env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${need[@]}"
fi
s systemctl enable --now docker >/dev/null 2>&1 || true
s usermod -aG docker "${SUDO_USER:-$USER}" 2>/dev/null || true
if s docker compose version >/dev/null 2>&1; then DC="docker compose"; else DC="docker-compose"; fi
echo "    $(s docker --version 2>&1)  |  $(s $DC version 2>&1 | head -1)  [$DC]"

# --- 3. the image ---------------------------------------------------------------------------------

if [[ -n "$TARBALL" ]]; then
    say "loading $(basename "$TARBALL")"
    [[ -f "$TARBALL" ]] || { echo "    !! no such tarball: $TARBALL" >&2; exit 1; }
    s docker load -i "$TARBALL" | sed 's/^/    /'
    IMAGE_NAME="plum-audio"; IMAGE_TAG="latest"
else
    say "pulling ${IMAGE_REF}"
    # Pull explicitly rather than letting `compose up` do it. A registry failure here is reported
    # before anything is torn down, whereas compose pulling mid-`up` fails after the old container
    # has already gone.
    s docker pull "$IMAGE_REF" 2>&1 | sed 's/^/    /'
    if [[ "${IMAGE_REF##*/}" == *:* ]]; then
        IMAGE_NAME="${IMAGE_REF%:*}"; IMAGE_TAG="${IMAGE_REF##*:}"
    else
        IMAGE_NAME="$IMAGE_REF"; IMAGE_TAG="latest"
    fi
fi
IMAGE_FULL="${IMAGE_NAME}:${IMAGE_TAG}"
s docker image inspect "$IMAGE_FULL" --format '    {{.Id}}  {{.Architecture}}' || {
    echo "    !! ${IMAGE_FULL} is not present after load/pull" >&2; exit 1; }

# --- 4. host setup --------------------------------------------------------------------------------
#
# provision.sh pushes these files from a workstation checkout. Here they come out of the image,
# which is the same tree and is already on the unit — so there is one source of truth and no
# second thing to keep in step. An image built before this script existed does not carry
# /app/host-setup, so fall back to --payload-dir, then to GitHub.
PAYLOAD="/var/lib/plum-audio/host-setup"
s mkdir -p "$PAYLOAD/bluez"

fetch_payload() {  # fetch_payload <path-in-repo> <dest>
    local rel="$1" dest="$2" in_image=""
    # Where each file lands INSIDE the image. The Dockerfile copies backend/ to /app/, and
    # scripts/host-setup/ and the compose file are copied in beside it precisely so this script can
    # read them without a repo checkout.
    case "$rel" in
        backend/*)             in_image="/app/${rel#backend/}" ;;
        scripts/host-setup/*)  in_image="/app/host-setup/${rel#scripts/host-setup/}" ;;
        docker/*)              in_image="/app/${rel#docker/}" ;;
        *)                     in_image="/app/${rel}" ;;
    esac
    if [[ -n "$PAYLOAD_DIR" && -f "${PAYLOAD_DIR}/${rel}" ]]; then
        s cp "${PAYLOAD_DIR}/${rel}" "$dest"; return 0
    fi
    # `--entrypoint cat`, not `sh -c`: entrypoint.sh would run its whole setup for a file read.
    if s docker run --rm --entrypoint cat "$IMAGE_FULL" "$in_image" > /tmp/plum-payload.$$ 2>/dev/null \
       && [[ -s /tmp/plum-payload.$$ ]]; then
        s cp /tmp/plum-payload.$$ "$dest"; rm -f /tmp/plum-payload.$$; return 0
    fi
    rm -f /tmp/plum-payload.$$
    if curl -fsSL -m 30 "${RAW_BASE}/${rel}" -o /tmp/plum-payload.$$ 2>/dev/null; then
        s cp /tmp/plum-payload.$$ "$dest"; rm -f /tmp/plum-payload.$$; return 0
    fi
    rm -f /tmp/plum-payload.$$
    return 1
}

say "host setup"
if fetch_payload "backend/config/bluealsa-plum-dbus.conf" "$PAYLOAD/bluealsa-plum-dbus.conf"; then
    # Debian's own policy grants own_prefix=org.bluealsa to root ONLY, and the source manager spawns
    # the daemon as our user — so without this it exits rc=1 about 3 s after every start and is
    # respawned forever, burying every unrelated diagnosis under it.
    if s cmp -s "$PAYLOAD/bluealsa-plum-dbus.conf" /etc/dbus-1/system.d/bluealsa-plum-dbus.conf 2>/dev/null; then
        echo "    bluealsa D-Bus policy: already current"
    else
        echo "    bluealsa D-Bus policy: installing"
        s install -m 0644 "$PAYLOAD/bluealsa-plum-dbus.conf" /etc/dbus-1/system.d/
        s systemctl reload dbus
    fi
else
    warn "could not get the bluealsa D-Bus policy from the image, --payload-dir or GitHub"
    warn "Bluetooth will respawn forever until it is installed; everything else still works"
fi

RESTART_BT=0

# BlueZ answers a soft block with a bare "Failed" and cannot clear it from inside the container
# (that needs /dev/rfkill plus CAP_NET_ADMIN). systemd-rfkill persists this across reboots.
if s rfkill list bluetooth 2>/dev/null | grep -q 'Soft blocked: yes'; then
    echo "    rfkill: unblocking bluetooth"
    s rfkill unblock bluetooth
else
    echo "    rfkill: already unblocked"
fi

# Without Experimental, MediaPlayer1.ObexPort stays hidden even on bluez >= 5.81, and cover art
# fails with nothing in any log — because we never get as far as asking for it.
if grep -qE '^Experimental *= *true' /etc/bluetooth/main.conf 2>/dev/null; then
    echo "    main.conf: Experimental already true"
elif [[ -f /etc/bluetooth/main.conf ]]; then
    echo "    main.conf: setting Experimental = true"
    s cp -n /etc/bluetooth/main.conf /etc/bluetooth/main.conf.plum.bak
    if grep -qE '^#?Experimental *=' /etc/bluetooth/main.conf; then
        s sed -i 's/^#*Experimental *=.*/Experimental = true/' /etc/bluetooth/main.conf
    else
        s sed -i '0,/^\[General\]/s//[General]\nExperimental = true/' /etc/bluetooth/main.conf
    fi
    grep -qE '^Experimental *= *true' /etc/bluetooth/main.conf || warn "failed to set Experimental"
    RESTART_BT=1
else
    warn "/etc/bluetooth/main.conf does not exist — is bluez installed?"
fi

# The distro's D-Bus-activated USER obexd steals the one AVRCP BIP session a phone will serve, and
# ours is then refused with ECONNREFUSED and no log line. Pi OS Lite does not ship it at all.
case "$(user_systemctl is-enabled obex.service 2>&1)" in
    masked)    echo "    user obexd: already masked" ;;
    not-found) echo "    user obexd: not installed — nothing to mask" ;;
    *)         echo "    user obexd: masking"; user_systemctl mask obex.service 2>/dev/null || true ;;
esac

# The container serves :80 itself under host networking. A host nginx keeps answering while the
# container's own crash-loops on bind() — which reads as a WORKING GUI, right up until someone
# notices it is serving a stale build.
if systemctl is-enabled nginx >/dev/null 2>&1; then
    echo "    host nginx: disabling (the container owns :80)"
    s systemctl disable --now nginx >/dev/null 2>&1 || true
else
    echo "    host nginx: not installed"
fi

if [[ "$RESTART_BT" == 1 ]]; then
    echo "    restarting bluetooth"
    s systemctl restart bluetooth
fi

# The audio HAT. Opt-in, because choosing the overlay is the operator's job — these boards expose
# no ID EEPROM, so there is nothing to auto-detect. --overlay and --unity are TWO PASSES with a
# reboot between them: --unity needs the card to be enumerated, and it is not until the overlay has
# been applied and the Pi has rebooted. Run together, --unity finds no card and leaves the HAT
# ~22 dB quiet with every volume slider reading correctly.
if [[ -n "$OVERLAY" || "$DO_UNITY" == 1 ]]; then
    if fetch_payload "scripts/host-setup/configure-audio-hat.sh" "$PAYLOAD/configure-audio-hat.sh"; then
        s chmod +x "$PAYLOAD/configure-audio-hat.sh"
        if [[ -n "$OVERLAY" ]]; then
            say "audio HAT overlay: $OVERLAY"
            s "$PAYLOAD/configure-audio-hat.sh" --overlay "$OVERLAY" 2>&1 | sed 's/^/    /'
            warn "REBOOT REQUIRED before the card exists — then run again with --unity"
        fi
        if [[ "$DO_UNITY" == 1 ]]; then
            say "pinning the HAT mixer to unity"
            s "$PAYLOAD/configure-audio-hat.sh" --unity 2>&1 | sed 's/^/    /'
        fi
    else
        warn "could not get configure-audio-hat.sh — use --payload-dir with a checkout of the repo"
    fi
fi

# The patched bluetoothd. ~30 minutes, and genuinely optional: nothing in the Python depends on it,
# an unpatched unit just cannot report a mid-track scrub from the phone. Last, because it is the
# only step that can fail slowly.
if [[ "$WITH_BLUEZ" == 1 ]]; then
    say "rebuilding bluetoothd with the AVRCP patches (~30 min)"
    if fetch_payload "backend/config/bluez/install_patched_bluez.sh" "$PAYLOAD/bluez/install_patched_bluez.sh"; then
        for p in 0001-avrcp-poll-getplaystatus-while-playing.patch 0002-avrcp-register-position-with-1s-interval.patch; do
            fetch_payload "backend/config/bluez/$p" "$PAYLOAD/bluez/$p" || warn "missing patch $p"
        done
        s chmod +x "$PAYLOAD/bluez/install_patched_bluez.sh"
        # /var/tmp, not /tmp: /tmp is a 1.9 GB tmpfs on Debian 13 and this build has browned out a
        # Pi's 5 V rail before now, which takes the log with it exactly when it is needed.
        LOG=/var/tmp/plum-bluez-install.log
        s env PLUM_BLUEZ_JOBS="${PLUM_BLUEZ_JOBS:-2}" "$PAYLOAD/bluez/install_patched_bluez.sh" 2>&1 \
            | tee "$LOG" | tail -25 | sed 's/^/    /'
        echo "    full log: $LOG"
    else
        warn "could not get install_patched_bluez.sh — use --payload-dir with a checkout of the repo"
    fi
fi

# --- 5. stop anything that would fight the container ------------------------------------------------
#
# Only ports here, not the pre-container ~/plum-test stack that deploy.sh sweeps: a Pi being
# commissioned from a fresh image has never run that stack. Under host networking a squatter on one
# of these ports makes supervisord crash-loop that program while the squatter keeps answering, so
# the install looks healthy and serves the old thing. Name it and refuse.
say "ports"
if s docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx plum-audio; then
    echo "    stopping the existing plum-audio container"
    s docker rm -f plum-audio >/dev/null
fi
busy="$(s ss -ltnp 2>/dev/null | awk '$4 ~ /:(80|5001|5002|8927|8928|8929)$/ {print $4, $6}')"
if [[ -n "$busy" ]]; then
    echo "    !! ports still bound:" >&2
    echo "$busy" | sed 's/^/      /' >&2
    exit 1
fi
echo "    80, 5001, 5002, 8927, 8928, 8929 all free"
echo "    free disk: $(df -h / | awk 'NR==2{print $4}')"

# --- 6. config --------------------------------------------------------------------------------------

say "$REMOTE_ROOT"
RUN_UID="$(id -u "${SUDO_USER:-$USER}")"
RUN_GID="$(id -g "${SUDO_USER:-$USER}")"
s mkdir -p "$REMOTE_ROOT"/{config,data,media}
s chown -R "${RUN_UID}:${RUN_GID}" "$REMOTE_ROOT"

# PRESERVED across a re-run. A Sendspin id is what the mesh routes on, and settings.json on this
# unit already refers to these — so a second run of this script must not silently rename the unit
# into a stranger its peers have never met. Read them back before the file is rewritten.
#
# PLUM_LOCAL_PLAYER_ID matters for the same reason in a smaller way: the player's mDNS listener name
# is derived from it, and that is the identifier the idle-speaker view joins on.
OLD_ENV="$REMOTE_ROOT/plum-audio.env"
prev() { [[ -f "$OLD_ENV" ]] && sed -n "s/^$1=//p" "$OLD_ENV" | tail -1 || true; }
UNIT_ID="$(prev PLUM_UNIT_ID)"
PLAYER_ID="$(prev PLUM_LOCAL_PLAYER_ID)"
[[ -n "$FLEET_PSK" ]] || FLEET_PSK="$(prev PLUM_FLEET_PSK)"

if [[ -n "$UNIT_ID" ]]; then
    echo "    keeping this unit's existing id: $UNIT_ID"
else
    # Derived from the HOSTNAME, which is what entrypoint.sh would derive if we wrote nothing — but
    # written down here, so the id is stable if the hostname is ever changed later. This is why the
    # README insists on setting a per-unit hostname in Pi Imager: two Pis both called `raspberrypi`
    # would claim one unit id, and two units claiming one id corrupt each other's routing rather
    # than merely looking alike.
    UNIT_ID="unit-$(hostname -s | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9-')"
    echo "    unit id: $UNIT_ID  (from the hostname)"
fi
[[ -n "$PLAYER_ID" ]] || PLAYER_ID="${UNIT_ID}-player"

# The fleet pairing secret. One value shared by every unit, so a unit's server can pair with any
# unit's SPEAKER with no operator step — without it a four-unit mesh needs twelve manual pairings,
# repeated whenever a unit is re-imaged, because a new identity is a new device to every peer.
# Recover it from a unit you already run, rather than inventing a second one. The secret is written
# into every unit's plum-audio.env, so any live peer can hand it back — which is the answer to
# "I set these up months ago and no longer have the code".
if [[ -z "$FLEET_PSK" && -n "$FLEET_PSK_FROM" ]]; then
    say "reading the fleet pairing secret from ${FLEET_PSK_FROM}"
    [[ "$FLEET_PSK_FROM" == *@* ]] || FLEET_PSK_FROM="${SUDO_USER:-$USER}@${FLEET_PSK_FROM}"
    FLEET_PSK="$(ssh -o ConnectTimeout=20 "$FLEET_PSK_FROM" \
        "grep -h '^PLUM_FLEET_PSK=' ${REMOTE_ROOT}/plum-audio.env 2>/dev/null | tail -1 | cut -d= -f2-" \
        2>/dev/null | tr -d '\r\n' || true)"
    if [[ -n "$FLEET_PSK" ]]; then
        echo "    got it"
    else
        echo "    !! ${FLEET_PSK_FROM} did not return a secret. Is it a deployed Plum-Audio unit?" >&2
        exit 1
    fi
fi

MINTED=0
if [[ -z "$FLEET_PSK" ]]; then
    # Minting is right for the FIRST unit and wrong for the fifth. It does not fail loudly: the new
    # unit comes up perfectly and only cross-unit routing to the older units is dead, as a speaker
    # that joins at the right volume and renders nothing. So say plainly what is about to happen.
    FLEET_PSK="$(head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '=\n')"
    MINTED=1
    warn "no fleet pairing secret given — minting a NEW one."
    warn "If you already run other Plum-Audio units, STOP: this unit will not be able to pair with"
    warn "their speakers. Re-run with --fleet-psk-from <an existing unit> instead."
fi

TZ_HOST="$(timedatectl show -p Timezone --value 2>/dev/null || echo UTC)"
s tee "$OLD_ENV" >/dev/null <<ENV
# Generated by scripts/plum-init.sh — re-run that script rather than hand-editing.
PUID=${RUN_UID}
PGID=${RUN_GID}
UMASK=002
TZ=${TZ_HOST}
DEBUG=false

# The mesh identity. PLUM_UNIT_ID is what routing, group membership and per-player volume key off.
# PLUM_UNIT_NAME is what a unit with no settings.json yet adopts BOTH as its own display name and
# as the name of every source endpoint it offers — and a rename in Settings overrides it
# permanently on this unit from then on.
PLUM_UNIT_ID=${UNIT_ID}
PLUM_UNIT_NAME=${NAME}
PLUM_LOCAL_PLAYER_ID=${PLAYER_ID}
PLUM_PLAYER_NAME=${NAME}

# A PortAudio name fragment, not an ALSA address: card numbers move between reboots. Only what this
# unit BOOTS with — Settings -> Audio outranks it permanently once anyone picks an output.
PLUM_DAC_DEVICE=${OUTPUT_SPEC}
PLUM_PLAYER_ENABLED=${PLAYER_ENABLED}
PLUM_STATIC_DELAY_MS=150
PLUM_LOG_LEVEL=INFO
PLUM_MESH_ENABLED=1
PLEXAMP_ENABLED=0

# Accept cleartext Sendspin clients. aiosendspin 9.x defaults this OFF and calls the cleartext path
# "non-spec transition mode"; for us it is a standing requirement, not transitional. sendspin-cpp —
# what ESPHome's Sendspin component, the HA Voice PE and the Esparagus/Satellite1 boards run — has
# no Noise support in any release, and our own GUI controller is a hand-rolled cleartext WebSocket.
# Setting this to 0 drops every third-party speaker on the LAN, Music Assistant, AND the web GUI.
PLUM_ALLOW_UNENCRYPTED=1

# Whether an ENCRYPTED-but-unpaired client may play. Off by default now that real pairing exists —
# it is encrypted but UNAUTHENTICATED, which the spec calls MITM-vulnerable. This is only the
# install-time default: a choice made in the GUI is stored in settings.json and wins from then on.
PLUM_UNPAIRED_ACCESS=0

# The fleet's shared Pairing PSK — identical on every unit, which is what lets a unit's server pair
# with any unit's speaker with no operator step.
PLUM_FLEET_PSK=${FLEET_PSK}

# Optional 8-digit static pairing PIN for this unit's speaker. Must be EXACTLY 8 digits or it is
# refused with a log line.
#PLUM_STATIC_PIN=
ENV

# COMPOSE_PROFILES has to be here, not in plum-audio.env: env_file is container environment, while
# this is compose INTERPOLATION. Written beside the compose file so a bare `docker compose up -d`
# or `restart` run by hand in this directory selects the same service this script does.
s tee "$REMOTE_ROOT/.env" >/dev/null <<COMPOSEENV
# Generated by scripts/plum-init.sh — selects which service in docker-compose.yml applies to this
# host, and which image it runs.
COMPOSE_PROFILES=${PROFILE}
PLUM_IMAGE=${IMAGE_NAME}
PLUM_TAG=${IMAGE_TAG}
COMPOSEENV

if ! fetch_payload "docker/docker-compose.yml" "$REMOTE_ROOT/docker-compose.yml"; then
    echo "    !! could not get docker-compose.yml from the image, --payload-dir or GitHub" >&2
    exit 1
fi
s chown "${RUN_UID}:${RUN_GID}" "$REMOTE_ROOT/docker-compose.yml" "$OLD_ENV" "$REMOTE_ROOT/.env"

# Check the POST-CONDITION, not the exit status. A full disk truncated these to zero bytes once
# while every step still reported success, and compose then refused an "empty compose file" while
# the run printed that the unit was deployed.
[[ -s "$REMOTE_ROOT/docker-compose.yml" && -s "$OLD_ENV" ]] || {
    echo "    !! docker-compose.yml or plum-audio.env is missing/empty" >&2; exit 1; }
echo "    $OLD_ENV  (tz=${TZ_HOST}, uid=${RUN_UID}:${RUN_GID}, profile=${PROFILE})"
echo "    $REMOTE_ROOT/.env            (image=${IMAGE_FULL})"

# --- 7. up + verify ---------------------------------------------------------------------------------

say "up"
cd "$REMOTE_ROOT"
# Through `env`, not an export: s() shells out via sudo, which strips the environment, and an unset
# COMPOSE_PROFILES starts NOTHING while still exiting 0.
s env COMPOSE_PROFILES="$PROFILE" PLUM_IMAGE="$IMAGE_NAME" PLUM_TAG="$IMAGE_TAG" $DC up -d 2>&1 | sed 's/^/    /'

# Wait on OUR process tree, not on a port. Under host networking a port can be answered by
# something that is not this container — that is exactly how a stale host nginx passed a GUI check.
SUPCTL="supervisorctl -c /app/supervisord/supervisord.conf"
for _ in $(seq 1 45); do
    st="$(s docker exec plum-audio $SUPCTL status 2>/dev/null || true)"
    tot="$(grep -c . <<<"$st" || true)"
    run="$(grep -c RUNNING <<<"$st" || true)"
    [[ "$tot" -ge "$WANT_PROGRAMS" && "$run" -eq "$tot" ]] && break
    sleep 2
done

fail=0
notrunning="$(s docker exec plum-audio $SUPCTL status 2>/dev/null | grep -v RUNNING || true)"
[[ -n "$notrunning" ]] && { printf '    \033[31mFAIL\033[0m %-22s %s\n' "supervisord" "not all programs RUNNING"; fail=1; }

chk() {  # chk <label> <url>
    if out="$(curl -fsS -m 5 "$2" 2>&1)"; then
        printf '    \033[32mOK\033[0m   %-22s %s\n' "$1" "$(echo "$out" | head -c 90 | tr -d '\n')"
    else
        printf '    \033[31mFAIL\033[0m %-22s %s\n' "$1" "$out"; fail=1
    fi
}
chk "config API :5002" "http://127.0.0.1:5002/api/settings"
# Polled, unlike the others: the mesh API is served from INSIDE the audio event loop, so it comes up
# a little after supervisord reports sendspin_server RUNNING.
for _ in $(seq 1 10); do
    curl -fsS -m 5 "http://127.0.0.1:5001/api/mesh/view" >/dev/null 2>&1 && break
    sleep 2
done
chk "mesh API :5001" "http://127.0.0.1:5001/api/mesh/view"
chk "web GUI :80"    "http://127.0.0.1/"

# Every check above passes on a unit that renders SILENCE. Under aiosendspin 9.x a client can be
# admitted, negotiated, grouped and at the right volume while activated for no roles — supervisord
# is green, all three APIs answer, both ports listen, and the room is quiet. `active_roles` is the
# only signal that separates the two, so ask for it directly.
if [[ "$WANT_PROGRAMS" -ge 4 ]]; then
    act=""
    for _ in $(seq 1 15); do
        act="$(curl -fsS -m 5 http://127.0.0.1:5001/api/mesh/view 2>/dev/null | python3 -c '
import json, sys
try:
    view = json.load(sys.stdin)
except Exception:
    print("mesh API not answering yet"); raise SystemExit
me = view.get("local_unit_id")
unit = next((u for u in view.get("units", []) if u.get("unit_id") == me), None)
players = (unit or {}).get("players", [])
rows = [p for p in players if any(r.startswith("player@") for r in (p.get("active_roles") or []))]
if rows:
    print("OK " + ",".join(sorted(rows[0].get("active_roles") or [])))
elif players:
    print("NONE " + repr([(p.get("player_id", "?")[:12], p.get("active_roles")) for p in players]))
elif (unit or {}).get("has_player") is False:
    print("OK no player on this unit (audio.output.device=none)")
else:
    print("OK released (idle, claimable) — routing dials it back")
' 2>/dev/null || true)"
        [[ "$act" == OK* ]] && break
        sleep 2
    done
    if [[ "$act" == OK* ]]; then
        printf '    \033[32mOK\033[0m   %-22s %s\n' "player role ACTIVE" "${act#OK }"
    else
        printf '    \033[31mFAIL\033[0m %-22s %s\n' "player role ACTIVE" "$act"
        echo "      A player that is connected but activated for NO roles renders silence with no"
        echo "      error at either end. Check /config/identity exists and the server log shows"
        echo "      'trusted local player'; see docs/OPERATIONS.md."
        fail=1
    fi
    echo "    sendspin player :8928 $(s ss -ltn | grep -q ':8928' && echo listening || echo 'NOT LISTENING')"
else
    echo "    sendspin player :8928 not started (this unit has no audio output)"
fi
echo "    sendspin server :8927 $(s ss -ltn | grep -q ':8927' && echo listening || echo 'NOT LISTENING')"
echo "    supervisord:"
s docker exec plum-audio $SUPCTL status 2>&1 | sed 's/^/      /'

if [[ $fail -ne 0 ]]; then
    for prog in $(echo "$notrunning" | awk '{print $1}'); do
        echo "    --- last 15 lines: $prog ---"
        s docker exec plum-audio tail -15 "/config/logs/${prog}.log" 2>&1 | sed 's/^/      /'
    done
    echo "    --- last 15 lines: sendspin_server ---"
    s docker exec plum-audio tail -15 /config/logs/sendspin_server.log 2>&1 | sed 's/^/      /'
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
say "done"
if [[ $fail -ne 0 ]]; then
    echo "this unit came up with failures — see above"
    exit 1
fi
echo "\"${NAME}\" is running.  Open http://${IP:-<this-pi>}/"
echo
if [[ "$MINTED" == 1 ]]; then
    cat <<PSK
This unit minted the fleet pairing secret. Every OTHER unit must get the same value,
or the units cannot pair with each other's speakers:

    PLUM_FLEET_PSK=${FLEET_PSK}

Write it down now. On the next unit, run:

    sudo ./plum-init.sh "<its name>" --fleet-psk ${FLEET_PSK}
PSK
else
    echo "Joined the fleet on the shared pairing secret."
    echo "Lost it before the next unit? Any running unit can hand it back:"
    echo "    sudo ./plum-init.sh \"<its name>\" --fleet-psk-from ${IP:-<this-pi>}"
    echo "To confirm the units can see each other, run this on either one:"
    echo
    echo "    curl -s http://127.0.0.1:5001/api/mesh/view | python3 -m json.tool | grep unit_id"
fi
