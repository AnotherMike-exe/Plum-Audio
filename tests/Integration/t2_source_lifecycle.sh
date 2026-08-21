#!/usr/bin/env bash
# Tier 2 — a source announces active/idle correctly as a sender starts and stops.
#
#   ./t2_source_lifecycle.sh <unit-host> [source-id]
#
# The idle contract (docs/SPEC-CONFORMANCE.md): a source is `active` + `streaming` only while a
# sender feeds its FIFO, and returns to idle (playback_state=stopped) on EOF. We drive it by feeding
# SILENCE into the source FIFO directly — no real sender needed — then stopping. Feeder idle logic
# can't be unit-tested (needs a real FIFO + group), so this is its home.
source "$(dirname "$0")/lib.sh"
UNIT="${1:?usage: t2_source_lifecycle.sh <unit-host> [source-id]}"
SOURCE="${2:-}"

echo "== Tier 2: source active/idle lifecycle (unit=$UNIT) =="

# POLLED, not read once. Run after another suite, a unit can still be unwinding that suite's
# teardown and answer a snapshot request slowly enough to come back empty — which aborted this test
# with "unit has no source" against a unit that has five.
first_source() { ssh_json "$UNIT" /api/mesh/snapshot 'd["sources"][0]["source_id"] if d["sources"] else ""'; }
[[ -z "$SOURCE" ]] && SOURCE="$(wait_for_nonempty 30 first_source)"
[[ -n "$SOURCE" ]] || { _no "unit has no source"; finish; exit; }
FIFO="/tmp/${SOURCE}-fifo"
echo "  source=$SOURCE fifo=$FIFO"

active_of() { ssh_json "$UNIT" /api/mesh/snapshot \
    "next((s[\"active\"] for s in d[\"sources\"] if s[\"source_id\"]==\"$SOURCE\"), None)"; }

# Baseline: idle.
assert_eq "$(active_of)" "False" "source starts idle (active=False)"

# Feed ~4s of silence into the FIFO in the background; feeder should flip to active.
# Inside the CONTAINER — the FIFO is not on the host's filesystem. See dexec_/feed_fifo_ in lib.sh.
feed_fifo_ "$UNIT" "$FIFO" 40 || true
defer "stop_feed_ \"$UNIT\" \"$FIFO\""

got="$(wait_for "True" 8 active_of)"
assert_eq "$got" "True" "source goes active while a sender feeds it"

# Also reflected as streaming (has_active_stream / playback_state=playing).
#
# POLLED, not read once: `streaming` legitimately lags `active`. Since the true-none rule a player is
# DETACHED while its source is idle, so the order on a feed is source-goes-active -> localActivity
# re-attaches the player -> the group acquires a stream. A single immediate read here raced that and
# reported a hard FAIL on a unit that was about to stream perfectly well.
streaming_of() { ssh_json "$UNIT" /api/mesh/snapshot \
    "next((s[\"streaming\"] for s in d[\"sources\"] if s[\"source_id\"]==\"$SOURCE\"), None)"; }
streaming="$(wait_for "True" 8 streaming_of)"
assert_eq "$streaming" "True" "source reports streaming while active"

# After the writer closes (EOF), it must return to idle — the group.stop() path.
back="$(wait_for "False" 15 active_of)"
assert_eq "$back" "False" "source returns to idle on EOF (announced stopped)"

finish
