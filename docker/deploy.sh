#!/usr/bin/env bash
# Deploy the Plum-Audio unit container to the R&D Pis.
#
#   ./deploy.sh all                          # every unit in units.conf
#   ./deploy.sh 192.0.2.10                   # one unit
#   ./deploy.sh all --tarball dist/x.tar.gz  # a specific build (default: newest in dist/)
#   ./deploy.sh all --no-migrate             # skip importing the ~/plum-test state
#   ./deploy.sh all --new-fleet-psk          # mint a NEW fleet pairing secret (then redeploy all)
#   ./deploy.sh all --image ghcr.io/anothermike-exe/plum-audio:1.0.0   # pull a published release
#   ./deploy.sh all --pull                   # shorthand for the default registry at :latest
#
# TWO WAYS TO GET THE IMAGE ONTO A UNIT, and they are a real trade, not a preference:
#   tarball (default) — build.sh + scp + `docker load`. Works with no internet on the unit and no
#                       registry auth, and deploys exactly the tree you have in front of you,
#                       including uncommitted work. ~200 MB over the LAN per unit.
#   --image / --pull  — the unit pulls from a registry. Deploys a BUILT, TESTED, TAGGED artifact
#                       rather than whatever is on this laptop, and four units pull in parallel
#                       instead of taking four sequential scp copies. Needs the unit to reach the
#                       internet, and the image must be public or the unit must be logged in.
#
# What it does per unit, in order: preflight -> ensure Docker -> STOP the pre-container dev stack
# and any old containers -> create /opt/plum-audio -> import existing rig state on first deploy ->
# load the image -> install compose + a per-unit env -> up -> verify.
#
# It is deliberately re-runnable: a second deploy of a new build stops at "load + up" and leaves
# /opt/plum-audio/{config,data} alone, so settings.json and the Spotify authorisations survive.
#
# The ~/plum-test tree is left ON DISK, only its processes are stopped — reverting to the dev stack
# is `docker compose down` on the unit plus ~/plum-test/run_*.sh, with no restore step.
set -euo pipefail

INVOKED_FROM="$PWD"          # captured BEFORE the cd — see the --tarball resolution below
cd "$(dirname "$0")"
HERE="$PWD"

# The rig credential is NOT stored in this repo. Export PLUM_TEST_PW, or put it in
# docker/.deploy.env (gitignored), which is sourced here when it exists.
[[ -f "${HERE}/.deploy.env" ]] && source "${HERE}/.deploy.env"
USER_="${PLUM_TEST_USER:-plum-admin}"
PW="${PLUM_TEST_PW:?not set — export it, or create docker/.deploy.env containing PLUM_TEST_PW=<rig password>}"

# --- fleet pairing secret -------------------------------------------------------------------------
# One Pairing PSK shared by every unit, so a unit's server can pair with any unit's SPEAKER without
# an operator. Without it a four-unit mesh needs twelve manual pairings, repeated whenever a unit is
# re-imaged (a new identity is a new device to every peer).
#
# Generated ONCE and kept in .deploy.env, which is gitignored, because the whole point is that every
# unit gets the SAME value — regenerating per deploy would silently unpair the fleet on every run.
# Losing the workstation copy is NOT losing the secret: every unit holds it in its own
# plum-audio.env, and the recovery below reads it back from one. Rotating (--new-fleet-psk) is the
# last resort, and it means redeploying every unit together.
#
# It is a shared secret: anyone holding it can pair with any unit. That is a real step down from a
# per-pair record and a real step up from the sentinel PSK, which is published. Unset it for the
# stricter posture, where units pair only with their own speaker and everything else is deliberate.
# LOSING .deploy.env IS THE COMMON CASE, not the exotic one: units get commissioned today and
# extended months later, from a laptop that has been reinstalled in between. The secret is NOT lost
# when that happens — deploy.sh wrote it into every unit's /opt/plum-audio/plum-audio.env, so a
# single already-deployed unit can hand it back.
#
# Minting a fresh one instead is the failure this guards. It does not error: the new units come up
# perfectly, pair with their own speakers, and serve their GUIs, and only CROSS-UNIT routing to the
# older generation is dead — a speaker that joins the group at the right volume and renders nothing.
# So recovery is attempted before minting, and minting is refused outright when any unit could not
# be asked. See "Losing the fleet secret" in docs/SENDSPIN-PAIRING.md.
NEED_PSK=0
if [[ -z "${PLUM_FLEET_PSK:-}" ]]; then
    if [[ -f "${HERE}/.deploy.env" ]] && grep -q '^PLUM_FLEET_PSK=' "${HERE}/.deploy.env"; then
        PLUM_FLEET_PSK="$(grep '^PLUM_FLEET_PSK=' "${HERE}/.deploy.env" | tail -1 | cut -d= -f2-)"
    else
        NEED_PSK=1   # resolved below, once ssh_ and the unit table exist
    fi
fi
# UserKnownHostsFile=/dev/null, not just StrictHostKeyChecking=no: a REIMAGED unit presents a new
# host key, and a conflicting known_hosts entry makes ssh refuse the connection outright — password
# auth is disabled in that state, so the deploy fails on the very first ssh of every unit with a
# man-in-the-middle warning. StrictHostKeyChecking=no only covers a host ssh has never seen. These
# units are a password-auth lab rig, so there is no key-pinning posture to preserve, and the
# alternative is telling the operator to run ssh-keygen -R by hand on exactly the runs (a fresh
# image) where they are least expecting the failure. Found re-imaging the mesh-pair units for the
# alpha, 2026-08-06.
SSH_OPTS="-o ConnectTimeout=20 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"
REMOTE_ROOT="/opt/plum-audio"
UNITS_FILE="${HERE}/units.conf"
[[ -f "$UNITS_FILE" ]] || {
    echo "no docker/units.conf — copy docker/units.conf.example to it and list your units" >&2
    exit 1
}

MIGRATE=1
FORCE_NEW_PSK=0
TARBALL=""
IMAGE_REF=""
DEFAULT_REGISTRY="ghcr.io/anothermike-exe/plum-audio"
# How many PREVIOUS image tags to keep on a unit, beyond the one being deployed and `latest`. Two is
# enough for a rollback without letting a 29 GB SD card fill; the tarballs in dist/ are the real
# rollback anyway.
KEEP_IMAGES="${PLUM_KEEP_IMAGES:-2}"
HOSTS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        all)          HOSTS+=("all"); shift ;;
        --tarball)    TARBALL="$2"; shift 2 ;;
        --image)      IMAGE_REF="$2"; shift 2 ;;
        --pull)       IMAGE_REF="${DEFAULT_REGISTRY}:latest"; shift ;;
        --no-migrate) MIGRATE=0; shift ;;
        --new-fleet-psk) FORCE_NEW_PSK=1; shift ;;
        -h|--help)    sed -n '2,32p' "$0"; exit 0 ;;
        -*)           echo "unknown flag $1" >&2; exit 2 ;;
        *)            HOSTS+=("$1"); shift ;;
    esac
