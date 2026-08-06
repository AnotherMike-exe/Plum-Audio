#!/usr/bin/env bash
# Plum-Audio — commission a fresh unit's HOST, from the workstation.
#
#   ./provision.sh all                           # every unit in docker/units.conf
#   ./provision.sh 192.0.2.10               # one unit
#   ./provision.sh all --overlay hifiberry-amp100 --unity   # a unit with an audio HAT (reboots)
#   ./provision.sh all --with-bluez              # + rebuild bluetoothd for AVRCP position (~30 min)
#   ./provision.sh all --check                   # report only, change nothing
#
# WHY THIS EXISTS
#   docs/HOST-PROVISIONING.md is the authority on WHAT has to be true on a host and why. But every
#   command in it runs ON the unit, against files that live in THIS repo — and a freshly imaged Pi
#   has no copy of the repo and no git remote to fetch one from. Before this script the gap was
#   filled by hand, differently each time, which is exactly how a unit ends up missing the one file
#   whose absence is silent (see the bluealsa D-Bus policy: 178 respawns on .7.204).
#
#   So: this pushes the host-setup payload to the unit and runs the checklist, idempotently. It is
#   the step BEFORE docker/deploy.sh, and it needs to run exactly once per image — not per deploy.
#
# WHAT IT DOES, in HOST-PROVISIONING.md's order
#   1. audio HAT overlay + mixer   — only with --overlay / --unity; skipped otherwise, because a
#                                    unit using the Pi's onboard output needs nothing here
#   2. rfkill unblock bluetooth    — BlueZ will not clear a soft block itself
#   3. patched bluetoothd          — only with --with-bluez (~30 min, and optional: an unpatched
#                                    unit plays audio, it just cannot report a scrub)
#      + Experimental = true       — always: without it cover art fails with no error at all
#   4. mask the distro user obexd  — a no-op on Raspberry Pi OS Lite, which does not ship it
#   5. bluealsa D-Bus policy       — silent catastrophe if missing; nothing installs it
#   6. stand down the host nginx   — a no-op unless the unit served the pre-container GUI
#
# It does NOT install Docker or deploy anything: docker/deploy.sh owns that, and owns it for every
# deploy rather than once per image.
set -euo pipefail

cd "$(dirname "$0")"
HERE="$PWD"
ROOT="$(cd ../.. && pwd)"

USER_="${PLUM_TEST_USER:-plum-admin}"
PW="${PLUM_TEST_PW:-REDACTED-USE-PLUM_TEST_PW}"
# UserKnownHostsFile=/dev/null, not just StrictHostKeyChecking=no: a REIMAGED unit presents a new
# host key, and a conflicting known_hosts entry makes ssh refuse the connection outright —
# StrictHostKeyChecking=no only covers a host it has never seen. Since the password is in this file,
# there is no key-pinning posture to preserve, and the alternative is telling every operator to run
# ssh-keygen -R by hand on exactly the runs where they are least expecting a failure.
SSH_OPTS="-o ConnectTimeout=20 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"
UNITS_FILE="${ROOT}/docker/units.conf"
PAYLOAD="plum-audio-hostsetup"      # lands in the unit's home directory

OVERLAY=""
DO_UNITY=0
WITH_BLUEZ=0
CHECK_ONLY=0
HOSTS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        all)          HOSTS+=("all"); shift ;;
        --overlay)    OVERLAY="$2"; shift 2 ;;
        --unity)      DO_UNITY=1; shift ;;
        --with-bluez) WITH_BLUEZ=1; shift ;;
        --check)      CHECK_ONLY=1; shift ;;
        -h|--help)    sed -n '2,40p' "$0"; exit 0 ;;
        -*)           echo "unknown flag $1" >&2; exit 2 ;;
        *)            HOSTS+=("$1"); shift ;;
    esac
