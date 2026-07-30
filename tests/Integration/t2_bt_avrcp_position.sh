#!/usr/bin/env bash
# Tier 2 — AVRCP position reporting on a unit whose bluetoothd carries the Plum patches.
#
# What this exists to catch: on a STOCK bluetoothd a mid-track scrub on the phone can never reach us,
# and not because of anything in our relay. bluetoothd registers EVENT_PLAYBACK_POS_CHANGED with an
# interval of UINT32_MAX / 1000 (49.7 days) and never polls GetPlayStatus, so both of AVRCP's ways of
# reporting a position are switched off. backend/config/bluez/ carries the patches and their
# installer; this asserts a unit actually has them, and that the poll is on the wire.
#
# Requires a phone PAIRED, CONNECTED and PLAYING. The wire checks report SKIP otherwise — they cannot
# be synthesised, and an idle link is deliberately quiet (the poll only runs while playing).
#
# NOT read-only: it bounces the AVRCP profile (see the comment at the capture, below). AVRCP only —
# A2DP audio keeps flowing and the session rebuilds itself in ~1 s, so there is nothing to undo.
#
#   ./t2_bt_avrcp_position.sh <host>

source "$(dirname "$0")/lib.sh"

UNIT="${1:?usage: t2_bt_avrcp_position.sh <unit-host>}"

# btmon and dbus-monitor both need root on the host; lib.sh only gives us the login.
sudo_() { ssh_ "$UNIT" "printf '%s\n' '$PW' | sudo -S -p '' $1"; }

AVRCP_REMOTE_UUID="0000110e-0000-1000-8000-00805f9b34fb"
# 12 s is six AVRCP_POSITION_POLL periods (2 s), so a working poll cannot miss it, and it leaves room
# for the AVRCP session to rebuild inside the window.
WINDOW=12
MIN_POLLS=3
CAP="/var/tmp/plum-t2-btmon.txt"

echo "### t2_bt_avrcp_position — $UNIT"

# --- the daemon itself -------------------------------------------------------------------------
VER="$(ssh_ "$UNIT" 'dpkg-query -W -f="\${Version}" bluez' 2>/dev/null)"
assert_contains "$VER" "+plum" "bluetoothd is a Plum-patched build ($VER)"

HELD="$(ssh_ "$UNIT" 'apt-mark showhold 2>/dev/null | grep -cx bluez' 2>/dev/null)"
assert_eq "${HELD:-0}" "1" "bluez is held (an apt upgrade cannot restore the unpatched daemon)"

if [[ "$VER" != *"+plum"* ]]; then
    printf '  \033[33mSKIP\033[0m wire checks — install the patches first:\n'
    printf '        scp -r backend/config/bluez %s:~/ && sudo ~/bluez/install_patched_bluez.sh\n' "$UNIT"
    finish; exit
fi

# --- is there a session to observe? -------------------------------------------------------------
PLAYER="$(ssh_ "$UNIT" "busctl --system tree org.bluez --list 2>/dev/null | grep -m1 -E '/player[0-9]+\$'" 2>/dev/null)"
if [[ -z "$PLAYER" ]]; then
    printf '  \033[33mSKIP\033[0m no AVRCP player on the bus — connect a phone and start playback\n'
    finish; exit
fi
DEV="${PLAYER%/player*}"
_ok "AVRCP player exported at $PLAYER"

STATUS="$(ssh_ "$UNIT" "busctl --system get-property org.bluez '$PLAYER' org.bluez.MediaPlayer1 Status 2>/dev/null" 2>/dev/null)"
if [[ "$STATUS" != *playing* ]]; then
    printf '  \033[33mSKIP\033[0m player is %s, not playing — the poll only runs while playing\n' \
        "$(printf '%s' "${STATUS:-unknown}" | tr -d 's "')"
    finish; exit
fi
_ok "player reports playing"

# --- the poll, on the wire ----------------------------------------------------------------------
# btmon decodes AVCTP only on a channel whose L2CAP SETUP it witnessed. Attach it to a session that
# is already up and every AVRCP frame arrives as undecoded "ACL Data" — a silent zero that reads
# exactly like a broken patch (it fooled this test's first version). So bounce the AVRCP profile
# inside the capture window: DisconnectProfile/ConnectProfile on 0000110e has its own .connect and
# .disconnect in BlueZ, so it rebuilds the AVRCP session — fresh GetCapabilities and registration
# sweep — while A2DP audio is untouched.
defer "sudo_ \"rm -f $CAP\" >/dev/null 2>&1"
sudo_ "sh -c 'setsid btmon > $CAP 2>&1 < /dev/null &'" >/dev/null 2>&1
sleep 1
ssh_ "$UNIT" "busctl --system call org.bluez $DEV org.bluez.Device1 DisconnectProfile s $AVRCP_REMOTE_UUID" >/dev/null 2>&1
sleep 2
ssh_ "$UNIT" "busctl --system call org.bluez $DEV org.bluez.Device1 ConnectProfile s $AVRCP_REMOTE_UUID" >/dev/null 2>&1
sleep "$WINDOW"
sudo_ 'pkill -f "[b]tmon"' >/dev/null 2>&1

# One poll is a request AND a response, so a period contributes 2 frames.
GPS="$(sudo_ "grep -ac 'AVRCP: GetPlayStatus' $CAP" 2>/dev/null | tr -dc '0-9')"
POLLS=$(( ${GPS:-0} / 2 ))
if [[ "$POLLS" -ge "$MIN_POLLS" ]]; then
    _ok "GetPlayStatus polled while playing (${POLLS} polls in ~${WINDOW}s)"
else
    _no "GetPlayStatus polled while playing" \
        "saw ${POLLS} polls (${GPS:-0} frames) in ~${WINDOW}s, wanted >= ${MIN_POLLS} — is 0001-avrcp-poll-*.patch in the build?"
fi

# Whether the TARGET also pushes position changes decides whether 0002 (the 1 s interval) does
# anything on this device. iOS does not advertise event 0x05 at all — verified on an iPhone
# 2026-07-29 — which is exactly why the poll is the half that matters. Informational.
if [[ "$(sudo_ "grep -ac 'EventsID: 0x05' $CAP" 2>/dev/null | tr -dc '0-9')" != "0" ]]; then
    printf '  \033[36mINFO\033[0m target advertises EVENT_PLAYBACK_POS_CHANGED — 0002 (1 s interval) is live here too\n'
else
    printf '  \033[36mINFO\033[0m target does NOT advertise EVENT_PLAYBACK_POS_CHANGED (expected on iOS); only the poll can carry a scrub\n'
fi

# --- and what reaches a D-Bus client ------------------------------------------------------------
# The half our relay consumes: bluetoothd emits PropertiesChanged for Position on every GetPlayStatus
# response, whether or not the value moved. Match string without inner quotes on purpose — it has to
# survive ssh + sudo.
DBUS_MATCH="type=signal,interface=org.freedesktop.DBus.Properties,member=PropertiesChanged"
POS="$(sudo_ "sh -c 'timeout 9 dbus-monitor --system $DBUS_MATCH 2>/dev/null | grep -ac \"string \\\"Position\\\"\"'" 2>/dev/null | tr -dc '0-9')"
if [[ "${POS:-0}" -ge 3 ]]; then
    _ok "Position reaches D-Bus clients (${POS} signals in 9s)"
else
    _no "Position reaches D-Bus clients" "saw ${POS:-0} signals in 9s, wanted >= 3"
fi

echo
echo "  Scrub on the phone and watch it land within ~2 s:"
echo "    ssh $UNIT \"grep -a 'seek signal' ~/plum-test/server.log | tail\""

finish
