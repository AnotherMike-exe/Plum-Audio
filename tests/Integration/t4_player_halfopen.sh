#!/usr/bin/env bash
# Tier 4 — a peer stops answering WITHOUT closing. Does the player free its speaker? (OPEN-ITEMS #23)
#
#   ./t4_player_halfopen.sh <unit-a> <unit-b>
#   T4_WEDGE=1 ./t4_player_halfopen.sh <unit-a> <unit-b>     # the old wedge reproducer, see below
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
# THE DEFAULT RUN tests the fix. The player has a peer-silence watchdog
# (`PLUM_PEER_SILENCE_TIMEOUT_S`, default 30 s, in backend/scripts/sendspin_player.py). It closes a
# session whose peer answered nothing for that time, at about 33 s after the freeze.
#
# Sequence: attach A's player to B's server -> freeze B -> A's player logs the silent peer and ends
# the session inside 45 s -> dial A's speaker while B is still frozen, and it attaches -> restart B
# -> use A's speaker again.
#
# T4_WEDGE=1 is the reproducer from before the watchdog. It runs A with
# `PLUM_PEER_SILENCE_TIMEOUT_S=0`, which turns the watchdog off, and then asserts the old findings:
# the session does NOT end while B is frozen, and step 3 shows whether the dial wedges. To do that
# it adds one line to A's /opt/plum-audio/plum-audio.env and recreates A's container (about 10 s
# of silence on A). On exit it restores the file and recreates the container again. The copy of the
# file is plum-audio.env.t4: if a kill -9 leaves that file behind, move it back by hand.
#
# ALWAYS unfreezes B, however this exits. A paused container is invisible in `docker ps` output that
# people skim, and leaving one paused would take that unit off the air silently.
source "$(dirname "$0")/lib.sh"
A="${1:?usage: t4_player_halfopen.sh <unit-a> <unit-b>}"
B="${2:?need unit-b}"

WEDGE_MODE="${T4_WEDGE:-0}"
UNIT_ROOT="${PLUM_REMOTE_ROOT:-/opt/plum-audio}"

if [[ "$WEDGE_MODE" == 1 ]]; then
    echo "== Tier 4: half-open peer, watchdog OFF — does it wedge the player? ($A player, $B frozen) =="
else
    echo "== Tier 4: half-open peer — does the watchdog free the player? ($A player, $B frozen) =="
fi

host_docker() { sudo_ssh_ "$1" "docker $2 plum-audio" >/dev/null 2>&1; }
unit_up() { ssh_json "$1" /api/mesh/snapshot 'json.dumps(bool(d.get("unit_id")))' | tr -d '"'; }

# T4_WEDGE=1: run A with the peer-silence watchdog off. The env file is the container's
# environment, so the container must be recreated to read it. The last line for a key wins.
wedge_env_on() {
    # A copy that exists is the original from a run that did not restore it. Keep that one.
    sudo_ssh_ "$1" sh -c "'cd $UNIT_ROOT && { test -f plum-audio.env.t4 || cp -p plum-audio.env plum-audio.env.t4; } \
        && echo PLUM_PEER_SILENCE_TIMEOUT_S=0 >> plum-audio.env && docker compose up -d'" >/dev/null 2>&1
}
wedge_env_off() {
    sudo_ssh_ "$1" sh -c "'cd $UNIT_ROOT && test -f plum-audio.env.t4 \
        && mv -f plum-audio.env.t4 plum-audio.env && docker compose up -d'" >/dev/null 2>&1
}
silence_timeout() { dexec_ "$1" printenv PLUM_PEER_SILENCE_TIMEOUT_S 2>/dev/null | tr -d '\r'; }

if [[ "$WEDGE_MODE" == 1 ]]; then
    echo "  T4_WEDGE=1: recreating $A with PLUM_PEER_SILENCE_TIMEOUT_S=0"
    defer "wedge_env_off \"$A\"; true"
    wedge_env_on "$A"
    wait_for "true" 90 unit_up "$A" >/dev/null
    got_timeout="$(silence_timeout "$A")"
    if [[ "$got_timeout" != "0" ]]; then
        _no "could not turn the watchdog off on $A (PLUM_PEER_SILENCE_TIMEOUT_S=[$got_timeout])"
        finish; exit
    fi
else
    # A unit that already runs with the watchdog off cannot pass the default run.
    got_timeout="$(silence_timeout "$A")"
    if [[ "$got_timeout" =~ ^0+(\.0*)?$ ]]; then
        _no "$A runs with PLUM_PEER_SILENCE_TIMEOUT_S=$got_timeout — the watchdog is off" \
            "remove the line from $UNIT_ROOT/plum-audio.env, or run with T4_WEDGE=1"
        finish; exit
    fi