done
[[ ${#HOSTS[@]} -gt 0 ]] || {
    echo "usage: provision.sh <all|host...> [--overlay <name>] [--unity] [--with-bluez] [--check]" >&2
    exit 2
}

command -v sshpass >/dev/null || { echo "sshpass required (brew install sshpass)" >&2; exit 1; }

# Same transient-auth retry as deploy.sh: a run opens several authenticated connections per unit in
# quick succession and sshd will occasionally refuse one on a password that is demonstrably correct.
retry_() {
    local n=0
    until "$@"; do
        n=$((n + 1))
        [[ $n -ge 3 ]] && return 1
        sleep 3
    done
}
ssh_() { retry_ sshpass -p "$PW" ssh $SSH_OPTS "${USER_}@$1" "${@:2}"; }
say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m    !! %s\033[0m\n' "$*"; }

units_all() { grep -vE '^\s*(#|$)' "$UNITS_FILE" | awk -F'|' '{gsub(/ /,"",$1); print $1}'; }

if [[ " ${HOSTS[*]} " == *" all "* ]]; then
    # Read loop, not `mapfile`: this is run from macOS, whose /bin/bash is 3.2.
    HOSTS=()
    while IFS= read -r _h; do
        [[ -n "$_h" ]] && HOSTS+=("$_h")
    done < <(units_all)
fi

# --- the report, which is also --check ------------------------------------------------------------

report_one() {
    ssh_ "$1" "bash -s -- '$PW'" <<'EOS'
set -uo pipefail
PW="$1"
s() { echo "$PW" | sudo -S -p '' "$@"; }
ok()   { printf '    \033[32mOK\033[0m   %-26s %s\n' "$1" "$2"; }
bad()  { printf '    \033[31mTODO\033[0m %-26s %s\n' "$1" "$2"; }
note() { printf '    \033[36m--\033[0m   %-26s %s\n' "$1" "$2"; }

note "host" "$(hostname) / $(dpkg --print-architecture) / $(. /etc/os-release; echo "$PRETTY_NAME")"
# [^] ]* not [^ ]*: a card id long enough to fill the padding has no space before the ']', so the
# looser class swallows the bracket and colon too (seen on .7.204's sndrpihifiberry).
note "cards" "$(sed -n 's/^ *[0-9]* \[\([^] ]*\).*/\1/p' /proc/asound/cards 2>/dev/null | tr '\n' ' ')"

systemctl is-active --quiet avahi-daemon && ok "avahi-daemon" "active" || bad "avahi-daemon" "NOT active"
systemctl is-active --quiet bluetooth    && ok "bluetoothd" "active"   || bad "bluetoothd" "NOT active"

v="$(dpkg-query -W -f='${Version}' bluez 2>/dev/null || echo none)"
case "$v" in
    *+plum*) ok  "bluez patches" "$v (held: $(s apt-mark showhold 2>/dev/null | grep -qx bluez && echo yes || echo NO))" ;;
    none)    bad "bluez" "not installed" ;;
    *)       bad "bluez patches" "$v is unpatched — no AVRCP position (--with-bluez)" ;;
esac
# >= 5.81 or MediaPlayer1.ObexPort does not exist and cover art fails with no error.
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

case "$(systemctl --user is-enabled obex.service 2>&1)" in
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

note "onboard audio" "$(grep -E '^dtparam=audio' /boot/firmware/config.txt 2>/dev/null || echo 'no dtparam=audio line')"
for c in Digital PCM Master; do
    if amixer -c 0 sget "$c" >/dev/null 2>&1; then
        note "card 0 mixer '$c'" "$(amixer -c 0 sget "$c" | grep -m1 -oE '\[[0-9]+%\] \[[-0-9.]+dB\]' || echo '?')"
        break
    fi
done
exit 0
EOS
}

# --- provisioning ---------------------------------------------------------------------------------

