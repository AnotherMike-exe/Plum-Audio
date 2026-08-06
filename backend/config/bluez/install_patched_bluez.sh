#!/usr/bin/env bash
#
# Plum-Audio — build and install a bluetoothd patched for AVRCP position reporting.
#
# WHY THIS EXISTS
#   A mid-track scrub on a connected phone can never reach us on stock BlueZ, and not for any
#   reason in our own code (see backend/scripts/sources/bluetooth_avrcp.py, POSITION HANDLING).
#   bluetoothd registers EVENT_PLAYBACK_POS_CHANGED with an interval of UINT32_MAX / 1000 — 49.7
#   days — and never polls GetPlayStatus, so the only mechanisms AVRCP has for reporting a position
#   are both switched off. Both patches here are in bluetoothd because that is the only place the
#   fix can live: the host owns the radio and the AVCTP channel (a second bluetoothd would fight it
#   for hci0), exactly like the rfkill unblock and the D-Bus policy this rig already needs.
#
# WHAT IT DOES
#   Rebuilds the DISTRO source package for the version already installed — so Raspberry Pi's own
#   +rptN patches are kept — with our patches appended to debian/patches/series, at version
#   <installed>+plumN. Installs the bluez binary package only and holds it, so an apt upgrade
#   cannot quietly restore the unpatched daemon. Idempotent: re-running with the same patches and
#   the same base version exits early unless --force.
#
# USAGE (on the unit, as root — this is HOST provisioning, not part of the container)
#   sudo ./install_patched_bluez.sh              # build + install + hold
#   sudo ./install_patched_bluez.sh --force      # rebuild even if a +plum build is installed
#   sudo ./install_patched_bluez.sh --revert     # unhold + reinstall the distro package
#   sudo ./install_patched_bluez.sh --keep-src   # leave the build tree for inspection
#
# Env overrides: PLUM_BLUEZ_BUILD_DIR (default /var/tmp/plum-bluez), PLUM_BLUEZ_PATCHES
# (space-separated patch filenames, in series order; default: every *.patch here, sorted),
# PLUM_BLUEZ_JOBS (default 2 — see below).
#
# Cost: ~30 min on a Pi 4 at 2 jobs, ~1.5 GB of build-deps and objects, both reclaimed on success
# unless --keep-src.
#
# TWO PI-SPECIFIC TRAPS, both paid for on 2026-07-29:
#   * Do NOT build at -j$(nproc). A 4-core LTO build (Debian's bluez rules pass -flto) pulled enough
#     current to brown out the 5 V rail on `.100.20` and the board reset ~10 minutes in, killing the
#     build. `vcgencmd get_throttled` reported under-voltage afterwards. Hence 2 jobs by default;
#     raise it only on a unit with a known-good supply.
#   * Log somewhere other than /tmp. It is a 1.9 GB tmpfs on Debian 13, so a reset takes the build
#     log with it — exactly when you need it. /var/tmp survives, which is also why the build tree
#     lives there.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="${PLUM_BLUEZ_BUILD_DIR:-/var/tmp/plum-bluez}"
SUFFIX_BASE="plum"
MAINTAINER="Plum Solutions <65366363+AnotherMike-exe@users.noreply.github.com>"

FORCE=0
REVERT=0
KEEP_SRC=0
for arg in "$@"; do
    case "$arg" in
        --force)    FORCE=1 ;;
        --revert)   REVERT=1 ;;
        --keep-src) KEEP_SRC=1 ;;
        -h|--help)  awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "${BASH_SOURCE[0]}"; exit 0 ;;
        *)          echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

