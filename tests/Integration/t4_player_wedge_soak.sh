#!/usr/bin/env bash
# Tier 4 — hunt the player wedge: a speaker that is connected but cannot be dialled.
#
#   ./t4_player_wedge_soak.sh <unit-a> <unit-b> [cycles]      (default 20 cycles, ~15 min)
#
# OPEN-ITEMS #23. Twice on the .7 pair, a unit's own player reached a state where:
#   - its player log showed `stream_end -> idle` and then NEVER `detached from server`;
#   - the server had no such client, while the player's self-report still said `attached: true`;
#   - every `reclaim of remote player ... timed out` from then on;
#   - only `docker restart` cleared it, though the process never died.
#
# `attach_websocket` blocks for the whole life of a connection, so a missing "detached" line means
# the player is STILL INSIDE IT, holding the one websocket a client is allowed, on a connection the
# server has already abandoned. That is the hypothesis this exists to confirm or kill.
#
# It is a HUNT, not an assertion: passing means "did not reproduce in N cycles", which is weaker
# than a green tick usually implies. It stops the moment it reproduces and dumps what a fix would
# need to be written against. Isolation runs after a clean restart did not reproduce it — roam-only
# 5/5 clean, tone-only 4x19 clean — so this deliberately MIXES the kinds of churn, which is the only
# condition it has ever appeared under.
#
# Leaves the rig as it found it, and restores calibration/follow config like its tier-3 neighbours.
source "$(dirname "$0")/lib.sh"
A="${1:?usage: t4_player_wedge_soak.sh <unit-a> <unit-b> [cycles]}"
B="${2:?need unit-b}"
CYCLES="${3:-20}"

echo "== Tier 4: player wedge soak ($A + $B, $CYCLES cycles) =="

own_player() { ssh_json "$1" /api/mesh/snapshot 'json.dumps((d.get("local_player") or {}).get("player_id") or "")' | tr -d '"'; }
first_source() { ssh_json "$1" /api/mesh/view "next((s[\"source_id\"] for u in d[\"units\"] if u[\"host\"]==\"$1\" for s in u[\"sources\"]), \"\")"; }

PA="$(wait_for_nonempty 30 own_player "$A")"
PB="$(wait_for_nonempty 30 own_player "$B")"
SRC_A="$(wait_for_nonempty 30 first_source "$A")"
SRC_B="$(wait_for_nonempty 30 first_source "$B")"
[[ -n "$PA" && -n "$PB" && -n "$SRC_A" && -n "$SRC_B" ]] || {
    _no "could not resolve players/sources (A=$PA B=$PB srcA=$SRC_A srcB=$SRC_B)"; finish; exit; }
echo "  A player=${PA:0:12}… on $SRC_A   B player=${PB:0:12}… on $SRC_B"

# Auto-follow would route players behind our back and make a cycle's outcome unattributable.
AUTO_A="$(ssh_json "$A" /api/settings 'json.dumps(d.get("autoSwitch") or {})')"
AUTO_B="$(ssh_json "$B" /api/settings 'json.dumps(d.get("autoSwitch") or {})')"
restore_auto() { [[ -z "$2" || "$2" == "{}" ]] && return; curl_ "$1" POST /api/settings "{\"autoSwitch\":$2}" >/dev/null 2>&1; }
defer "restore_auto \"$A\" \"\$AUTO_A\""
defer "restore_auto \"$B\" \"\$AUTO_B\""
for _u in "$A" "$B"; do
    curl_ "$_u" POST /api/settings \
        '{"autoSwitch":{"localActivity":false,"slave":{"enabled":false,"masterUnitId":null}}}' >/dev/null
done

FIFO_A="/tmp/${SRC_A}-fifo"
FIFO_B="/tmp/${SRC_B}-fifo"
defer "stop_feed_ \"$A\" \"$FIFO_A\""
defer "stop_feed_ \"$B\" \"$FIFO_B\""
defer "curl_ \"$A\" POST /api/mesh/calibration/tone/stop >/dev/null 2>&1; true"
defer "curl_ \"$B\" POST /api/mesh/calibration/tone/stop >/dev/null 2>&1; true"

# -- the wedge predicate ---------------------------------------------------------------------------
#
# The player insists it is attached while NO unit's server lists it. Both halves matter: the
# self-report alone is briefly true during a legitimate roam, and an empty players list alone is
# just an idle speaker. Held for two reads several seconds apart so a roam in flight is not
# mistaken for the wedge.

visible_somewhere() {  # visible_somewhere <unit-to-ask> <player-id>
    ssh_json "$1" /api/mesh/view "any(\"$2\" == p[\"player_id\"] for u in d[\"units\"] for p in u[\"players\"])"
}
claims_attached() {  # claims_attached <its-own-unit>
    ssh_json "$1" /api/mesh/snapshot 'json.dumps(bool((d.get("local_player") or {}).get("attached")))' | tr -d '"'
}

is_wedged() {  # is_wedged <unit> <player-id>
    [[ "$(claims_attached "$1")" == "true" ]] || return 1
    [[ "$(visible_somewhere "$A" "$2")" == "False" ]] || return 1
    sleep 6
    [[ "$(claims_attached "$1")" == "true" ]] || return 1
    [[ "$(visible_somewhere "$A" "$2")" == "False" ]] || return 1
    return 0
}

