#!/usr/bin/env bash
# Tier 4 — can a peer that stops answering WITHOUT closing wedge our player? (OPEN-ITEMS #23)
#
#   ./t4_player_halfopen.sh <unit-a> <unit-b>
#
# 80 cycles of soak across two churn profiles did not reproduce the wedge, and the reason is worth
# stating: every disconnect those profiles produce is a CLEAN one. The peer closes, the player's
# `attach_websocket` returns, and the session is logged as ended. The wedge is the opposite — the
# session never ends — so the soak was never going to find it.
#
# `attach_websocket` blocks for the whole life of a connection. For it to block forever the peer
# must stop answering without ever closing: a half-open socket. `docker pause` models that exactly
# — the process freezes mid-connection, the socket stays open, and nothing is ever sent to end it.
# That is not a contrived condition; it is what a power cut, a crashed unit or a dropped link looks
# like from the other end, and the observed consequence (a speaker unreachable until its container
# is restarted) matches the wedge precisely.
#
# Sequence: attach A's player to B's server -> freeze B -> confirm A's session does NOT end ->
# restart B so it has no memory of the client -> try to use A's speaker again.
#
# ALWAYS unfreezes B, however this exits. A paused container is invisible in `docker ps` output that
# people skim, and leaving one paused would take that unit off the air silently.
source "$(dirname "$0")/lib.sh"
A="${1:?usage: t4_player_halfopen.sh <unit-a> <unit-b>}"
B="${2:?need unit-b}"

echo "== Tier 4: half-open peer — does it wedge the player? ($A player, $B frozen) =="

host_docker() { ssh_ "$1" "echo '$PW' | sudo -S -p '' docker $2 plum-audio" >/dev/null 2>&1; }

own_player() { ssh_json "$1" /api/mesh/snapshot 'json.dumps((d.get("local_player") or {}).get("player_id") or "")' | tr -d '"'; }
PA="$(wait_for_nonempty 30 own_player "$A")"
SRC_B="$(ssh_json "$B" /api/mesh/snapshot 'd["sources"][0]["source_id"] if d["sources"] else ""')"
SRC_A="$(ssh_json "$A" /api/mesh/snapshot 'd["sources"][0]["source_id"] if d["sources"] else ""')"
[[ -n "$PA" && -n "$SRC_A" && -n "$SRC_B" ]] || { _no "could not resolve player/sources"; finish; exit; }
echo "  A's player=${PA:0:12}…   B's source=$SRC_B   A's source=$SRC_A"

# Unfreezing B is not optional — see the header.
defer "host_docker \"$B\" unpause; true"
defer "stop_feed_ \"$B\" \"/tmp/${SRC_B}-fifo\""

AUTO_A="$(ssh_json "$A" /api/settings 'json.dumps(d.get("autoSwitch") or {})')"
AUTO_B="$(ssh_json "$B" /api/settings 'json.dumps(d.get("autoSwitch") or {})')"
restore_auto() { [[ -z "$2" || "$2" == "{}" ]] && return; curl_ "$1" POST /api/settings "{\"autoSwitch\":$2}" >/dev/null 2>&1; }
defer "restore_auto \"$A\" \"\$AUTO_A\""
defer "restore_auto \"$B\" \"\$AUTO_B\""
for _u in "$A" "$B"; do
    curl_ "$_u" POST /api/settings \
        '{"autoSwitch":{"localActivity":false,"slave":{"enabled":false,"masterUnitId":null}}}' >/dev/null
done

# -- 1. put A's speaker on B's server ---------------------------------------------------------------

feed_fifo_ "$B" "/tmp/${SRC_B}-fifo" 600 || true
sleep 3
curl_ "$B" POST /api/mesh/route "{\"player_id\":\"$PA\",\"source_id\":\"$SRC_B\"}" >/dev/null

on_b() { ssh_json "$B" /api/mesh/snapshot "any(p[\"player_id\"]==\"$PA\" for p in d[\"players\"])"; }
assert_eq "$(wait_for "True" 20 on_b)" "True" "A's speaker is attached to B's server"