done
[[ ${#HOSTS[@]} -gt 0 ]] || {
    echo "usage: deploy.sh <all|host...> [--tarball f | --image ref | --pull] [--no-migrate]" >&2
    exit 2
}
[[ -z "$IMAGE_REF" || -z "$TARBALL" ]] || {
    echo "--tarball and --image/--pull are mutually exclusive: pick where the image comes from" >&2
    exit 2
}

# Split the ref once, here, rather than in the per-host function: compose interpolates PLUM_IMAGE and
# PLUM_TAG separately, and a ref with a port (registry:5000/x:tag) makes the naive rsplit wrong.
if [[ -n "$IMAGE_REF" ]]; then
    if [[ "${IMAGE_REF##*/}" == *:* ]]; then
        PLUM_IMAGE_NAME="${IMAGE_REF%:*}"
        PLUM_IMAGE_TAG="${IMAGE_REF##*:}"
    else
        PLUM_IMAGE_NAME="$IMAGE_REF"
        PLUM_IMAGE_TAG="latest"
    fi
else
    PLUM_IMAGE_NAME="plum-audio"
    PLUM_IMAGE_TAG="latest"
fi

command -v sshpass >/dev/null || { echo "sshpass required (brew install sshpass)" >&2; exit 1; }

# Newest tarball wins when none was named — the common case is "I just ran build.sh". Skipped
# entirely in registry mode, where there is no tarball to find and demanding one would be absurd.
if [[ -n "$IMAGE_REF" ]]; then
    TARBALL=""
elif [[ -z "$TARBALL" ]]; then
    TARBALL="$(ls -t ../dist/plum-audio-*.tar.gz 2>/dev/null | head -1 || true)"
elif [[ "$TARBALL" != /* ]]; then
    # A relative --tarball has to be resolved against the caller's cwd, not this script's. We have
    # already cd'd into docker/, so `--tarball dist/plum-audio-<tag>-arm64.tar.gz` — the form
    # docs/OPERATIONS.md documents, run from the repo root — would look for docker/dist/... and fail
    # with "no image tarball (run build.sh first)" about a file that is plainly there.
    for cand in "${INVOKED_FROM}/${TARBALL}" "${HERE}/../${TARBALL}" "${TARBALL}"; do
        [[ -f "$cand" ]] && { TARBALL="$cand"; break; }
    done
fi
if [[ -z "$IMAGE_REF" ]]; then
    [[ -f "$TARBALL" ]] || { echo "no image tarball at '${TARBALL:-<none>}' (run docker/build.sh first)" >&2; exit 1; }
    TARBALL="$(cd "$(dirname "$TARBALL")" && pwd)/$(basename "$TARBALL")"
fi

# A deploy opens a dozen authenticated connections per unit in quick succession, and sshd will
# occasionally refuse one ("Permission denied" on a password that is demonstrably correct). Retry
# rather than fail a whole unit on a transient auth refusal.
#
# But retry the TRANSPORT ONLY. `retry_` re-runs the whole remote block, so a state-changing step
# that fails once and then "succeeds" on a second attempt — because the first attempt already left
# the file half-written — reports success for a step that did not do what it says. That is the
# likeliest way a full disk truncated docker-compose.yml and the deploy carried on regardless.
# ssh exits 255 for its own failures and otherwise passes the remote command's status straight
# through, so keying on 255 retries a refused connection and never masks a genuine failure.
# (tests/Integration/lib.sh carries the same rule for the same reason.)
retry_() {
    local n=0
    until "$@"; do
        n=$((n + 1))
        [[ $n -ge 3 ]] && return 1
        sleep 3
    done
}
retry_ssh_() {
    local n=0 rc
    while :; do
        "$@"; rc=$?
        [[ $rc -ne 255 ]] && return $rc
        n=$((n + 1))
        [[ $n -ge 3 ]] && return $rc
        sleep 3
    done
}
ssh_()  { retry_ssh_ sshpass -p "$PW" ssh $SSH_OPTS "${USER_}@$1" "${@:2}"; }
# scp keeps the blanket retry: it moves 200 MB and an interrupted transfer is genuinely worth
# re-running, with no remote state to leave half-changed.
scp_()  { retry_ sshpass -p "$PW" scp $SSH_OPTS "$1" "${USER_}@$2:$3"; }
# Send a local file over an existing-style ssh session instead of a second scp auth. Used for the
# small config files; the image tarball still goes by scp (scp is far faster for 200 MB).
put_()  { retry_ssh_ sshpass -p "$PW" ssh $SSH_OPTS "${USER_}@$1" "cat > '$3'" < "$2"; }
say()   { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn()  { printf '\033[33m    !! %s\033[0m\n' "$*"; }

# --- unit table --------------------------------------------------------------------------------

units_all() { grep -vE '^\s*(#|$)' "$UNITS_FILE" | awk -F'|' '{gsub(/ /,"",$1); print $1}'; }

unit_field() {  # unit_field <host> <1-based field>
    grep -vE '^\s*(#|$)' "$UNITS_FILE" \
        | awk -F'|' -v h="$1" -v f="$2" '{gsub(/^[ \t]+|[ \t]+$/,"",$1); if ($1==h) {v=$f; gsub(/^[ \t]+|[ \t]+$/,"",v); print v}}'
}

# units.conf is the ONLY thing that makes units distinguishable, and nothing else checks it.
#
# The unit-name column becomes PLUM_UNIT_NAME, which a unit with no settings.json yet adopts as its
# own display name AND as the name of every source endpoint it offers — so two rows sharing a name
# produce two units that are indistinguishable in the mesh view, in the GUI's unit cards, and to an
# AirPlay sender picking a speaker. That is the confusion the greenfield alpha hit on 2026-08-06. The
# ids are worse than cosmetic: the mesh keys routing, group membership and per-player volume off them,
# so two units claiming one unit_id corrupt each other's routing rather than merely looking alike.
#
# A clash DISAMBIGUATES, it does not stop the deploy. Refusing was the first attempt and it was the
# wrong trade: it turns a cosmetic naming slip into a rig that will not deploy at all, and the failure
# lands after someone has already re-flashed a card. So a duplicated field gets `-<token>` appended and
# a loud warning, which is recoverable in the GUI at any time. Rename in Settings to take control.
#
# Computed once for the WHOLE table rather than per selected host: a clash is a property of the file,
# and `deploy.sh <one-host>` must suffix identically to `deploy.sh all` or the two disagree.
dup_in_column() {  # dup_in_column <1-based field>
    grep -vE '^\s*(#|$)' "$UNITS_FILE" \
        | awk -F'|' -v f="$1" '{v=$f; gsub(/^[ \t]+|[ \t]+$/,"",v); if (v!="") print v}' \
        | sort | uniq -d
}
# LEGACY TABLES. units.conf used to be six columns —
#   host | unit id | unit name | player id | player name | DAC
# — and is now two, with an optional third:
#   host | name | [DAC]
# The ids went away because nothing needed an operator to choose them: entrypoint.sh derives them
# from the hostname, and this script preserves whatever a unit is ALREADY running under. But the
# two formats are indistinguishable by shape alone, and reading a six-column row as a two-column
# one takes `unit-133` for the unit NAME — which would rename a live unit to its own id. So detect
# the column count and map the old layout, loudly, rather than quietly getting it wrong.
LEGACY_TABLE=0
if grep -vE '^\s*(#|$)' "$UNITS_FILE" | awk -F'|' 'NF>=5{f=1} END{exit !f}'; then
    LEGACY_TABLE=1
    printf '\033[33m!! %s is in the old six-column format. Reading it, but the ids are now derived —\033[0m\n' "${UNITS_FILE##*/}"
    printf '\033[33m   see docker/units.conf.example for the two-column layout.\033[0m\n'
fi
# Which field holds what, in each layout.
if [[ "$LEGACY_TABLE" == 1 ]]; then
    F_NAME=3; F_DAC=6
else
    F_NAME=2; F_DAC=3
fi

DUP_HOST="$(dup_in_column 1)"
DUP_UNIT_NAME="$(dup_in_column "$F_NAME")"

if [[ -n "$DUP_HOST$DUP_UNIT_NAME" ]]; then
    printf '\033[33m!! %s has duplicate values; they will be suffixed per unit:\033[0m\n' "${UNITS_FILE##*/}"
    for pair in "host:$DUP_HOST" "name:$DUP_UNIT_NAME"; do
        [[ -n "${pair#*:}" ]] && printf '     %-12s %s\n' "${pair%%:*}" "$(echo "${pair#*:}" | tr '\n' ' ')"
    done
    # A duplicated HOST is the one case a suffix cannot help — it is the same box twice, so the second
    # pass simply overwrites the first. Say so; the run is still harmless.
    [[ -n "$DUP_HOST" ]] && printf '\033[33m     (a repeated host just deploys twice to the same box)\033[0m\n'
fi

# A short, stable, per-unit token used only to break a units.conf clash.
#
# The Pi's SoC serial, not a MAC: it survives a NIC swap, a reflash and every reboot, whereas a
# default-route MAC moves the moment a unit is put on wlan0 instead of eth0 — which would silently
# RENAME the unit on its next deploy, the exact problem this is here to prevent. MACs are the fallback
# for non-Pi hosts, lowest-first so enumeration order cannot change the answer.
unit_token() {  # unit_token <host>
    ssh_ "$1" "bash -s" <<'EOS'
set -uo pipefail
t="$(sed -n 's/^Serial[[:space:]]*:[[:space:]]*//p' /proc/cpuinfo 2>/dev/null | tail -1 | tr -d ' ')"
if [[ -z "$t" ]]; then
    macs=""
    for n in /sys/class/net/*; do
        i="${n##*/}"
        case "$i" in lo|docker*|veth*|br-*|dummy*) continue ;; esac
        [[ -e "$n/device" ]] || continue          # physical only
        macs="${macs}$(cat "$n/address" 2>/dev/null | tr -d ':')
"
    done
    t="$(printf '%s' "$macs" | grep -v '^$' | sort | head -1)"
fi
# Last four hex, uppercased. Short enough to read out over the phone, long enough that a rig would
# need ~65k units before a collision is likely.
printf '%s' "$t" | tr -cd '0-9A-Fa-f' | tail -c 4 | tr '[:lower:]' '[:upper:]'
EOS
}

# The unit's audio output, read from the cards it actually has.
#
# This is the column an operator used to have to fill in, and it is the one they were least able to
# answer from the workstation: the spec is a PortAudio NAME FRAGMENT, matched as a substring against
# PortAudio's own device names, and it is not the same list as `aplay -l`. The long name at the tail
# of a /proc/asound/cards line is the form already proven on this rig (`bcm2835`,
# `snd_rpi_hifiberry_dacplus`), so take that.
#
# Ranking, lowest wins. A HAT or USB DAC is something someone fitted ON PURPOSE, so it outranks the
# onboard jack. HDMI is last: it is present on every Pi, it is almost never the intended output for
# this, and PortAudio's own default lands on it more often than not. Empty output means no cards at
# all, which the caller reads as a headless unit.
#
# Mirrored in scripts/plum-init.sh, which does the same job from the unit itself. Keep the two in
# step; a unit must not get a different answer depending on which script commissioned it.
detect_output_on() {  # detect_output_on <host>
    ssh_ "$1" "bash -s" <<'EOS'
set -uo pipefail
best=""; best_rank=99
while IFS= read -r line; do
    trimmed="${line#"${line%%[![:space:]]*}"}"
    # A glob, not a regex: a bracket inside [[ =~ ]] is a portability trap.
    case "$trimmed" in [0-9]*"["*) ;; *) continue ;; esac
    id="${trimmed#*[}"; id="${id%%]*}"; id="$(printf '%s' "$id" | tr -d ' ')"
    longname="${line##* - }"
    [ -n "$longname" ] && [ "$longname" != "$line" ] || longname="$id"
    case "$id$longname" in
        *vc4hdmi*|*HDMI*|*hdmi*)             rank=3 ;;
        *bcm2835*|*Headphones*|*headphones*) rank=2 ;;
        *)                                   rank=1 ;;
    esac
    [ "$rank" -lt "$best_rank" ] && { best_rank="$rank"; best="$longname"; }
    printf '      card %-18s %s\n' "$id" "$longname" >&2
