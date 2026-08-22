#!/usr/bin/env bash
# Tier 3 — calibrated endpoints hold a matched loudness across two units.
#
#   ./t3_loudness_match.sh <unit-a> <unit-b>
#
# The acoustic half of volume calibration needs a person with a meter. THIS half does not, and it is
# most of the machinery: that a curve saved on one unit is visible to another (records are written
# to the unit serving the GUI page but matching runs on whichever unit owns the GROUP), that the
# causal `rev` advances, and that moving one endpoint's slider re-derives the other through its own
# curve rather than copying the percentage.
#
# The curves are synthetic but physically shaped: both are ideal 20 dB/decade amplitude scalers, B
# six decibels less efficient than A. Six dB at 20 dB/decade is exactly a doubling of volume, so the
# expected answer is arithmetic and not a fudge factor — A at 30% must drive B to ~60%.
#
# Deliberately stores A's curve on A and B's curve on B, so the cross-unit merge is load-bearing:
# if it were broken, A's matcher would see one calibrated endpoint and do nothing.
source "$(dirname "$0")/lib.sh"
A="${1:?usage: t3_loudness_match.sh <unit-a> <unit-b>}"
B="${2:?need unit-b}"

echo "== Tier 3: loudness matching ($A + $B) =="

# POLL for the rig's identifiers rather than reading them once. Run straight after another suite,
# a unit is still unwinding that suite's teardown — players mid-detach, a source list momentarily
# unreadable — and a single read gets an empty string and aborts a test that would otherwise pass.
own_player() { ssh_json "$1" /api/mesh/snapshot 'json.dumps((d.get("local_player") or {}).get("player_id") or "")' | tr -d '"'; }
first_source() { ssh_json "$1" /api/mesh/snapshot 'd["sources"][0]["source_id"] if d["sources"] else ""'; }
PA="$(wait_for_nonempty 30 own_player "$A")"
PB="$(wait_for_nonempty 30 own_player "$B")"
[[ -n "$PA" && -n "$PB" ]] || { _no "both units need a local player (A=$PA B=$PB)"; finish; exit; }
SRC_A="$(wait_for_nonempty 30 first_source "$A")"
[[ -n "$SRC_A" ]] || { _no "unit A has no source to group on"; finish; exit; }
echo "  A=$PA  B=$PB  source=$SRC_A"

# Ideal 20 dB/decade curves; B is 6 dB less efficient, i.e. it needs twice the volume to match.
CURVE_A='[{"volume":35,"db":70.88},{"volume":85,"db":78.59}]'
CURVE_B='[{"volume":35,"db":64.88},{"volume":85,"db":72.59}]'

# This test must be safe to run on a rig somebody has really calibrated, and its own numbers must
# still be the ones in play while it runs. Two things follow.
#
# The test's writes claim a high `rev` so they outrank any real record for the same endpoint,
# wherever in the mesh it is stored — records are merged newest-rev-wins across units, so a real
# rev-2 curve would otherwise beat the test's rev-1 one and every arithmetic assertion below would
# be computed against the wrong speaker. Observed exactly that on the .7 pair.
#
# And any pre-existing record is SAVED and put back, rather than deleted. An earlier version simply
# DELETEd what it wrote, which destroys a real calibration whenever the user's record happens to
# live on the unit the test targets.
TEST_REV=1000
RESTORE_REV=2000

# Built with sys.argv rather than shell interpolation: nesting a rev into a single-quoted python -c
# inside a command substitution inside a function is exactly the quoting knot that breaks scripts.
RESTORE_PY='import json, sys
r = json.load(sys.stdin)
print(json.dumps({
    "name": r.get("name", ""), "url": r.get("url"), "enabled": r.get("enabled", True),
    "samples": r.get("samples", []),
    "maxLimit": r.get("maxLimit", {"mode": "percentage", "value": 100}),
    "trimDb": r.get("trimDb", 0), "knownRev": int(sys.argv[1]),
}))'