sessions_closed() { dexec_ "$A" sh -c "'grep -c \"detached from server\" /config/logs/sendspin_player.log'" 2>/dev/null | tr -d '\r'; }
CLOSED_BEFORE="$(sessions_closed)"

# -- 2. freeze B mid-connection ---------------------------------------------------------------------

host_docker "$B" pause
echo "  B frozen; waiting 45s to see whether A notices"
sleep 45

CLOSED_AFTER="$(sessions_closed)"
if [[ "$CLOSED_AFTER" == "$CLOSED_BEFORE" ]]; then
    _ok "A's session did NOT end while B was frozen (the socket is half-open, as predicted)"
else
    _no "A's session ended on its own ($CLOSED_BEFORE -> $CLOSED_AFTER) — the player DOES notice a frozen peer" \
        "then the wedge has another cause; this hypothesis is dead"
fi

# -- 3. THE REAL TEST: dial the speaker while the socket is still half-open ---------------------------
#
# This is the step that matters, and an earlier version of this test missed it by restarting B
# first. Restarting a container tears down its network namespace, which RESETS the connection — so
# the player finally saw a close, `attach_websocket` returned, and everything worked. A half-open
# socket on its own does not wedge anything.
#
# The wedge needs a dial to arrive WHILE the socket is still open to nobody. That is the ordinary
# way a person meets this: a peer loses power, and they then try to play something in their own
# room. The speaker is holding the one websocket a client is allowed, on a server that no longer
# exists, so the dial has nothing to answer it.

on_a() { ssh_json "$A" /api/mesh/snapshot "any(p[\"player_id\"]==\"$PA\" for p in d[\"players\"])"; }

echo "  dialing A's own speaker while B is STILL frozen"
curl_ "$A" POST /api/mesh/route "{\"player_id\":\"$PA\",\"source_id\":\"$SRC_A\"}" >/dev/null 2>&1
got="$(wait_for "True" 30 on_a)"

if [[ "$got" == "True" ]]; then
    _ok "A's speaker answered a dial even with its old peer frozen (this path does not wedge)"
    WEDGED=0
else
    _no "A's speaker is WEDGED — it would not answer a dial while holding a half-open connection" \
        "this is OPEN-ITEMS #23 reproduced deliberately"
    WEDGED=1
    echo "  ---- diagnostics ----"
    dexec_ "$A" sh -c "'grep -E \"server dialed us|detached from server|has been attached\" /config/logs/sendspin_player.log | tail -8'" 2>/dev/null
    dexec_ "$A" sh -c "'grep -E \"reclaim|timed out\" /config/logs/sendspin_server.log | tail -5'" 2>/dev/null
fi

# -- 4. does it recover once the peer comes back? -----------------------------------------------------
#
# Worth knowing either way: if the speaker frees itself when the dead peer's connection is finally
# reset, the bug is "unreachable until the peer returns" rather than "unreachable until restarted",
# and that is a materially different severity.

host_docker "$B" unpause
host_docker "$B" restart
echo "  B restarted; waiting for it to come back"
b_up() { ssh_json "$B" /api/mesh/snapshot 'json.dumps(bool(d.get("unit_id")))' | tr -d '"'; }
wait_for "true" 90 b_up >/dev/null
sleep 10

curl_ "$A" POST /api/mesh/route "{\"player_id\":\"$PA\",\"source_id\":\"$SRC_A\"}" >/dev/null 2>&1
recovered="$(wait_for "True" 40 on_a)"
if [[ "$recovered" == "True" ]]; then
    if [[ "${WEDGED:-0}" -eq 1 ]]; then
        _ok "and it recovers by itself once the peer's connection is reset (no restart needed)"
    else
        _ok "still reachable after the peer returned"
    fi
else
    _no "A's speaker is STILL unreachable after the peer came back — only a restart clears it"
fi

curl_ "$A" POST /api/mesh/unroute "{\"player_id\":\"$PA\",\"source_id\":\"$SRC_A\"}" >/dev/null 2>&1
finish
