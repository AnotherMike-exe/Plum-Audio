#!/usr/bin/env bash
# Tier 3 — auto-follow ("slave" mode) with the edge/override semantics in backend/scripts/mesh/follow.py.
#
#   ./t3_autofollow.sh <unit-a-host> <unit-b-host>
#
# B follows A. Players boot IDLE now (no home-source pre-attach), so the test drives A "playing" by
# routing A's own player onto its fed source — that group is what B mirrors. It then checks:
#   1. idle B auto-joins A's stream (follow);
#   2. selecting None on B HOLDS while A keeps playing (a local override always wins — the reported bug);
#   3. when A goes idle while B is idle, B's override is reset, and A's next stream is followed again
#      (the master-idle reset).
# Silent feeds (dd from /dev/zero) stand in for real senders, same trick as t2_source_lifecycle.sh.
source "$(dirname "$0")/lib.sh"
A="${1:?usage: t3_autofollow.sh <unit-a-host> <unit-b-host>}"
B="${2:?usage: t3_autofollow.sh <unit-a-host> <unit-b-host>}"

echo "== Tier 3: auto-follow, edge/override semantics ($B follows $A) =="

A_UNIT="$(ssh_json "$A" /api/mesh/view "next((u[\"unit_id\"] for u in d[\"units\"] if u[\"host\"]==\"$A\"), \"\")")"
SRC_A="$(ssh_json "$A" /api/mesh/view "next((s[\"source_id\"] for u in d[\"units\"] if u[\"host\"]==\"$A\" for s in u[\"sources\"]), \"\")")"
PLAYER_A="$(ssh_json "$A" /api/mesh/view "next((p[\"player_id\"] for u in d[\"units\"] if u[\"host\"]==\"$A\" for p in u[\"players\"]), \"\")")"
PLAYER_B="$(ssh_json "$A" /api/mesh/view "next((p[\"player_id\"] for u in d[\"units\"] if u[\"host\"]==\"$B\" for p in u[\"players\"]), \"\")")"
[[ -n "$A_UNIT" && -n "$SRC_A" && -n "$PLAYER_A" && -n "$PLAYER_B" ]] || {
    _no "could not resolve unit/source/player ids from the mesh view (are both units up and discovered?)"
    finish; exit; }
echo "  $B/$PLAYER_B following $A_UNIT ($A); A's source=$SRC_A, A's player=$PLAYER_A"

FIFO_A="/tmp/${SRC_A}-fifo"

HOME_B="$(ssh_json "$B" /api/mesh/view "next((s[\"source_id\"] for u in d[\"units\"] if u[\"host\"]==\"$B\" for s in u[\"sources\"]), \"\")")"
# -- teardown, LIFO: settings disabled, A player home/idle, B re-homed to its OWN unit (routing it
# onto its home source reclaims it back from A), feeder killed. Just unrouting B off A would leave
# it DETACHED on A, breaking the next run's resolve — so route B home, then leave it idle. --------
defer "curl_ \"$B\" POST /api/settings \"{\\\"autoSwitch\\\":{\\\"localActivity\\\":false,\\\"slave\\\":{\\\"enabled\\\":false,\\\"masterUnitId\\\":null}}}\" >/dev/null 2>&1; true"
defer "curl_ \"$B\" POST /api/mesh/unroute \"{\\\"player_id\\\":\\\"$PLAYER_B\\\",\\\"source_id\\\":\\\"$HOME_B\\\"}\" >/dev/null 2>&1; true"
defer "curl_ \"$B\" POST /api/mesh/route \"{\\\"player_id\\\":\\\"$PLAYER_B\\\",\\\"source_id\\\":\\\"$HOME_B\\\"}\" >/dev/null 2>&1; sleep 2; true"
defer "curl_ \"$A\" POST /api/mesh/unroute \"{\\\"player_id\\\":\\\"$PLAYER_A\\\",\\\"source_id\\\":\\\"$SRC_A\\\"}\" >/dev/null 2>&1; true"
defer "ssh_ \"$A\" \"pkill -f 'dd if=/dev/zero of=$FIFO_A' 2>/dev/null; true\""

in_group() {  # in_group <player-id> -> is it in SRC_A's player_ids, per A's aggregated view?
    ssh_json "$A" /api/mesh/view \
        "\"$1\" in next((s[\"player_ids\"] for u in d[\"units\"] if u[\"host\"]==\"$A\" for s in u[\"sources\"] if s[\"source_id\"]==\"$SRC_A\"), [])"
}
feed_a() { ssh_ "$A" "setsid sh -c 'dd if=/dev/zero of=$FIFO_A bs=17640 count=$1 2>/dev/null' </dev/null >/dev/null 2>&1 &" || true; }
a_active() { ssh_json "$A" /api/mesh/snapshot "next((s[\"active\"] for s in d[\"sources\"] if s[\"source_id\"]==\"$SRC_A\"), None)"; }

# -- make A "play": feed its source and route A's own player onto it ------------------------------
feed_a 300
assert_eq "$(wait_for "True" 10 a_active)" "True" "A's source is active (test feeder)"
curl_ "$A" POST /api/mesh/route "{\"player_id\":\"$PLAYER_A\",\"source_id\":\"$SRC_A\"}" >/dev/null
assert_eq "$(wait_for "True" 10 in_group "$PLAYER_A")" "True" "A's own player is on its source (A is playing)"

# -- configure B to follow A ---------------------------------------------------------------------
curl_ "$B" POST /api/settings "{\"autoSwitch\":{\"localActivity\":false,\"slave\":{\"enabled\":true,\"masterUnitId\":\"$A_UNIT\"}}}" >/dev/null

# -- 1. idle B auto-joins A's stream -------------------------------------------------------------
# Cold start: B must poll the new slave setting, A's player must self-report onto its source, B's
# aggregator must fetch it, then the reconciler routes + reclaims — a longer window than a warm tick.
assert_eq "$(wait_for "True" 30 in_group "$PLAYER_B")" "True" "idle B auto-joined A's stream (follow)"

# -- 2. selecting None on B holds while A keeps playing (local override wins) ---------------------
curl_ "$A" POST /api/mesh/unroute "{\"player_id\":\"$PLAYER_B\",\"source_id\":\"$SRC_A\"}" >/dev/null
sleep 6  # >2 reconciler ticks
assert_eq "$(in_group "$PLAYER_B")" "False" "None on B holds — not re-followed while A still plays"

# -- 3. master-idle reset: A goes idle while B idle -> next A stream is followed again ------------
ssh_ "$A" "pkill -f 'dd if=/dev/zero of=$FIFO_A' 2>/dev/null; true"          # stop A's feed
curl_ "$A" POST /api/mesh/unroute "{\"player_id\":\"$PLAYER_A\",\"source_id\":\"$SRC_A\"}" >/dev/null  # A player idle
assert_eq "$(wait_for "False" 12 a_active)" "False" "A went idle"
sleep 4  # let B's reconciler observe the master-idle reset while B is idle
feed_a 300                                                                    # A streams again
assert_eq "$(wait_for "True" 10 a_active)" "True" "A streaming again"
curl_ "$A" POST /api/mesh/route "{\"player_id\":\"$PLAYER_A\",\"source_id\":\"$SRC_A\"}" >/dev/null
assert_eq "$(wait_for "True" 15 in_group "$PLAYER_B")" "True" "follow resumed after master-idle reset"

finish