say()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m warn\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31mfail\033[0m %s\n' "$*" >&2; exit 1; }

[[ "$(id -u)" == "0" ]] || die "run as root (sudo $0)"

INSTALLED="$(dpkg-query -W -f='${Version}' bluez 2>/dev/null || true)"
[[ -n "$INSTALLED" ]] || die "bluez is not installed — provision Bluetooth first"
ARCH="$(dpkg --print-architecture)"

# --- revert ------------------------------------------------------------------------------------
if [[ "$REVERT" == "1" ]]; then
    say "unholding bluez and restoring the distro package"
    apt-mark unhold bluez >/dev/null 2>&1 || true
    CAND="$(apt-cache policy bluez | awk '/Candidate:/ {print $2}')"
    [[ -n "$CAND" && "$CAND" != *"+${SUFFIX_BASE}"* ]] || die "no distro candidate to revert to (got '${CAND:-none}')"
    apt-get install -y --allow-downgrades "bluez=${CAND}"
    systemctl restart bluetooth || warn "could not restart bluetooth — do it manually"
    say "reverted to bluez ${CAND}"
    exit 0
fi

# --- patch set ---------------------------------------------------------------------------------
if [[ -n "${PLUM_BLUEZ_PATCHES:-}" ]]; then
    read -r -a PATCHES <<< "$PLUM_BLUEZ_PATCHES"
else
    mapfile -t PATCHES < <(cd "$HERE" && ls -1 *.patch 2>/dev/null | sort)
fi
[[ "${#PATCHES[@]}" -gt 0 ]] || die "no patches found in $HERE"
for p in "${PATCHES[@]}"; do [[ -f "$HERE/$p" ]] || die "missing patch: $HERE/$p"; done
say "patches: ${PATCHES[*]}"

# The base version to build from: strip any +plumN we previously added, so repeated runs rebuild
# from the distro version rather than stacking suffixes.
BASE="${INSTALLED%%+${SUFFIX_BASE}*}"
NEW="${BASE}+${SUFFIX_BASE}1"

if [[ "$INSTALLED" == *"+${SUFFIX_BASE}"* && "$FORCE" != "1" ]]; then
    say "bluez ${INSTALLED} is already a Plum build — nothing to do (use --force to rebuild)"
    exit 0
fi

# --- deb-src ------------------------------------------------------------------------------------
# The installed version may come from the Raspberry Pi archive, whose source is only reachable with
# a deb-src line. Add throwaway ones rather than editing the host's real sources, and take them
# away again on exit.
TMP_SRC=()
cleanup() {
    for f in "${TMP_SRC[@]:-}"; do [[ -n "$f" ]] && rm -f "$f"; done
    if [[ "${#TMP_SRC[@]}" -gt 0 ]]; then apt-get update -qq >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT

if ! apt-cache showsrc bluez 2>/dev/null | grep -q "^Version: ${BASE}$"; then
    say "enabling deb-src for the archives that ship bluez"
    for f in /etc/apt/sources.list.d/*.sources; do
        [[ -e "$f" ]] || continue
        case "$f" in *plum-bluez-src-*) continue ;; esac
        grep -q '^Types: deb$' "$f" || continue
        out="/etc/apt/sources.list.d/plum-bluez-src-$(basename "$f")"
        sed 's/^Types: deb$/Types: deb-src/' "$f" > "$out"
        TMP_SRC+=("$out")
    done
    [[ "${#TMP_SRC[@]}" -gt 0 ]] || die "no deb822 .sources files to derive deb-src from — add one by hand"
    apt-get update -qq
    apt-cache showsrc bluez 2>/dev/null | grep -q "^Version: ${BASE}$" \
        || die "source for bluez ${BASE} is not available in any configured archive"
fi

# --- build ---------------------------------------------------------------------------------------
say "installing build dependencies"
apt-get install -y --no-install-recommends build-essential dpkg-dev fakeroot >/dev/null
apt-get build-dep -y bluez

rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"
cd "$BUILD_DIR"
say "fetching bluez ${BASE} source"
apt-get source "bluez=${BASE}" >/dev/null

SRC_DIR="$(find . -maxdepth 1 -type d -name 'bluez-*' | head -1)"
[[ -n "$SRC_DIR" ]] || die "unpacked source tree not found under $BUILD_DIR"
cd "$SRC_DIR"

[[ -f debian/patches/series ]] || { mkdir -p debian/patches; : > debian/patches/series; }
for p in "${PATCHES[@]}"; do
    cp "$HERE/$p" "debian/patches/$p"
    grep -qx "$p" debian/patches/series || echo "$p" >> debian/patches/series
done
# Apply the series now, with the same machinery dpkg-buildpackage uses, so a stale patch fails here
# rather than 20 minutes in. It has to be validated as an ordered SET, not patch by patch: each
# one's context can contain the previous one's additions.
dpkg-source --before-build . \
    || die "the patch series does not apply to bluez ${BASE} — regenerate it against this version"
say "patch set applies cleanly to bluez ${BASE}"

# Prepend a changelog entry so the built package sorts above the archive's and shows the patches.
{
    printf '%s (%s) unstable; urgency=medium\n\n' bluez "$NEW"
    printf '  * Plum-Audio: AVRCP position patches (backend/config/bluez/):\n'
    for p in "${PATCHES[@]}"; do printf '    - %s\n' "$p"; done
    printf '\n -- %s  %s\n\n' "$MAINTAINER" "$(date -R)"
    cat debian/changelog
} > debian/changelog.plum
mv debian/changelog.plum debian/changelog

JOBS="${PLUM_BLUEZ_JOBS:-2}"
say "building bluez ${NEW} at ${JOBS} job(s) — this takes a while"
DEB_BUILD_OPTIONS="nocheck parallel=${JOBS}" nice -n 10 dpkg-buildpackage -b -uc -us

DEB="$(ls -1 "${BUILD_DIR}"/bluez_"${NEW}"_"${ARCH}".deb 2>/dev/null | head -1)"
[[ -n "$DEB" ]] || die "expected ${BUILD_DIR}/bluez_${NEW}_${ARCH}.deb — check the build log above"

# --- install -------------------------------------------------------------------------------------
say "installing $(basename "$DEB")"
apt-mark unhold bluez >/dev/null 2>&1 || true
dpkg -i "$DEB"
apt-mark hold bluez
systemctl daemon-reload
systemctl restart bluetooth || warn "could not restart bluetooth — do it manually"

NOW="$(dpkg-query -W -f='${Version}' bluez)"
[[ "$NOW" == "$NEW" ]] || die "installed version is ${NOW}, expected ${NEW}"

if [[ "$KEEP_SRC" == "1" ]]; then
    say "build tree kept at ${BUILD_DIR}/${SRC_DIR#./}"
else
    rm -rf "$BUILD_DIR"
fi

cat <<EOF

$(say "bluez ${NOW} installed and held")

Verify (with a phone connected and playing):
  systemctl status bluetooth --no-pager | head -3
  sudo btmon | grep -iE 'GetPlayStatus|PlaybackPos|Position'     # 0x30 every ~2 s while playing
  dbus-monitor --system "type='signal',interface='org.freedesktop.DBus.Properties',\\
arg0='org.bluez.MediaPlayer1'" | grep -A3 Position

Undo:  sudo $0 --revert
EOF