provision_one() {
    local host="$1"

    say "$host — before"
    report_one "$host" || return 1
    [[ "$CHECK_ONLY" == 1 ]] && return 0

    # The payload. Only what the checklist actually runs on the unit — the container image goes by
    # docker/deploy.sh, and there is no reason for a Pi to hold a copy of the whole repo.
    say "$host — pushing host-setup payload to ~/${PAYLOAD}"
    ssh_ "$host" "rm -rf '$PAYLOAD' && mkdir -p '$PAYLOAD/bluez'" || return 1
    retry_ sshpass -p "$PW" scp $SSH_OPTS \
        "${HERE}/configure-audio-hat.sh" "${USER_}@${host}:${PAYLOAD}/" || return 1
    retry_ sshpass -p "$PW" scp $SSH_OPTS \
        "${ROOT}/backend/config/bluez/"* "${USER_}@${host}:${PAYLOAD}/bluez/" || return 1
    retry_ sshpass -p "$PW" scp $SSH_OPTS \
        "${ROOT}/backend/config/bluealsa-plum-dbus.conf" "${USER_}@${host}:${PAYLOAD}/" || return 1
    ssh_ "$host" "chmod +x '$PAYLOAD/configure-audio-hat.sh' '$PAYLOAD/bluez/install_patched_bluez.sh'" || return 1

    # Steps 2, 3 (Experimental), 4, 5, 6 — all quick, all idempotent.
    say "$host — checklist"
    ssh_ "$host" "bash -s -- '$PW' '$PAYLOAD'" <<'EOS' || return 1
set -euo pipefail
PW="$1"; PAYLOAD="$2"
s() { echo "$PW" | sudo -S -p '' "$@"; }

# 2. rfkill. BlueZ answers a soft block with a bare "Failed" and cannot clear it from the container
#    (that would need /dev/rfkill plus CAP_NET_ADMIN). systemd-rfkill persists this across reboots.
if s rfkill list bluetooth | grep -q 'Soft blocked: yes'; then
    echo "    rfkill: unblocking bluetooth"
    s rfkill unblock bluetooth
else
    echo "    rfkill: already unblocked"
fi

# 3b. Experimental = true, or MediaPlayer1.ObexPort stays hidden even on bluez >= 5.81 and cover art
#     fails with nothing in any log, because we never get as far as asking.
if grep -qE '^Experimental *= *true' /etc/bluetooth/main.conf; then
    echo "    main.conf: Experimental already true"
else
    echo "    main.conf: setting Experimental = true"
    s cp -n /etc/bluetooth/main.conf /etc/bluetooth/main.conf.plum.bak
    if grep -qE '^#?Experimental *=' /etc/bluetooth/main.conf; then
        s sed -i 's/^#*Experimental *=.*/Experimental = true/' /etc/bluetooth/main.conf
    else
        # No line at all to rewrite — append under [General], which is the only section it is read in.
        s sed -i '0,/^\[General\]/s//[General]\nExperimental = true/' /etc/bluetooth/main.conf
    fi
    grep -qE '^Experimental *= *true' /etc/bluetooth/main.conf || { echo "    !! failed to set it" >&2; exit 1; }
    RESTART_BT=1
fi

# 4. The distro's D-Bus-activated USER obexd steals the one AVRCP BIP session a phone will serve, and
#    ours is refused with ECONNREFUSED and no log line. Pi OS Lite does not ship it at all.
case "$(systemctl --user is-enabled obex.service 2>&1)" in
    masked)    echo "    user obexd: already masked" ;;
    not-found) echo "    user obexd: not installed — nothing to mask" ;;
    *)         echo "    user obexd: masking"; systemctl --user mask obex.service ;;
esac

# 5. The bluealsa system D-Bus policy. Debian's own grants own_prefix=org.bluealsa to root ONLY, and
#    the source manager spawns the daemon as our user — so without this it exits rc=1 about 3 s after
#    every start and is respawned forever (178 times on .7.204), burying unrelated diagnosis.
if s cmp -s "$PAYLOAD/bluealsa-plum-dbus.conf" /etc/dbus-1/system.d/bluealsa-plum-dbus.conf 2>/dev/null; then
    echo "    bluealsa D-Bus policy: already current"
else
    echo "    bluealsa D-Bus policy: installing"
    s install -m 0644 "$PAYLOAD/bluealsa-plum-dbus.conf" /etc/dbus-1/system.d/
    s systemctl reload dbus
fi

