#!/usr/bin/env bash
# Tier 4 — third-party interop with Music Assistant. Read-only; sends no commands.
#
#   ./t4_interop_ma.sh <plum-unit-host>
#
# REQUIRES: the unit is on the SAME L2 segment as a Music Assistant Sendspin server (mDNS is
# link-local). Discovers MA via the neighbourhood API rather than a hardcoded address, so it works
# on any segment. Confirms: MA is discovered, MA can discover us, and — if MA is currently playing
# to our speaker — that our self-report names MA as the captor. It does NOT require MA to be
# playing; the play-dependent checks are reported as SKIP when idle.
source "$(dirname "$0")/lib.sh"
UNIT="${1:?usage: t4_interop_ma.sh <plum-unit-host>}"

echo "== Tier 4: Music Assistant interop (unit=$UNIT) =="

nb="$(curl_ "$UNIT" GET /api/mesh/neighbourhood)"

# We must see at least one FOREIGN Sendspin server on the segment (MA advertises _sendspin-server).
foreign_server="$(printf '%s' "$nb" | ssh_ "$UNIT" "python3 -c '
import json,sys
d=json.load(sys.stdin)
ma=[s for s in d[\"servers\"] if not s[\"is_own\"]]
print(ma[0][\"friendly_name\"] if ma else \"\")'")"
if [[ -z "$foreign_server" ]]; then
    _no "a foreign Sendspin server is visible on the segment" \
        "no non-own _sendspin-server._tcp found — is MA on this VLAN?"
    finish; exit
fi
_ok "foreign Sendspin server discovered: $foreign_server"

# Our own server must be advertising (so MA could discover us in return).
own_server="$(printf '%s' "$nb" | ssh_ "$UNIT" "python3 -c '
import json,sys; d=json.load(sys.stdin)
print(next((s[\"friendly_name\"] for s in d[\"servers\"] if s[\"is_own\"]), \"\"))'")"
[[ -n "$own_server" ]] && _ok "our server advertises as '$own_server'" || _no "our server is not advertising"

# Our own player must be advertising _sendspin._tcp (this is what lets MA dial us).
own_player="$(printf '%s' "$nb" | ssh_ "$UNIT" "python3 -c '
import json,sys; d=json.load(sys.stdin)
print(next((p[\"friendly_name\"] for p in d[\"players\"] if p[\"is_own\"]), \"\"))'")"
[[ -n "$own_player" ]] && _ok "our player advertises as '$own_player'" \
    || _no "our player is not advertising _sendspin._tcp"

# If MA is currently playing to our speaker, the self-report should name it (the same code path a
# peer-claim uses). When idle, this is informational, not a failure.
lp="$(ssh_json "$UNIT" /api/mesh/snapshot 'json.dumps(d.get("local_player") or {})')"
server_name="$(printf '%s' "$lp" | ssh_ "$UNIT" "python3 -c 'import json,sys; print((json.load(sys.stdin) or {}).get(\"server_name\") or \"\")'")"
state="$(printf '%s' "$lp" | ssh_ "$UNIT" "python3 -c 'import json,sys; print((json.load(sys.stdin) or {}).get(\"playback_state\") or \"\")'")"
if [[ -n "$server_name" && "$server_name" != "$own_server" ]]; then
    _ok "our speaker is claimed by a foreign server and self-reports it: '$server_name' ($state)"
else
    printf '  \033[33mSKIP\033[0m our-speaker-claimed-by-MA (start MA playback to this unit to exercise)\n'
fi

finish