done < /proc/asound/cards 2>/dev/null
printf '%s' "$best"
EOS
}

# Echo $1, suffixed with the unit's token when $2 (a newline-separated duplicate list) contains it.
# Warnings go to STDERR: this runs inside a command substitution, so anything on stdout becomes part
# of the value and would end up in plum-audio.env.
disambiguate() {  # disambiguate <value> <dup-list> <label> <host>
    local v="$1" dups="$2" label="$3" host="$4"
    if [[ -n "$dups" ]] && printf '%s\n' "$dups" | grep -qxF -- "$v"; then
        [[ -n "${TOKEN:-}" ]] || TOKEN="$(unit_token "$host")"
        if [[ -n "$TOKEN" ]]; then
            printf '\033[33m    !! %s %s is duplicated in units.conf -> using %s-%s\033[0m\n' \
                "$label" "$v" "$v" "$TOKEN" >&2
            printf '%s-%s' "$v" "$TOKEN"
            return
        fi
        printf '\033[33m    !! %s %s is duplicated and no token could be derived\033[0m\n' "$label" "$v" >&2
    fi
    printf '%s' "$v"
}

if [[ " ${HOSTS[*]} " == *" all "* ]]; then
    # Plain read loop, not `mapfile`: this is normally run from macOS, whose /bin/bash is 3.2 and
    # has no mapfile. The failure only ever showed on `deploy.sh all`, since single-host runs skip it.
    HOSTS=()
    while IFS= read -r _h; do
        [[ -n "$_h" ]] && HOSTS+=("$_h")
    done < <(units_all)
