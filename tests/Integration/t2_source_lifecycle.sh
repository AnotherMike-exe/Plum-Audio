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

[[ -z "$SOURCE" ]] && SOURCE="$(ssh_json "$UNIT" /api/mesh/snapshot 'd["sources"][0]["source_id"] if d["sources"] else ""')"
[[ -n "$SOURCE" ]] || { _no "unit has no source"; finish; exit; }
FIFO="/tmp/${SOURCE}-fifo"
echo "  source=$SOURCE fifo=$FIFO"

active_of() { ssh_json "$UNIT" /api/mesh/snapshot \
    "next((s[\"active\"] for s in d[\"sources\"] if s[\"source_id\"]==\"$SOURCE\"), None)"; }

# Baseline: idle.
assert_eq "$(active_of)" "False" "source starts idle (active=False)"

# Feed ~4s of silence into the FIFO in the background; feeder should flip to active.
ssh_ "$UNIT" "setsid sh -c 'dd if=/dev/zero of=$FIFO bs=17640 count=40 2>/dev/null' </dev/null >/dev/null 2>&1 &" || true
defer "ssh_ \"$UNIT\" \"pkill -f 'dd if=/dev/zero of=$FIFO' 2>/dev/null; true\""

got="$(wait_for "True" 8 active_of)"
assert_eq "$got" "True" "source goes active while a sender feeds it"

# Also reflected as streaming (has_active_stream / playback_state=playing).
streaming="$(ssh_json "$UNIT" /api/mesh/snapshot "next((s[\"streaming\"] for s in d[\"sources\"] if s[\"source_id\"]==\"$SOURCE\"), None)")"
assert_eq "$streaming" "True" "source reports streaming while active"

# After the writer closes (EOF), it must return to idle — the group.stop() path.
back="$(wait_for "False" 15 active_of)"
assert_eq "$back" "False" "source returns to idle on EOF (announced stopped)"

finish