dump_diagnostics() {  # dump_diagnostics <unit> <label>
    echo
    echo "  ---- $2 ($1) ----"
    echo "  -- player: session lifecycle (a missing 'detached' is the signature) --"
    dexec_ "$1" sh -c "'grep -E \"server dialed us|detached from server|stream_end|has been attached\" /config/logs/sendspin_player.log | tail -12'" 2>/dev/null
    echo "  -- server: reclaim outcomes --"
    dexec_ "$1" sh -c "'grep -E \"reclaim|timed out\" /config/logs/sendspin_server.log | tail -8'" 2>/dev/null
    echo "  -- supervisord (the process should still be RUNNING) --"
    dexec_ "$1" supervisorctl status 2>/dev/null
}

# -- the churn -------------------------------------------------------------------------------------

route()   { curl_ "$1" POST /api/mesh/route   "{\"player_id\":\"$2\",\"source_id\":\"$3\"}" >/dev/null 2>&1; }

# Create and destroy a real source mid-cycle. This is the churn the earlier version lacked, and the
# reason it matters: removing an endpoint tears its source down WHILE a player is attached to it,
# which is a strong candidate for leaving a connection half-open — exactly the state the wedge looks
# like from the player's side. The full suite did this (t2_endpoint_crud) during both real wedges.
add_source() {  # add_source <unit> -> prints the new endpoint id, or empty
    local raw
    raw="$(curl_ "$1" POST /api/integrations/spotify/endpoints '{"deviceName":"WedgeProbe","enabled":true}')"
    printf '%s' "$raw" | json_ 'd["endpoint"]["id"]' 2>/dev/null
}

drop_source() {  # drop_source <unit> <endpoint-id>
    [[ -n "$2" ]] && curl_ "$1" DELETE "/api/integrations/spotify/endpoints/$2" >/dev/null 2>&1
}

# Never leave a probe endpoint behind. t2_endpoint_crud once did exactly that and its orphan sat in
# a real unit's config for days, so this sweeps by NAME rather than by a remembered id — the id is
# lost if the run dies mid-cycle.
sweep_probes() {  # sweep_probes <unit>
    local ids
    ids="$(curl_ "$1" GET /api/integrations/spotify/endpoints \
        | json_ '" ".join(e["id"] for e in d["endpoints"] if e.get("deviceName") == "WedgeProbe")' 2>/dev/null)"
    for _id in $ids; do drop_source "$1" "$_id"; done
}
defer "sweep_probes \"$A\""
defer "sweep_probes \"$B\""
unroute() { curl_ "$1" POST /api/mesh/unroute "{\"player_id\":\"$2\",\"source_id\":\"$3\"}" >/dev/null 2>&1; }
tone()    { curl_ "$1" POST /api/mesh/calibration/tone "{\"player_id\":\"$2\",\"volume\":30,\"seconds\":8}" >/dev/null 2>&1; }
tone_off(){ curl_ "$1" POST /api/mesh/calibration/tone/stop >/dev/null 2>&1; }

reproduced=0
for cycle in $(seq 1 "$CYCLES"); do
    printf '  cycle %2d/%s: ' "$cycle" "$CYCLES"

    # Feeds on and off, so sources go active and then EOF — which detaches every player (true-none)
    # and is the churn a quiet source never produces.
    feed_fifo_ "$A" "$FIFO_A" 60 || true
    feed_fifo_ "$B" "$FIFO_B" 60 || true
    sleep 3

    route "$A" "$PA" "$SRC_A"; printf 'a'
    route "$A" "$PB" "$SRC_A"; printf 'B'          # cross-server reclaim of B's speaker
    sleep 4
    tone "$A" "$PA"; printf 't'                     # tone interleaved with a live group
    sleep 5
    tone_off "$A"; printf 'T'
    route "$B" "$PB" "$SRC_B"; printf 'b'           # send B's speaker home (reclaim the other way)
    sleep 3

    # A source that appears, takes a player, and is destroyed under it.
    PROBE_ID="$(add_source "$A")"
    if [[ -n "$PROBE_ID" ]]; then
        printf 's'
        PROBE_SRC="spotify-$PROBE_ID"
        # Wait for the source manager to actually bring it up before routing onto it.
        wait_for "True" 12 ssh_json "$A" /api/mesh/snapshot \
            "any(s[\"source_id\"]==\"$PROBE_SRC\" for s in d[\"sources\"])" >/dev/null
        route "$A" "$PA" "$PROBE_SRC"; printf 'r'
        sleep 4
        drop_source "$A" "$PROBE_ID"; printf 'S'    # destroyed with a player still attached
        sleep 4
    fi
    unroute "$A" "$PA" "$SRC_A"; printf 'u'
    stop_feed_ "$A" "$FIFO_A"; stop_feed_ "$B" "$FIFO_B"; printf 'e'   # EOF -> go idle -> detach all
    sleep 5

    if is_wedged "$A" "$PA"; then
        echo " WEDGED (unit A)"; dump_diagnostics "$A" "unit A"; reproduced=1; break
    fi
    if is_wedged "$B" "$PB"; then
        echo " WEDGED (unit B)"; dump_diagnostics "$B" "unit B"; reproduced=1; break
    fi

    # A speaker that is idle should still be routable. This catches the wedge's practical symptom
    # even if the predicate above misses it.
    route "$A" "$PA" "$SRC_A"
    if [[ "$(wait_for "True" 12 visible_somewhere "$A" "$PA")" != "True" ]]; then
        echo " UNROUTABLE (unit A: idle speaker refused a dial)"
        dump_diagnostics "$A" "unit A"; reproduced=1; break
    fi
    unroute "$A" "$PA" "$SRC_A"
    echo " ok"
done

if [[ $reproduced -eq 1 ]]; then
    _no "reproduced the wedge — diagnostics above; see OPEN-ITEMS #23"
else
    _ok "no wedge in $CYCLES cycles (absence of evidence: this hunts, it does not prove)"
fi

finish