fi

# --- fleet pairing secret, part two: recover it before inventing one -------------------------------

if [[ "$NEED_PSK" == 1 ]]; then
    say "no fleet pairing secret on this workstation — asking the units"
    # EVERY unit in the table, not just the ones being deployed. A peer that is not part of this run
    # still holds the fleet's secret, and it is exactly the unit an operator forgets to mention when
    # adding two new rooms to a system built months ago.
    _found=""; _found_on=""; _unreachable=""
    while IFS= read -r _h; do
        [[ -n "$_h" ]] || continue
        _psk="$(ssh_ "$_h" "grep -h '^PLUM_FLEET_PSK=' ${REMOTE_ROOT}/plum-audio.env 2>/dev/null | tail -1 | cut -d= -f2-" 2>/dev/null | tr -d '\r\n' || true)"
        if [[ -z "$_psk" ]]; then
            # Tell "answered, has no secret" from "did not answer at all". Only the first is safe to
            # mint over: it means the unit is genuinely greenfield, not merely switched off.
            if ssh_ "$_h" true >/dev/null 2>&1; then
                echo "    $_h — reachable, no secret stored"
            else
                echo "    $_h — UNREACHABLE"
                _unreachable="${_unreachable}${_h} "
            fi
            continue
        fi
        echo "    $_h — has one"
        if [[ -z "$_found" ]]; then
            _found="$_psk"; _found_on="$_h"
        elif [[ "$_psk" != "$_found" ]]; then
            warn "$_h disagrees with $_found_on — this fleet is ALREADY split into two pairing groups."
            warn "Pick one and redeploy every unit with it: deploy.sh all (after fixing .deploy.env)."
        fi
    done < <(units_all)

    if [[ -n "$_found" ]]; then
        PLUM_FLEET_PSK="$_found"
        printf 'PLUM_FLEET_PSK=%s\n' "$PLUM_FLEET_PSK" >> "${HERE}/.deploy.env"
        say "recovered the fleet pairing secret from ${_found_on} and restored it to docker/.deploy.env"
    elif [[ -n "$_unreachable" && "$FORCE_NEW_PSK" != 1 ]]; then
        # Refusing beats a silent split. A new secret here would leave the units that ARE up unable
        # to pair with the ones that are down, and nothing would report it as an error.
        echo
        warn "no unit could hand back a fleet secret, and these were unreachable: ${_unreachable}"
        warn "Minting a new one now would split the fleet, and cross-unit routing would go silent."
        warn "Either bring those units up and re-run, or pass --new-fleet-psk to mint one anyway"
        warn "and then redeploy EVERY unit together so they all share it."
        exit 1
    else
        PLUM_FLEET_PSK="$(head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '=\n')"
        printf 'PLUM_FLEET_PSK=%s\n' "$PLUM_FLEET_PSK" >> "${HERE}/.deploy.env"
        say "minted a fleet pairing secret into docker/.deploy.env (shared by every unit)"
        [[ "$FORCE_NEW_PSK" == 1 ]] && warn "--new-fleet-psk: redeploy EVERY unit so they all get this value"
    fi
fi

# --- per-unit deploy ---------------------------------------------------------------------------