saved_record() {  # saved_record <unit> <player-id> -> the stored record as JSON, or {}
    ssh_json "$1" /api/audio/calibration "json.dumps((d.get(\"calibrations\") or {}).get(\"$2\") or {})"
}

restore_record() {  # restore_record <unit> <player-id> <json>
    local unit="$1" pid="$2" rec="$3" body
    if [[ -z "$rec" || "$rec" == "{}" || "$rec" == "null" ]]; then
        curl_ "$unit" DELETE "/api/audio/calibration/$pid" >/dev/null 2>&1
        return
    fi
    body="$(printf '%s' "$rec" | python3 -c "$RESTORE_PY" "$RESTORE_REV")"
    curl_ "$unit" PUT "/api/audio/calibration/$pid" "$body" >/dev/null 2>&1
}

ORIG_A="$(saved_record "$A" "$PA")"
ORIG_B="$(saved_record "$B" "$PB")"

# Registered FIRST so it runs LAST (defers unwind in reverse): let the rig settle before handing
# it to the next suite. Killing the feed drives the source to EOF, which detaches every player and
# releases them, and that churn takes a couple of aggregator polls to propagate. Without the pause,
# a suite that runs straight after this one sees players mid-detach and fails to resolve them —
# observed, and it is this test being a bad neighbour rather than anything being broken.
defer "sleep 12"

# Leave the rig as we found it: drop both curves, restore the policy, unroute B.
defer "restore_record \"$A\" \"$PA\" \"\$ORIG_A\""
defer "restore_record \"$B\" \"$PB\" \"\$ORIG_B\""
defer "curl_ \"$A\" PUT /api/audio/calibration/policy '{\"mode\":\"follow\",\"sets\":[]}' >/dev/null 2>&1; true"
defer "curl_ \"$A\" POST /api/mesh/unroute \"{\\\"player_id\\\":\\\"$PB\\\",\\\"source_id\\\":\\\"$SRC_A\\\"}\" >/dev/null 2>&1; true"
# Send B's speaker home rather than leaving it parked on A's server.
defer "curl_ \"$B\" POST /api/mesh/route \"{\\\"player_id\\\":\\\"$PB\\\",\\\"source_id\\\":\\\"airplay-1\\\"}\" >/dev/null 2>&1; true"

# -- curves are stored where they are written, and seen everywhere -------------------------------

sa="$(curl_ "$A" PUT "/api/audio/calibration/$PA" "{\"name\":\"A\",\"samples\":$CURVE_A,\"knownRev\":$TEST_REV}")"
assert_not_contains "$sa" '"error"' "A's curve accepted on A" "$sa"
assert_contains "$sa" '"calibrated":true' "A's curve fits"

sb="$(curl_ "$B" PUT "/api/audio/calibration/$PB" "{\"name\":\"B\",\"samples\":$CURVE_B,\"knownRev\":$TEST_REV}")"
assert_not_contains "$sb" '"error"' "B's curve accepted on B" "$sb"

# The causal counter, which is what makes ordering independent of a clock on a machine with no RTC.
# Arithmetic is precomputed: $(( )) nested inside a command substitution that is already quoting
# JSON is a parse error waiting to happen, and it was one.
EXPECT_REV=$((TEST_REV + 1))
BUMP_REV=$((TEST_REV + 40))
EXPECT_BUMP=$((TEST_REV + 41))
assert_contains "$sa" "\"rev\":$EXPECT_REV" "a save allocates the next rev above what the client knows"
resave="$(curl_ "$A" PUT "/api/audio/calibration/$PA" "{\"name\":\"A\",\"samples\":$CURVE_A,\"knownRev\":$BUMP_REV}")"
assert_contains "$resave" "\"rev\":$EXPECT_BUMP" "a client's high-water mark lifts the counter"

# The merged view: written on two different units, readable from either.
merged_of() { ssh_json "$1" /api/mesh/calibration "sorted(d.keys())==sorted([\"$PA\",\"$PB\"])"; }
assert_eq "$(wait_for "True" 12 merged_of "$A")" "True" "A sees both curves (cross-unit merge)"
assert_eq "$(wait_for "True" 12 merged_of "$B")" "True" "B sees both curves from the other side"
# The derived half has to travel too, or the GUI shows a calibrated endpoint as uncalibrated.
assert_contains "$(ssh_json "$A" /api/mesh/calibration)" '"calibrated": true' "the merged read carries the fit"