fi

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

player_log_count() { dexec_ "$A" sh -c "'grep -c \"$1\" /config/logs/sendspin_player.log'" 2>/dev/null | tr -d '\r'; }
sessions_closed() { player_log_count "detached from server"; }
silent_closes() { player_log_count "is held by a SILENT peer"; }
CLOSED_BEFORE="$(sessions_closed)"
SILENT_BEFORE="$(silent_closes)"
grew() { [[ "$("$1")" -gt "${2:-0}" ]] && echo yes; }   # grew <counter> <value before>

# -- 2. freeze B mid-connection ---------------------------------------------------------------------

host_docker "$B" pause

if [[ "$WEDGE_MODE" == 1 ]]; then
    # The old reproducer. With the watchdog off nothing on A notices a frozen peer.
    echo "  B frozen; waiting 45s to see whether A notices (watchdog off)"
    sleep 45

    CLOSED_AFTER="$(sessions_closed)"
    if [[ "$CLOSED_AFTER" == "$CLOSED_BEFORE" ]]; then
        _ok "A's session did NOT end while B was frozen (the socket is half-open, as predicted)"
    else
        _no "A's session ended on its own ($CLOSED_BEFORE -> $CLOSED_AFTER) — the player DOES notice a frozen peer" \
            "the watchdog is off, so this close has another cause"
    fi
else
    # The watchdog needs 30 s of silence, and it runs on the player's health poll. The log line
    # comes at about 33 s. The close then waits for the dead peer's close frame, up to 12 s, and
    # aborts the TCP connection if the session is still held.
    echo "  B frozen; A's watchdog must log the silent peer inside 45s"
    FROZEN_AT=$SECONDS
    if [[ "$(wait_for "yes" 45 grew silent_closes "$SILENT_BEFORE")" == "yes" ]]; then
        _ok "A's player logged the silent peer after about $((SECONDS - FROZEN_AT))s and started the close"
    else
        _no "A's player did NOT log a silent peer inside 45s" \
            "expected the line: session N is held by a SILENT peer ... closing it so a new dial can attach"
    fi
    if [[ "$(wait_for "yes" 20 grew sessions_closed "$CLOSED_BEFORE")" == "yes" ]]; then
        _ok "A's session ended $((SECONDS - FROZEN_AT))s after the freeze (the watchdog closed it)"
    else
        _no "A's session did NOT end after the close" \
            "the player exits 4 when a closed session stays attached: read supervisord.log on $A"
    fi
fi

# -- 3. dial the speaker while B is STILL frozen -------------------------------------------------------
#
# This is the step a person meets: a peer loses power, and they then try to play something in
# their own room.
#
# Default run: the watchdog ended the half-open session in step 2, so the listener is free. The
# dial must attach. A failure here means the watchdog closed the session and the speaker is still
# not reachable.
#
# T4_WEDGE=1: the session is still open to nobody, and this step shows whether the dial wedges.
# An earlier version of this test missed that by restarting B first. Restarting a container tears
# down its network namespace, which RESETS the connection — so the player finally saw a close,
# `attach_websocket` returned, and everything worked. The wedge needs a dial to arrive WHILE the
# socket is still half-open: the speaker holds the one websocket a client is allowed, on a server
# that no longer exists. On 2026-08-24 this path did not wedge.

on_a() { ssh_json "$A" /api/mesh/snapshot "any(p[\"player_id\"]==\"$PA\" for p in d[\"players\"])"; }

echo "  dialing A's own speaker while B is STILL frozen"
curl_ "$A" POST /api/mesh/route "{\"player_id\":\"$PA\",\"source_id\":\"$SRC_A\"}" >/dev/null 2>&1
got="$(wait_for "True" 30 on_a)"

if [[ "$got" == "True" ]]; then
    if [[ "$WEDGE_MODE" == 1 ]]; then
        _ok "A's speaker answered a dial even with its old peer frozen (this path does not wedge)"
    else
        _ok "the next dial attached A's speaker while its old peer was still frozen"
    fi
    WEDGED=0
else
    if [[ "$WEDGE_MODE" == 1 ]]; then
        _no "A's speaker is WEDGED — it would not answer a dial while holding a half-open connection" \
            "this is OPEN-ITEMS #23 reproduced deliberately"
    else
        _no "A's speaker did NOT attach to a new dial after the watchdog closed the old session" \
            "the watchdog did not free the listener"
    fi
    WEDGED=1
    echo "  ---- diagnostics ----"
    dexec_ "$A" sh -c "'grep -E \"server dialed us|detached from server|has been attached|SILENT peer|after the close\" /config/logs/sendspin_player.log | tail -8'" 2>/dev/null
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
wait_for "true" 90 unit_up "$B" >/dev/null
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