deploy_one() {
    local host="$1"
    local unit_id unit_name player_id player_name dac
    unit_name="$(unit_field "$host" "$F_NAME")"
    dac="$(unit_field "$host" "$F_DAC")"
    [[ -n "$unit_name" ]] || { warn "$host is not in units.conf — skipping"; return 1; }

    # Break any units.conf clash before the value reaches plum-audio.env. TOKEN is fetched at most
    # once per unit, and only when there is actually a clash — an unambiguous table costs no extra ssh.
    local TOKEN=""
    unit_name="$(disambiguate "$unit_name" "$DUP_UNIT_NAME" name "$host")"
    # A unit and its speaker are one thing to the user, and entrypoint.sh already defaults the player
    # name to the unit name. Naming them apart only ever produced two names for one box.
    player_name="$unit_name"

    say "$host — ${unit_name}"

    # 1. Preflight: reachable, arm64, sudo, and the host daemons this container depends on.
    ssh_ "$host" "bash -s -- '$PW'" <<'EOS' || return 1
set -euo pipefail
PW="$1"
s() { echo "$PW" | sudo -S -p '' "$@"; }
echo "    host:  $(hostname) / $(dpkg --print-architecture) / $(. /etc/os-release; echo "$PRETTY_NAME")"
[[ "$(dpkg --print-architecture)" == arm64 ]] || { echo "    !! not arm64" >&2; exit 1; }
s true || { echo "    !! sudo failed" >&2; exit 1; }
# The container talks to BOTH of these over the host's system bus; without them AirPlay/Spotify
# never advertise and Bluetooth never sees the radio.
systemctl is-active --quiet avahi-daemon || echo "    !! avahi-daemon is NOT running on the host"
systemctl is-active --quiet bluetooth    || echo "    !! bluetoothd is NOT running on the host"
# Bluetooth scrub reporting needs the patched bluez (backend/config/bluez/). Informational: an
# unpatched unit still plays audio, it just cannot report position from the phone.
dpkg -l bluez 2>/dev/null | grep -q '+plum' \
    || echo "    !! host bluez is UNPATCHED (no AVRCP position; see backend/config/bluez/)"
EOS

    # THE UNIT IDENTITY, in one place, and only after the host has answered.
    #
    # A Sendspin id is what the mesh keys routing, group membership and per-player volume off, and
    # settings.json on the unit already refers to it. So the first question is never "what does the
    # table say", it is "what is this unit already running under" — redeploying a live unit must
    # never rename it into a stranger its peers have never met.
    #
    # Nothing is lost by deriving the rest. entrypoint.sh has always defaulted the unit id from the
    # hostname and the player id from the unit id, so the old six-column table only wrote down what
    # those defaults would have produced anyway. What an operator actually chooses is the NAME.
    local existing derived_id
    existing="$(ssh_ "$host" "cat ${REMOTE_ROOT}/plum-audio.env 2>/dev/null" 2>/dev/null || true)"
    unit_id="$(printf '%s\n' "$existing" | sed -n 's/^PLUM_UNIT_ID=//p' | tail -1)"
    player_id="$(printf '%s\n' "$existing" | sed -n 's/^PLUM_LOCAL_PLAYER_ID=//p' | tail -1)"
    if [[ -n "$unit_id" ]]; then
        echo "    unit id: $unit_id  (already deployed — kept)"
    else
        derived_id="$(ssh_ "$host" "hostname -s" 2>/dev/null | tr -d '\r' | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9-')"
        # `raspberrypi` is Pi Imager's default, and the one hostname a rig genuinely repeats. Two
        # units claiming one unit id corrupt each other's routing rather than merely looking alike,
        # so break that case with the SoC token, exactly as a duplicated name is broken.
        if [[ -z "$derived_id" || "$derived_id" == "raspberrypi" ]]; then
            [[ -n "$TOKEN" ]] || TOKEN="$(unit_token "$host")"
            warn "hostname is '${derived_id:-unknown}' — set a unique one in Pi Imager; using the SoC token"
            derived_id="${derived_id:-plum}-${TOKEN:-$RANDOM}"
        fi
        unit_id="unit-${derived_id}"
        echo "    unit id: $unit_id  (derived from the hostname)"
    fi
    [[ -n "$player_id" ]] || player_id="${unit_id}-player"

    # THE OUTPUT. Optional in the table, because it is derivable AND because getting it wrong is
    # cheap: PLUM_DAC_DEVICE is only what a unit boots with, and Settings -> Audio overrides it
    # permanently the first time anyone picks a device. So read the cards the Pi actually has, and
    # let the column exist as an override for the case where the guess is wrong.
    if [[ -z "$dac" ]]; then
        dac="$(detect_output_on "$host")"
        if [[ -n "$dac" ]]; then
            echo "    audio output: $dac  (detected — change it in Settings -> Audio)"
        else
            dac="none"
            echo "    audio output: none — this unit will ingest and route only"
        fi
    else
        echo "    audio output: $dac  (from units.conf)"
    fi

    # A DAC of `none` means this host has no audio output: no player process, no /dev/snd, and the
    # headless compose profile (the audio one cannot even be CREATED without /dev/snd).
    local profile player_enabled expected_programs
    # `tr`, not ${dac,,}: that is bash 4+, and macOS — where this script is RUN — ships bash 3.2,
    # so the parameter expansion is a hard syntax error before any unit is contacted.
    if [[ "$(printf '%s' "$dac" | tr '[:upper:]' '[:lower:]')" == "none" ]]; then
        profile="headless"; player_enabled=0; expected_programs=3
    else
        profile="audio";    player_enabled=1; expected_programs=4
    fi


    # 2. Docker. .113 shipped without it, and compose reaches the units two different ways: .122
    #    runs Docker CE from Docker's own apt repo (compose as a CLI plugin, `docker compose`),
    #    while the Debian-packaged units get trixie's `docker-compose` 2.26 standalone binary.
    #    Same compose file either way — so detect the invocation rather than forcing one repo
    #    layout onto every unit. NOTE: trixie has no `docker-compose-v2`; the package is
    #    `docker-compose` and it is compose v2, not the retired Python v1.
    say "$host — docker"
    ssh_ "$host" "bash -s -- '$PW'" <<'EOS' || return 1
set -euo pipefail
PW="$1"
s() { echo "$PW" | sudo -S -p '' "$@"; }
need=()
command -v docker >/dev/null || need+=(docker.io)
if ! s docker compose version >/dev/null 2>&1 && ! command -v docker-compose >/dev/null 2>&1; then
    need+=(docker-compose)
fi
if [[ ${#need[@]} -gt 0 ]]; then
    echo "    installing: ${need[*]}"
    s apt-get update -qq
    # `env` sets DEBIAN_FRONTEND: sudo does not take VAR=value before the command.
    s env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${need[@]}"
fi
s systemctl enable --now docker >/dev/null 2>&1 || true
# Convenience only (takes effect at next login); this script keeps using sudo regardless.
s usermod -aG docker "$USER" 2>/dev/null || true
if s docker compose version >/dev/null 2>&1; then dc="docker compose"; else dc="docker-compose"; fi
echo "    $(s docker --version 2>&1)  |  $(s $dc version 2>&1 | head -1)  [$dc]"
EOS

    # 3. Stop what would fight the container for ports, ALSA, D-Bus names and the radio: the
    #    pre-container dev stack, and any old container generation.
    say "$host — stopping the pre-container stack"
    ssh_ "$host" "bash -s -- '$PW'" <<'EOS' || return 1
set -uo pipefail
PW="$1"
s() { echo "$PW" | sudo -S -p '' "$@"; }

# Containers FIRST. A previous plum-audio generation runs the very same script names, and the host
# PID namespace shows them, so sweeping processes first would match the container's own tree and
# report it as leftover dev stack.
for c in plum-audio plum-snapcast-server plum-snapcast-frontend; do
    if s docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$c"; then
        echo "    removing container $c"
        s docker rm -f "$c" >/dev/null
    fi
done
for i in $(s docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -E 'plum-snapcast' || true); do
    echo "    removing image $i"
    s docker rmi -f "$i" >/dev/null 2>&1 || true
done

[[ -x ~/plum-test/stop_stack.sh ]] && ~/plum-test/stop_stack.sh >/dev/null 2>&1

# Patterns are scoped to plum-test / our own venv so nothing of the HOST's (bluetoothd, avahi, the
# system obexd if any) is caught by a stray match. `spin/bin/python` is the rig's venv: the config
# API runs as a bare `server.py` from its own cwd, so only the interpreter path identifies it —
# matching 'apis/server.py' alone silently misses it, and it keeps :5002 from the container.
DEV_PATTERNS=('sendspin_server\.py' 'sendspin_player\.py' 'apis/server\.py' 'spin/bin/python server\.py'
              'plum-test/shairport' 'plum-test/go-librespot' 'plum-test/bluetooth'
              'shairport-sync -c /home' 'go-librespot --config_dir /home'
              'bluealsa --profile' '/usr/libexec/bluetooth/obexd')
for pat in "${DEV_PATTERNS[@]}"; do
    pkill -f "$pat" 2>/dev/null
done
sleep 2
# SIGKILL every pattern, not just the Python ones. A shairport-sync whose private session bus has
# already been killed traps SIGTERM and hangs in shutdown — pkill reports success, the process
# stays, and it still holds AirPlay's RAOP port (5050), which is outside the port check below
# because endpoint ports are configurable. Escalating unconditionally is cheaper than reasoning
# about which daemon died cleanly.
for pat in "${DEV_PATTERNS[@]}"; do
    pkill -9 -f "$pat" 2>/dev/null
done
sleep 1

# Survivors are a hard failure, not a note: anything left here holds a source port or an ALSA
# device the container is about to want.
left="$(pgrep -af 'plum-test|spin/bin/python' | grep -v pgrep || true)"
if [[ -n "$left" ]]; then
    echo "    !! dev-stack processes survived SIGKILL:"
    echo "$left" | sed 's/^/      /'
    exit 1
fi

# The pre-container GUI was served by the HOST's nginx (/var/www/plum-audio, with the same proxy
# config the container now ships). The unit container serves :80 itself under host networking, so
# the host copy has to stand down — otherwise the container's nginx crash-loops on bind() while
# the host keeps answering, which reads as a WORKING GUI right up until you notice it is serving
# a stale build. Config and webroot are left on disk; only the service is stopped.
if systemctl is-active --quiet nginx 2>/dev/null; then
    echo "    stopping + disabling host nginx (the container now owns :80)"
    s systemctl disable --now nginx >/dev/null 2>&1 || true
fi

echo "    free disk: $(df -h / | awk 'NR==2{print $4}')"
# The container binds these under host networking. Anything still holding one is a HARD stop, not a
# warning: supervisord would crash-loop that program while the squatter keeps answering on the port,
# so the deploy would look healthy and serve the old thing. Name it and refuse.
busy="$(s ss -ltnp 2>/dev/null | awk '$4 ~ /:(80|5001|5002|8927|8928|8929)$/ {print $4, $6}')"
if [[ -n "$busy" ]]; then
    echo "    !! ports still bound after stopping the dev stack:"
    echo "$busy" | sed 's/^/      /'
    exit 1
fi
exit 0
EOS

    # 4. Volume layout + first-deploy state import.
    say "$host — ${REMOTE_ROOT} + state"
    ssh_ "$host" "bash -s -- '$PW' '$REMOTE_ROOT' '$MIGRATE'" <<'EOS' || return 1
set -euo pipefail
PW="$1"; ROOT="$2"; MIGRATE="$3"
s() { echo "$PW" | sudo -S -p '' "$@"; }
s mkdir -p "$ROOT"/{config,data,media}
s chown -R "$(id -u):$(id -g)" "$ROOT"

if [[ "$MIGRATE" == "1" && -d ~/plum-test && ! -f "$ROOT/data/settings.json" ]]; then
    echo "    first deploy — importing ~/plum-test state"
    # settings.json IS the unit's configuration: endpoints, device names, visualiser prefs, audio
    # output. Importing it means the container comes up as the same unit the rig has been testing,
    # not a factory-default one.
    [[ -f ~/plum-test/settings.json ]] && cp -a ~/plum-test/settings.json "$ROOT/data/settings.json"
    # go-librespot's state.json holds each Spotify endpoint's authorisation — lose it and every
    # endpoint has to be re-picked from a Spotify client before it will play.
    for d in go-librespot shairport bluetooth; do
        [[ -d ~/plum-test/$d ]] && cp -a ~/plum-test/$d "$ROOT/data/$d"
    done
    # Sockets and lockfiles are per-process liveness artefacts; a stale one makes dbus-daemon
    # refuse to bind or go-librespot think another instance owns the endpoint.
    find "$ROOT/data" \( -name '*.socket' -o -name 'lockfile' -o -name '*.pid' \) -delete 2>/dev/null || true
    echo "    imported: $(cd "$ROOT/data" && ls -d * 2>/dev/null | tr '\n' ' ')"
else
    echo "    keeping existing $ROOT/data (settings.json $( [[ -f "$ROOT/data/settings.json" ]] && echo present || echo absent ))"
fi
EOS

    # 5. Image — either scp+load a local build, or have the unit pull a published one.
    if [[ -n "$IMAGE_REF" ]]; then
        say "$host — pulling ${IMAGE_REF}"
        ssh_ "$host" "bash -s -- '$PW' '$IMAGE_REF'" <<'EOS' || return 1
set -euo pipefail
PW="$1"; REF="$2"
s() { echo "$PW" | sudo -S -p '' "$@"; }
# Pull explicitly rather than letting `compose up` do it implicitly. A registry failure here is
# reported against the unit that had it, before anything is torn down — whereas compose pulling
# mid-`up` fails after the old container is already gone.
s docker pull "$REF" 2>&1 | sed 's/^/    /'
s docker image inspect "$REF" --format '    {{.Id}}  {{.Architecture}}  {{index .RepoDigests 0}}'
EOS
    else
        say "$host — loading $(basename "$TARBALL")"
        scp_ "$TARBALL" "$host" "/tmp/plum-audio-image.tar.gz" || return 1
        # Every deploy leaves another ~600 MB image behind, and nothing ever removed them. On a 29 GB
        # SD card that is roughly forty deploys to a FULL DISK — reached on both rig units in one
        # afternoon. The failure is nasty rather than obvious: the image loads, the compose file is
        # truncated to nothing, and the unit ends up with no container. Keep the tag being deployed
        # plus KEEP_IMAGES previous ones, and fail loudly if there is still no room afterwards.
        ssh_ "$host" "bash -s -- '$PW' '$PLUM_IMAGE_NAME' '$PLUM_IMAGE_TAG' '$KEEP_IMAGES'" <<'EOS' || return 1
set -euo pipefail
PW="$1"; IMAGE_NAME="$2"; IMAGE_TAG="$3"; KEEP="$4"
s() { echo "$PW" | sudo -S -p '' "$@"; }

# Ordered newest-first by creation, so "keep the last N" means what it says. `latest` and the tag we
# are about to deploy are never candidates.
stale="$(s docker images "$IMAGE_NAME" --format '{{.Tag}}\t{{.CreatedAt}}' 2>/dev/null \
    | grep -vE "^(latest|${IMAGE_TAG})\s" | sort -k2 -r | awk -v k="$KEEP" 'NR>k{print $1}')"
for t in $stale; do
    echo "    pruning old image ${IMAGE_NAME}:${t}"
    s docker rmi -f "${IMAGE_NAME}:${t}" >/dev/null 2>&1 || true
done

free_kb="$(df -Pk / | awk 'NR==2{print $4}')"
if [[ "$free_kb" -lt 1500000 ]]; then
    echo "    !! only $((free_kb / 1024)) MB free after pruning — refusing to deploy into a full disk" >&2
    exit 1
fi

s docker load -i /tmp/plum-audio-image.tar.gz | sed 's/^/    /'
rm -f /tmp/plum-audio-image.tar.gz
EOS
    fi

    # 6. compose + per-unit env. TZ and PUID/PGID are read from the unit itself so files under
    #    /opt/plum-audio stay owned by the account that administers it.
    say "$host — config"
    put_ "$host" "${HERE}/docker-compose.yml" "/tmp/plum-audio-compose.yml" || return 1
    ssh_ "$host" "bash -s -- '$PW' '$REMOTE_ROOT' '$unit_id' '$unit_name' '$player_id' '$player_name' '$dac' '$profile' '$player_enabled' '$PLUM_IMAGE_NAME' '$PLUM_IMAGE_TAG' '${PLUM_FLEET_PSK:-}'" <<'EOS' || return 1
set -euo pipefail
PW="$1"; ROOT="$2"; UNIT_ID="$3"; UNIT_NAME="$4"; PLAYER_ID="$5"; PLAYER_NAME="$6"; DAC="$7"
PROFILE="$8"; PLAYER_ENABLED="$9"; IMAGE_NAME="${10}"; IMAGE_TAG="${11}"
# Passed as an ARG, not referenced in the heredoc below. The heredoc is expanded on the REMOTE host,
# where a local-only variable is unbound — and under `set -u` that aborts AFTER `cat >` has already
# truncated the file, leaving a unit with an EMPTY plum-audio.env. It then boots on entrypoint
# defaults with no DAC device and no unit id, which reads as a broken image rather than a bad deploy.
FLEET_PSK="${12:-}"
s() { echo "$PW" | sudo -S -p '' "$@"; }
mv /tmp/plum-audio-compose.yml "$ROOT/docker-compose.yml"
TZ_HOST="$(timedatectl show -p Timezone --value 2>/dev/null || echo UTC)"
cat > "$ROOT/plum-audio.env" <<ENV
# Generated by docker/deploy.sh — edit units.conf and redeploy rather than hand-editing.
PUID=$(id -u)
PGID=$(id -g)
UMASK=002
TZ=${TZ_HOST}
DEBUG=false

PLUM_UNIT_ID=${UNIT_ID}
PLUM_UNIT_NAME=${UNIT_NAME}
PLUM_LOCAL_PLAYER_ID=${PLAYER_ID}
PLUM_PLAYER_NAME=${PLAYER_NAME}

PLUM_DAC_DEVICE=${DAC}
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

# Whether an ENCRYPTED-but-unpaired client may play (the sentinel-PSK path). Off by default now that
# real pairing exists — it is encrypted but UNAUTHENTICATED, which the spec calls MITM-vulnerable.
# This is only the deploy-time default: a choice made in the GUI is stored in settings.json and wins
# from then on, including across upgrades. It does NOT affect cleartext clients (ESP32 speakers,
# Music Assistant, the web GUI) — they never reach this gate.
PLUM_UNPAIRED_ACCESS=0

# The fleet's shared Pairing PSK — identical on every unit, which is what lets a unit's server pair
# with any unit's speaker with no operator step. Generated once into docker/.deploy.env.
PLUM_FLEET_PSK=${FLEET_PSK}

# Optional 8-digit static pairing PIN, offered as a pairing method for this unit's speaker. Must be
# EXACTLY 8 digits or it is refused with a log line. The spec gesture-gates every static-PIN attempt,
# so this is convenience, not unattended pairing — someone still confirms in the GUI.
#PLUM_STATIC_PIN=
ENV
# COMPOSE_PROFILES has to be here, not in plum-audio.env: env_file is container environment, while
# this is compose INTERPOLATION. Written beside the compose file so a bare `docker compose up -d` or
# `restart` run by hand in this directory selects the same service deploy.sh does.
#
# PLUM_IMAGE/PLUM_TAG ride here for the same reason: which image compose resolves is interpolation,
# decided before the container exists. Pinning both means a hand-run `docker compose up -d` in this
# directory starts the image this deploy chose, not whatever `latest` has drifted to since.
cat > "$ROOT/.env" <<COMPOSEENV
# Generated by docker/deploy.sh — selects which service in docker-compose.yml applies to this host,
# and which image it runs.
COMPOSE_PROFILES=${PROFILE}
PLUM_IMAGE=${IMAGE_NAME}
PLUM_TAG=${IMAGE_TAG}
COMPOSEENV
echo "    $ROOT/plum-audio.env  (tz=${TZ_HOST}, uid=$(id -u):$(id -g), profile=${PROFILE})"
echo "    $ROOT/.env            (image=${IMAGE_NAME}:${IMAGE_TAG})"
EOS

    # 7 + 8. Up, then prove it actually serves — a running container says nothing about whether
    # supervisord's tree came up.
    # Check the POST-CONDITION, not just the exit status. This step's entire purpose is that two
    # files exist and are non-empty, and the observed failure was exactly that they were not: a full
    # disk truncated docker-compose.yml to zero bytes while the step still reported success. An exit
    # status describes what a command believed; this describes what the unit actually has.
    ssh_ "$host" "test -s '$REMOTE_ROOT/docker-compose.yml' && test -s '$REMOTE_ROOT/plum-audio.env'" || {
        warn "$host: docker-compose.yml or plum-audio.env is missing/empty after the config step"
        return 1
    }

    say "$host — up"
    # `|| return 1` is load-bearing: this is the step that decides whether the unit is actually
    # RUNNING, and it was the one ssh_ call without it. A full disk truncated docker-compose.yml,
    # compose refused it as an "empty compose file", and the run still printed "all units deployed"
    # while BOTH units were left with no container at all. Any step that can leave a unit down has
    # to be able to fail that unit.
    ssh_ "$host" "bash -s -- '$PW' '$REMOTE_ROOT' '$expected_programs' '$profile' '$PLUM_IMAGE_NAME' '$PLUM_IMAGE_TAG'" <<'EOS' || return 1
set -euo pipefail
PW="$1"; ROOT="$2"; WANT="$3"; PROFILE="$4"; IMAGE_NAME="$5"; IMAGE_TAG="$6"
# Belt and braces alongside $ROOT/.env: compose v1 (the Debian units) and v2 (the Docker-CE one)
# differ in how they pick .env up, and selecting no profile silently starts NOTHING rather than
# failing — a deploy that looks like it worked and left the unit down.
export COMPOSE_PROFILES="$PROFILE"
s() { echo "$PW" | sudo -S -p '' "$@"; }
cd "$ROOT"
# Plugin (`docker compose`) on the Docker-CE unit, standalone (`docker-compose`) on the Debian ones.
if s docker compose version >/dev/null 2>&1; then DC="docker compose"; else DC="docker-compose"; fi
# Through `env`, not an export: s() shells out via sudo, which strips the environment, so an exported
# COMPOSE_PROFILES would never reach compose — and an unset profile starts NOTHING while exiting 0.
# PLUM_IMAGE/PLUM_TAG come along for the same reason. They are also in $ROOT/.env, but compose v1 and
# v2 disagree about when that file is read, and an unresolved image name is a confusing failure.
s env COMPOSE_PROFILES="$PROFILE" PLUM_IMAGE="$IMAGE_NAME" PLUM_TAG="$IMAGE_TAG" $DC up -d 2>&1 | sed 's/^/    /'

# Wait on OUR process tree, not on a port. Under host networking a port can be answered by
# something that is not this container (that is exactly how a stale host nginx passed a GUI check),
# so supervisord's own view is the only honest readiness signal.
SUPCTL="supervisorctl -c /app/supervisord/supervisord.conf"
# Wait for every DECLARED program, not a hardcoded four: a headless unit has three (no
# sendspin_player), and `-ge 4` would spin the full 90s on every deploy and then pass anyway — slow
# enough to be mistaken for a real fault. Counting non-empty status lines rather than trusting WANT
# alone means an unexpected program count still resolves, and the strict check below still judges it.
for i in $(seq 1 45); do
    st="$(s docker exec plum-audio $SUPCTL status 2>/dev/null || true)"
    tot="$(grep -c . <<<"$st" || true)"
    run="$(grep -c RUNNING <<<"$st" || true)"
    [[ "$tot" -ge "$WANT" && "$run" -eq "$tot" ]] && break
    sleep 2
done

fail=0
notrunning="$(s docker exec plum-audio $SUPCTL status 2>/dev/null | grep -v RUNNING || true)"
if [[ -n "$notrunning" ]]; then
    printf '    \033[31mFAIL\033[0m %-22s %s\n' "supervisord" "not all programs RUNNING"; fail=1
fi
chk() {  # chk <label> <url> [jq-ish grep]
    if out="$(curl -fsS -m 5 "$2" 2>&1)"; then
        printf '    \033[32mOK\033[0m   %-22s %s\n' "$1" "$(echo "$out" | head -c 90 | tr -d '\n')"
    else
        printf '    \033[31mFAIL\033[0m %-22s %s\n' "$1" "$out"; fail=1
    fi
}
chk "config API :5002"  "http://127.0.0.1:5002/api/settings"
# Polled, unlike the others: the mesh API is served from INSIDE the audio event loop, so it comes up
# a little after supervisord reports sendspin_server RUNNING. A one-shot curl here raced it and
# reported a hard FAIL against an API that was answering peers seconds later.
for i in $(seq 1 10); do
    curl -fsS -m 5 "http://127.0.0.1:5001/api/mesh/view" >/dev/null 2>&1 && break
    sleep 2
done
chk "mesh API :5001"    "http://127.0.0.1:5001/api/mesh/view"
chk "web GUI :80"       "http://127.0.0.1/"
echo "    sendspin server :8927 $(s ss -ltn | grep -q ':8927' && echo listening || echo 'NOT LISTENING')"
# Every check above passes on a unit that renders SILENCE. Under aiosendspin 9.x a client can be
# admitted, negotiated, grouped and at the right volume while activated for no roles — supervisord
# is green, all three APIs answer, both ports listen, and the room is quiet. `active_roles` is the
# only signal that separates the two, so the deploy asks for it directly. Poll, because the server
# dials the local player a few seconds after start.
if [[ "$WANT" -ge 4 ]]; then
    # Deliberately curl-on-the-host, NOT `s docker exec ... python3 -`: s() pipes the sudo password
    # into stdin, so anything reading stdin gets the password instead of its script. That cost one
    # false FAIL on this check's first real run.
    act=""
    for i in $(seq 1 15); do
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
    # Attached but NOT activated: the silent-failure signature this check exists for. A speaker in
    # this state joins the group at the right volume and renders nothing, with no error at either end.
    print("NONE " + repr([(p.get("player_id", "?")[:12], p.get("active_roles")) for p in players]))
elif (unit or {}).get("has_player") is False:
    print("OK no player on this unit (audio.output.device=none)")
else:
    # No player attached at all is the NORMAL resting state since we stopped holding our own
    # player: an idle unit releases it so a foreign server can claim the speaker. Routing dials it
    # back. Asserting "attached at boot" here would fail every healthy unit.
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
    echo "    identity: $(s docker exec plum-audio sh -c 'ls /config/identity 2>/dev/null | tr "\n" " "' || echo MISSING)"
fi
if [[ "$WANT" -lt 4 ]]; then
    # No player by design — reporting NOT LISTENING here would cry wolf on every headless deploy.
    echo "    sendspin player :8928 not started (this unit has no audio output)"
else
    echo "    sendspin player :8928 $(s ss -ltn | grep -q ':8928' && echo listening || echo 'NOT LISTENING')"
fi
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
exit $fail
EOS
}

rc=0
for h in "${HOSTS[@]}"; do
    deploy_one "$h" || { rc=1; warn "$h FAILED"; }
done

say "done"
[[ $rc -eq 0 ]] && echo "all units deployed" || echo "one or more units failed — see above"
exit $rc
