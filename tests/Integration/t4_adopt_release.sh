#!/usr/bin/env bash
# Tier 4 — adopt and release a FOREIGN Sendspin speaker.
#
#   ./t4_adopt_release.sh <plum-unit-host> [foreign-player-url]
#
# With no URL, auto-picks the first foreign player from the neighbourhood (e.g. a Home Assistant
# Voice PE). Adopts it onto the unit's first source, asserts it joined the group, then releases it
# and asserts BOTH that it left the group AND that the socket is gone — the four-step release that
# hardware proved necessary (a speaker that "released" but stayed ESTABLISHed is a regression).
#
# LIVE-DEVICE SAFETY: the target is someone's real speaker. This never sends audio to it; it only
# joins/leaves an idle source group, and the release runs even if an assertion fails (via defer).
source "$(dirname "$0")/lib.sh"
UNIT="${1:?usage: t4_adopt_release.sh <plum-unit-host> [foreign-player-url]}"
URL="${2:-}"

echo "== Tier 4: adopt / release a foreign speaker (unit=$UNIT) =="

# The source to adopt onto: the unit's first source.
SOURCE="$(ssh_json "$UNIT" /api/mesh/snapshot 'd["sources"][0]["source_id"] if d["sources"] else ""')"
[[ -n "$SOURCE" ]] || { _no "unit has no source to adopt onto"; finish; exit; }

# The foreign speaker: given, or the first non-own player on the segment.
if [[ -z "$URL" ]]; then
    URL="$(ssh_json "$UNIT" /api/mesh/neighbourhood \
        'next((p["url"] for p in d["players"] if not p["is_own"]), "")')"
fi
[[ -n "$URL" ]] || { printf '  \033[33mSKIP\033[0m no foreign speaker on the segment to adopt\n'; finish; exit; }
echo "  target: $URL  ->  source $SOURCE"

# Always hand the speaker back, whatever happens below.
PLAYER=""  # filled in after adopt; release keys on the client_id the server learned
defer '[[ -n "$PLAYER" ]] && { echo "  [teardown] releasing $PLAYER"; curl_ "$UNIT" POST /api/mesh/release "{\"player_id\":\"$PLAYER\",\"source_id\":\"$SOURCE\",\"url\":\"$URL\"}" >/dev/null; }'

# -- adopt --------------------------------------------------------------------------------------
adopt="$(curl_ "$UNIT" POST /api/mesh/adopt "{\"url\":\"$URL\",\"source_id\":\"$SOURCE\"}")"
assert_contains "$adopt" '"ok": true' "adopt returned ok"

# The speaker should now be a player in the source's group. Learn its client_id from the snapshot.
PLAYER="$(wait_for_nonempty 10 ssh_json "$UNIT" /api/mesh/snapshot \
    "next((pid for s in d[\"sources\"] if s[\"source_id\"]==\"$SOURCE\" for pid in s[\"player_ids\"]), \"\")")"
[[ -n "$PLAYER" ]] && _ok "foreign speaker joined the group as '$PLAYER'" \
    || _no "foreign speaker never appeared in the source group"

# -- release ------------------------------------------------------------------------------------
rel="$(curl_ "$UNIT" POST /api/mesh/release "{\"player_id\":\"$PLAYER\",\"source_id\":\"$SOURCE\",\"url\":\"$URL\"}")"
assert_contains "$rel" '"ok": true' "release returned ok"

# It must leave the group... (print a bool; ssh_json's python -c is single-quoted, so no inner quotes)
gone="$(wait_for "False" 8 ssh_json "$UNIT" /api/mesh/snapshot \
    "\"$PLAYER\" in next((s[\"player_ids\"] for s in d[\"sources\"] if s[\"source_id\"]==\"$SOURCE\"), [])")"
assert_eq "$gone" "False" "speaker left the source group"

# ...AND the connection must actually close (the four-step hang-up; the reason this test exists).
host="$(printf '%s' "$URL" | sed -E 's#^ws://([^:/]+).*#\1#')"
sock="$(ssh_ "$UNIT" "ss -tn 2>/dev/null | grep -c '$host' || true")"
assert_eq "${sock:-0}" "0" "socket to the foreign speaker is closed (no lingering ESTABLISHED)"
PLAYER=""  # already released cleanly; suppress the redundant teardown release

finish
