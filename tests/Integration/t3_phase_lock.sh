#!/usr/bin/env bash
# Tier 3 — multi-room phase lock: several units on ONE source, each reporting its own timing error.
#
#   ./t3_phase_lock.sh <lead-unit> <other-unit>...
#
# The renderer schedules every chunk against `compute_play_time()` and the DAC deadline
# (docs/ARCHITECTURE.md §4). That is unit-tested headless in tests/Unit/test_render_sync.py; what
# only a rig can answer is whether PortAudio reports a usable `outputBufferDacTime` on each unit's
# actual card, and how far apart real units end up.
#
# This measures it WITHOUT a microphone. Each player publishes its own error in the mesh self-report
# (`local_player.sync`), so the check is: every unit locked, every unit inside the deadband, and no
# unit stepping after it aligned. A unit that reports `locked:false` while audio flows is
# free-running — read its log for the "no usable DAC time" warning, and nothing else here applies to
# it.
#
# Silence is a perfectly good test signal: the lock is a function of timestamps, not of content.
source "$(dirname "$0")/lib.sh"
LEAD="${1:?usage: t3_phase_lock.sh <lead-unit> <other-unit>...}"
shift
OTHERS=("$@")
[[ ${#OTHERS[@]} -ge 1 ]] || { echo "need at least two units"; exit 2; }
UNITS=("$LEAD" "${OTHERS[@]}")

# How far off the deadline a unit may sit and still pass. The renderer's deadband is 1.5 ms and the
# trim holds it there, so 5 ms is generous — it is a "the lock is working" gate, not the deadband.
TOLERANCE_MS="${PLUM_SYNC_TOLERANCE_MS:-5}"
FEED_CHUNKS=900  # 0.1 s per chunk — 90 s: join, settle, then a measured window with no churn

echo "== Tier 3: multi-room phase lock (lead=$LEAD, +${#OTHERS[@]} units) =="

first_source() { ssh_json "$LEAD" /api/mesh/snapshot 'd["sources"][0]["source_id"] if d["sources"] else ""'; }
SRC="$(wait_for_nonempty 30 first_source)"
[[ -n "$SRC" ]] || { _no "lead unit has no source"; finish; exit; }
FIFO="/tmp/${SRC}-fifo"
echo "  source=$SRC fifo=$FIFO tolerance=${TOLERANCE_MS}ms"

# A player idle at rest is attached to nothing, so it is in no unit's `players` list and appears only
# in its own unit's `local_player` self-report — resolve it the way mesh.router does.
player_of() {  # player_of <unit-host>
    ssh_json "$LEAD" /api/mesh/view \
        "next((p[\"player_id\"] for u in d[\"units\"] if u[\"host\"]==\"$1\" for p in u[\"players\"]), \"\") or next(((u.get(\"local_player\") or {}).get(\"player_id\") or \"\" for u in d[\"units\"] if u[\"host\"]==\"$1\"), \"\")"
}
home_source_of() { ssh_json "$1" /api/mesh/snapshot 'd["sources"][0]["source_id"] if d["sources"] else ""'; }

# Its own unit reports a player's sync, never a peer: it is the player process that measures it.
sync_of() {  # sync_of <unit-host> <field>
    ssh_json "$1" /api/mesh/snapshot "((d.get(\"local_player\") or {}).get(\"sync\") or {}).get(\"$2\")"
}
locked_of() { sync_of "$1" locked; }

# Indexed arrays, not associative ones: the workstation is a Mac and macOS ships bash 3.2, which
# has no `declare -A`. It does not fail loudly either — it treats the host string as an arithmetic
# index, stores every unit under slot 0, and the test then routes one player and reports the other
# three as broken. Measured 2026-09-13.
PLAYERS=()
HOMES=()
for host in "${UNITS[@]}"; do
    p="$(player_of "$host")"
    [[ -n "$p" ]] || { _no "could not resolve a player for $host"; finish; exit; }
    PLAYERS+=("$p")
    HOMES+=("$(home_source_of "$host")")
done

# Always put every player back where it was, however this ends. A unit with no source of its own has
# nowhere to go home to, and `unroute` is not enough for it: that drops the player from the group but
# leaves the dial up, so the speaker sits attached to the lead unit in no group. `release` drops the
# group AND hangs up, which is the state a unit boots into — released, idle, claimable. Measured
# 2026-09-13: after an unroute, two speakers stayed attached to the lead with `group=None`.
for i in "${!UNITS[@]}"; do
    if [[ -n "${HOMES[$i]}" ]]; then
        defer "curl_ \"${UNITS[$i]}\" POST /api/mesh/route \"{\\\"player_id\\\":\\\"${PLAYERS[$i]}\\\",\\\"source_id\\\":\\\"${HOMES[$i]}\\\"}\" >/dev/null 2>&1; true"
    else
        defer "curl_ \"$LEAD\" POST /api/mesh/release \"{\\\"player_id\\\":\\\"${PLAYERS[$i]}\\\",\\\"source_id\\\":\\\"$SRC\\\"}\" >/dev/null 2>&1; true"
    fi
done
defer "stop_feed_ \"$LEAD\" \"$FIFO\""

# -- one source, every player ----------------------------------------------------------------------
feed_fifo_ "$LEAD" "$FIFO" "$FEED_CHUNKS" || true
for i in "${!UNITS[@]}"; do
    curl_ "$LEAD" POST /api/mesh/route "{\"player_id\":\"${PLAYERS[$i]}\",\"source_id\":\"$SRC\"}" >/dev/null
done

for host in "${UNITS[@]}"; do
    got="$(wait_for "True" 25 locked_of "$host")"
    assert_eq "$got" "True" "$host is scheduling against the deadline (locked)"
done

# Settle before measuring. Every join restarts the stream (SourceFeeder.membership_change), so the
# units that were already playing legitimately step while the others arrive — that transient is the
# routing working, not the lock failing.
echo "  settling..."
sleep 15

BASE_STEPS=()
for host in "${UNITS[@]}"; do
    BASE_STEPS+=("$(sync_of "$host" steps)")
done

# -- the steady state ------------------------------------------------------------------------------
# Measured over a window with no membership change in it, which is the only condition under which a
# step means something is wrong.
sleep 20

for i in "${!UNITS[@]}"; do
    host="${UNITS[$i]}"
    avg="$(sync_of "$host" sync_avg_ms)"
    steps="$(sync_of "$host" steps)"
    inside="$(PLUM_AVG="$avg" PLUM_TOL="$TOLERANCE_MS" python3 -c '
import os
avg = os.environ["PLUM_AVG"]
print("True" if avg not in ("", "None") and abs(float(avg)) <= float(os.environ["PLUM_TOL"]) else "False")')"
    assert_eq "$inside" "True" "$host is within ${TOLERANCE_MS}ms of its deadline (avg=${avg}ms)"
    assert_eq "$steps" "${BASE_STEPS[$i]}" "$host did not step during a settled 20 s window"
done

finish
