#!/usr/bin/env bash
# Tier 2 — the calibration tone reaches the audio pipeline, and puts the endpoint back.
#
#   ./t2_calibration_tone.sh <unit-host>
#
# This is the STRUCTURAL half of volume calibration, and it needs no dB meter and no speaker: it
# proves the tone becomes a real source, that audio actually flows into it, that the endpoint is
# routed onto it and, crucially, that stopping restores where the endpoint was.
#
# It exists because the predecessor's version of this feature failed in exactly this layer and was
# never caught: its tone went straight to local ALSA, so it never touched the audio path or the
# endpoint's gain stage at all. `streaming=True` on a `cal:` source is the assertion that would have
# caught it — audio is being committed to a group, not written to a card behind the pipeline's back.
#
# What it deliberately does NOT prove is the measurement itself. That the endpoint's VOLUME changes
# the tone's SPL is the one thing only a person with a meter can confirm, and it is item 2 of the
# rig checklist in docs/VOLUME-CALIBRATION.md.
source "$(dirname "$0")/lib.sh"
UNIT="${1:?usage: t2_calibration_tone.sh <unit-host>}"

echo "== Tier 2: calibration tone lifecycle (unit=$UNIT) =="

# The unit's OWN speaker, from its self-report — the only place an unattached player appears.
PLAYER="$(ssh_json "$UNIT" /api/mesh/snapshot 'json.dumps((d.get("local_player") or {}).get("player_id") or "")' | tr -d '"')"
[[ -n "$PLAYER" ]] || { _no "unit reports no local player (has_player=false, or the player has not reported yet)"; finish; exit; }
echo "  player=$PLAYER"

CAL_SOURCE="cal:${PLAYER}"

# Where it is now, so we can assert it goes back. Empty is a legitimate answer (idle).
#
# Scans the whole MESH VIEW, not this unit's snapshot. A unit's snapshot lists only the sources IT
# ingests, so a speaker that has roamed to a peer — which is exactly what happens when this unit
# follows another — appears nowhere in it and reads as "idle". The tone's own restore logic looks
# across the mesh, so a local-only check here asserted the wrong thing and failed against a tone
# that had put the speaker back correctly.
before_source() { ssh_json "$UNIT" /api/mesh/view \
    "next((s[\"source_id\"] for u in d[\"units\"] for s in u[\"sources\"] if \"$PLAYER\" in s[\"player_ids\"] and not s[\"source_id\"].startswith(\"cal:\")), \"\")"; }
BEFORE="$(before_source)"
echo "  before: source=${BEFORE:-(idle)}"

# Always stop the tone, however this exits — an abandoned tone is a speaker making noise.
defer "curl_ \"$UNIT\" POST /api/mesh/calibration/tone/stop >/dev/null 2>&1; true"

# -- start ---------------------------------------------------------------------------------------

playing_of() { ssh_json "$UNIT" /api/mesh/calibration/tone 'd["playing"]'; }
assert_eq "$(playing_of)" "False" "no tone playing to begin with"

start="$(curl_ "$UNIT" POST /api/mesh/calibration/tone \
    "{\"player_id\":\"$PLAYER\",\"volume\":35,\"seconds\":25}")"
assert_not_contains "$start" '"error"' "tone start accepted" "$start"
assert_contains "$start" '"playing": true' "tone reports playing"

assert_eq "$(playing_of)" "True" "tone status is playing"

# -- the tone is a REAL source, and audio is flowing into it --------------------------------------

cal_active_of() { ssh_json "$UNIT" /api/mesh/snapshot \
    "next((s[\"active\"] for s in d[\"sources\"] if s[\"source_id\"]==\"$CAL_SOURCE\"), None)"; }
cal_streaming_of() { ssh_json "$UNIT" /api/mesh/snapshot \
    "next((s[\"streaming\"] for s in d[\"sources\"] if s[\"source_id\"]==\"$CAL_SOURCE\"), None)"; }

assert_eq "$(wait_for "True" 8 cal_active_of)" "True" "a cal: source exists and a writer is feeding it"
# THE assertion. Audio is being committed to a group — i.e. it travels encode -> websocket -> the
# player's gain stage, which is the quantity being measured. The predecessor's tone could never
# have made this true.
assert_eq "$(wait_for "True" 8 cal_streaming_of)" "True" "the tone is streaming through the audio path"

cal_members_of() { ssh_json "$UNIT" /api/mesh/snapshot \
    "next((\"$PLAYER\" in s[\"player_ids\"] for s in d[\"sources\"] if s[\"source_id\"]==\"$CAL_SOURCE\"), None)"; }
assert_eq "$(wait_for "True" 8 cal_members_of)" "True" "the endpoint is routed onto the tone, alone"

# The endpoint's level was commanded to the requested value.
vol_of() { ssh_json "$UNIT" /api/mesh/snapshot \
    "next((p[\"volume\"] for p in d[\"players\"] if p[\"player_id\"]==\"$PLAYER\"), None)"; }
assert_eq "$(wait_for "35" 8 vol_of)" "35" "the endpoint is at the requested calibration level"

# -- re-level without a restart ------------------------------------------------------------------

relevel="$(curl_ "$UNIT" POST /api/mesh/calibration/tone/volume '{"volume":70}')"
assert_not_contains "$relevel" '"error"' "re-level accepted" "$relevel"
assert_eq "$(wait_for "70" 8 vol_of)" "70" "the endpoint follows a re-level"
# Re-levelling must not tear the source down and build it again: that gaps the noise while the
# meter is still integrating, which is exactly when a reading goes wrong.
assert_eq "$(cal_streaming_of)" "True" "the tone kept streaming across a re-level"

# -- stop restores ---------------------------------------------------------------------------------

stop="$(curl_ "$UNIT" POST /api/mesh/calibration/tone/stop)"
assert_not_contains "$stop" '"error"' "tone stop accepted" "$stop"
assert_eq "$(playing_of)" "False" "tone reports stopped"

cal_gone_of() { ssh_json "$UNIT" /api/mesh/snapshot \
    "any(s[\"source_id\"]==\"$CAL_SOURCE\" for s in d[\"sources\"])"; }
# The tone source is always created on THIS unit, so the snapshot is the right scope for it —
# unlike the endpoint above, which may live anywhere in the mesh.
assert_eq "$(wait_for "False" 8 cal_gone_of)" "False" "the cal: source is torn down"

after="$(wait_for "$BEFORE" 10 before_source)"
assert_eq "$after" "$BEFORE" "the endpoint is back where it started (${BEFORE:-idle})"

# -- it expires on its own -------------------------------------------------------------------------
#
# A browser that navigates away cannot press Stop, so the session has to end itself. Worth a real
# assertion: without it, an abandoned wizard leaves a speaker playing noise into an empty house.

curl_ "$UNIT" POST /api/mesh/calibration/tone \
    "{\"player_id\":\"$PLAYER\",\"volume\":20,\"seconds\":5}" >/dev/null
assert_eq "$(playing_of)" "True" "a short tone starts"
assert_eq "$(wait_for "False" 20 playing_of)" "False" "the tone expires on its own"
assert_eq "$(wait_for "False" 10 cal_gone_of)" "False" "an expired tone also tears its source down"
assert_eq "$(wait_for "$BEFORE" 10 before_source)" "$BEFORE" "an expired tone also restores the endpoint"

finish