# 6. The host's nginx served the pre-container GUI from the same proxy config the image now ships.
#    Under host networking the container's nginx crash-loops on bind() while the host keeps
#    answering :80 — a GUI that looks perfect and is a stale build. deploy.sh also does this.
if systemctl is-enabled nginx >/dev/null 2>&1; then
    echo "    host nginx: disabling (the container owns :80)"
    s systemctl disable --now nginx >/dev/null 2>&1 || true
else
    echo "    host nginx: not installed"
fi

if [[ "${RESTART_BT:-0}" == "1" ]]; then
    echo "    restarting bluetooth"
    s systemctl restart bluetooth
fi
exit 0
EOS

    # 1. The audio HAT. Opt-in, because choosing the overlay is the operator's job — the boards on
    #    this rig expose no ID EEPROM, so there is nothing to auto-detect. A unit on the Pi's onboard
    #    output needs neither flag: the stock image already carries dtparam=audio=on and the onboard
    #    control sits at 0.00 dB.
    if [[ -n "$OVERLAY" ]]; then
        say "$host — audio HAT overlay: $OVERLAY"
        ssh_ "$host" "bash -s -- '$PW' '$PAYLOAD' '$OVERLAY'" <<'EOS' || return 1
set -euo pipefail
PW="$1"; PAYLOAD="$2"; OVERLAY="$3"
s() { echo "$PW" | sudo -S -p '' "$@"; }
s "$HOME/$PAYLOAD/configure-audio-hat.sh" --overlay "$OVERLAY" 2>&1 | sed 's/^/    /'
echo "    !! REBOOT REQUIRED before the card exists — then re-run with --unity"
EOS
    fi

    # --unity is a separate pass on purpose: it needs the card to already be enumerated, i.e. AFTER
    # the reboot the overlay demands. On a power amplifier it makes the unit louder the instant it is
    # applied — see HOST-PROVISIONING.md.
    if [[ "$DO_UNITY" == 1 ]]; then
        say "$host — pinning the HAT mixer to unity"
        ssh_ "$host" "bash -s -- '$PW' '$PAYLOAD'" <<'EOS' || return 1
set -euo pipefail
PW="$1"; PAYLOAD="$2"
s() { echo "$PW" | sudo -S -p '' "$@"; }
s "$HOME/$PAYLOAD/configure-audio-hat.sh" --unity 2>&1 | sed 's/^/    /'
EOS
    fi

    # 3a. The patched bluetoothd. ~30 min per unit and genuinely optional — nothing in our Python
    #     depends on it, an unpatched unit just cannot report a mid-track scrub from the phone. It is
    #     last because it is the only step that can fail slowly.
    if [[ "$WITH_BLUEZ" == 1 ]]; then
        say "$host — rebuilding bluetoothd with the AVRCP patches (~30 min, 2 jobs)"
        ssh_ "$host" "bash -s -- '$PW' '$PAYLOAD'" <<'EOS' || return 1
set -euo pipefail
PW="$1"; PAYLOAD="$2"
s() { echo "$PW" | sudo -S -p '' "$@"; }
# Log to /var/tmp, not /tmp: /tmp is a 1.9 GB tmpfs on Debian 13 and this build has browned out a
# Pi's 5 V rail before now, which takes the log with it exactly when it is needed.
LOG=/var/tmp/plum-bluez-install.log
s env PLUM_BLUEZ_JOBS="${PLUM_BLUEZ_JOBS:-2}" "$HOME/$PAYLOAD/bluez/install_patched_bluez.sh" 2>&1 \
    | tee "$LOG" | tail -25 | sed 's/^/    /'
echo "    full log: $LOG"
EOS
    fi

    say "$host — after"
    report_one "$host" || return 1
}

rc=0
for h in "${HOSTS[@]}"; do
    provision_one "$h" || { rc=1; warn "$h FAILED"; }
done

say "done"
if [[ "$CHECK_ONLY" == 1 ]]; then
    echo "check only — nothing was changed"
elif [[ $rc -eq 0 ]]; then
    echo "hosts provisioned — next: docker/build.sh && docker/deploy.sh all"
else
    echo "one or more hosts failed — see above"
fi
exit $rc