# -- group them and prove the match ----------------------------------------------------------------

curl_ "$A" PUT /api/audio/calibration/policy '{"mode":"stream","sets":[]}' >/dev/null

# Keep a sender on the source for the duration. This is not decoration: a source with no sender
# hits SOURCE_IDLE_TIMEOUT_S and `_go_idle` DETACHES every player — the true-none rule — so a group
# built on a silent source dissolves on its own every five minutes. An earlier version of this test
# grouped onto a silent source and passed or failed depending on where in that window it landed.
FIFO_A="/tmp/${SRC_A}-fifo"
# ~300 s of silence at 0.1 s per chunk. Generously longer than the test: when the feed runs out the
# source hits EOF, `_go_idle` detaches every player, and the remaining assertions read a volume for
# an endpoint that is no longer attached. That is the true-none rule working, not a fault — but it
# makes the test's length a hidden dependency, so leave real headroom.
feed_fifo_ "$A" "$FIFO_A" 3000 || true
defer "stop_feed_ \"$A\" \"$FIFO_A\""
active_of() { ssh_json "$A" /api/mesh/snapshot \
    "next((s[\"active\"] for s in d[\"sources\"] if s[\"source_id\"]==\"$SRC_A\"), None)"; }
assert_eq "$(wait_for "True" 10 active_of)" "True" "the source has a sender feeding it"

curl_ "$A" POST /api/mesh/route "{\"player_id\":\"$PA\",\"source_id\":\"$SRC_A\"}" >/dev/null
curl_ "$A" POST /api/mesh/route "{\"player_id\":\"$PB\",\"source_id\":\"$SRC_A\"}" >/dev/null

grouped_of() { ssh_json "$A" /api/mesh/snapshot \
    "next((sorted([\"$PA\",\"$PB\"])==sorted([p for p in s[\"player_ids\"] if p in [\"$PA\",\"$PB\"]]) for s in d[\"sources\"] if s[\"source_id\"]==\"$SRC_A\"), False)"; }
assert_eq "$(wait_for "True" 15 grouped_of)" "True" "both endpoints are on one source"

vol_of() { ssh_json "$A" /api/mesh/snapshot \
    "next((p[\"volume\"] for p in d[\"players\"] if p[\"player_id\"]==\"$1\"), None)"; }

# Let the reconciler adopt the current state as its baseline before we move anything. It must NOT
# drive a group on first sight — that would rearrange a user's house for no reason.
sleep 5
curl_ "$A" POST /api/mesh/volume "{\"player_id\":\"$PB\",\"volume\":25}" >/dev/null
sleep 5

# Now move A. B must follow through ITS OWN curve: 6 dB less efficient is exactly a doubling.
curl_ "$A" POST /api/mesh/volume "{\"player_id\":\"$PA\",\"volume\":30}" >/dev/null
got="$(wait_for "60" 20 vol_of "$PB")"
assert_eq "$got" "60" "B re-levels to 60% when A is set to 30% (6 dB less efficient)"
assert_eq "$(vol_of "$PA")" "30" "A, the endpoint the user moved, is left alone"

# And it tracks, rather than being a one-shot on join.
curl_ "$A" POST /api/mesh/volume "{\"player_id\":\"$PA\",\"volume\":45}" >/dev/null
assert_eq "$(wait_for "90" 20 vol_of "$PB")" "90" "B tracks A upward (45% -> 90%)"

# Turning matching off must stop it driving anything.
curl_ "$A" PUT /api/audio/calibration/policy '{"mode":"off","sets":[]}' >/dev/null
sleep 3
curl_ "$A" POST /api/mesh/volume "{\"player_id\":\"$PA\",\"volume\":20}" >/dev/null
sleep 6
assert_eq "$(vol_of "$PB")" "90" "with matching off, B is left where it was"

finish
