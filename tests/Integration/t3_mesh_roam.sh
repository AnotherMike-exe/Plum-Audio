#!/usr/bin/env bash
# Tier 3 — cross-server roam between two Plum units.
#
#   ./t3_mesh_roam.sh <unit-a-host> <unit-b-host>
#
# Reclaims unit B's player onto one of unit A's sources (the cross-server path), asserts the player
# lands in A's group and is gone from B, then routes it home and asserts the reverse. This exercises
# the loopback-reclaim-URL fix (a peer that advertised ws://127.0.0.1 would be dialed on OUR
# loopback) — the bug that made roam "time out" before the router rewrote the host.
source "$(dirname "$0")/lib.sh"
A="${1:?usage: t3_mesh_roam.sh <unit-a-host> <unit-b-host>}"
B="${2:?usage: t3_mesh_roam.sh <unit-a-host> <unit-b-host>}"

echo "== Tier 3: cross-server roam ($A <-> $B) =="

# Each unit's own player id, and unit A's first source, from A's aggregated view.
PLAYER_B="$(ssh_json "$A" /api/mesh/view \
    "next((p[\"player_id\"] for u in d[\"units\"] if u[\"host\"]==\"$B\" for p in u[\"players\"]), \"\")")"
SRC_A="$(ssh_json "$A" /api/mesh/view \
    "next((s[\"source_id\"] for u in d[\"units\"] if u[\"host\"]==\"$A\" for s in u[\"sources\"]), \"\")")"
HOME_B="$(ssh_json "$B" /api/mesh/view \
    "next((s[\"source_id\"] for u in d[\"units\"] if u[\"host\"]==\"$B\" for s in u[\"sources\"]), \"\")")"
[[ -n "$PLAYER_B" && -n "$SRC_A" && -n "$HOME_B" ]] || {
    _no "could not resolve player/source from the mesh view (are both units up and discovered?)"
    finish; exit; }
echo "  roaming $PLAYER_B onto $A/$SRC_A, then home to $B/$HOME_B"

# Always leave B's player back on its own unit.
defer "curl_ \"$B\" POST /api/mesh/route \"{\\\"player_id\\\":\\\"$PLAYER_B\\\",\\\"source_id\\\":\\\"$HOME_B\\\"}\" >/dev/null 2>&1; true"

in_group() {  # in_group <unit-host-in-view> <source> -> is PLAYER_B in that source's player_ids?
    ssh_json "$A" /api/mesh/view \
        "\"$PLAYER_B\" in next((s[\"player_ids\"] for u in d[\"units\"] if u[\"host\"]==\"$1\" for s in u[\"sources\"] if s[\"source_id\"]==\"$2\"), [])"
}

# -- roam A <- B --------------------------------------------------------------------------------
curl_ "$A" POST /api/mesh/route "{\"player_id\":\"$PLAYER_B\",\"source_id\":\"$SRC_A\"}" >/dev/null
landed="$(wait_for "True" 12 in_group "$A" "$SRC_A")"
assert_eq "$landed" "True" "player-B reclaimed cross-server onto A/$SRC_A"

# -- roam home ----------------------------------------------------------------------------------
curl_ "$B" POST /api/mesh/route "{\"player_id\":\"$PLAYER_B\",\"source_id\":\"$HOME_B\"}" >/dev/null
home="$(wait_for "True" 12 in_group "$B" "$HOME_B")"
assert_eq "$home" "True" "player-B roamed back home to B/$HOME_B"
still_on_a="$(in_group "$A" "$SRC_A")"
assert_eq "$still_on_a" "False" "player-B no longer on A after roaming home"

finish
