#!/usr/bin/env bash
# Tier 2 — live endpoint CRUD applies without a restart, and leaves other endpoints untouched.
#
#   ./t2_endpoint_crud.sh <unit-host> [spotify|airplay]
#
# Adds an endpoint via the config API, asserts the manager brings a matching source up within a few
# seconds, renames it (asserts the source label follows), then removes it (asserts the source goes
# away). Any endpoints that existed before are asserted still present at the end. Config API on 5002.
source "$(dirname "$0")/lib.sh"
UNIT="${1:?usage: t2_endpoint_crud.sh <unit-host> [spotify|airplay]}"
SRC="${2:-spotify}"

echo "== Tier 2: live endpoint CRUD ($SRC on $UNIT) =="
API="/api/integrations/$SRC"

count_sources() { ssh_json "$UNIT" /api/mesh/snapshot "len([s for s in d[\"sources\"] if s[\"source_id\"].startswith(\"$SRC-\")])"; }
before_count="$(count_sources)"
existing_ids="$(curl_ "$UNIT" GET "$API/endpoints" | ssh_ "$UNIT" "python3 -c 'import json,sys; print(sorted(e[\"id\"] for e in json.load(sys.stdin)[\"endpoints\"]))'")"
echo "  before: $before_count live source(s); endpoint ids=$existing_ids"

# -- add --------------------------------------------------------------------------------------
add="$(curl_ "$UNIT" POST "$API/endpoints" '{"deviceName":"CRUD Probe","enabled":true}')"
ID="$(printf '%s' "$add" | ssh_ "$UNIT" "python3 -c 'import json,sys; print(json.load(sys.stdin)[\"endpoint\"][\"id\"])'")"
[[ -n "$ID" ]] || { _no "add did not return an endpoint id"; finish; exit; }
defer "curl_ \"$UNIT\" DELETE \"$API/endpoints/$ID\" >/dev/null 2>&1; true"   # always clean up the probe
_ok "added endpoint id=$ID"

up="$(wait_for "True" 8 ssh_json "$UNIT" /api/mesh/snapshot \
    "any(s[\"source_id\"]==\"$SRC-$ID\" for s in d[\"sources\"])")"
assert_eq "$up" "True" "manager brought up source $SRC-$ID live"

# -- rename (label must follow on the live source) --------------------------------------------
curl_ "$UNIT" PUT "$API/endpoints/$ID" '{"deviceName":"CRUD Renamed"}' >/dev/null
named="$(wait_for "CRUD Renamed" 8 ssh_json "$UNIT" /api/mesh/snapshot \
    "next((s[\"name\"] for s in d[\"sources\"] if s[\"source_id\"]==\"$SRC-$ID\"), \"\")")"
assert_eq "$named" "CRUD Renamed" "rename applied to the live source label"

# -- remove -----------------------------------------------------------------------------------
curl_ "$UNIT" DELETE "$API/endpoints/$ID" >/dev/null
gone="$(wait_for "False" 8 ssh_json "$UNIT" /api/mesh/snapshot \
    "any(s[\"source_id\"]==\"$SRC-$ID\" for s in d[\"sources\"])")"
assert_eq "$gone" "False" "remove tore the source down live"

# -- the pre-existing endpoints are all still there -------------------------------------------
after_ids="$(curl_ "$UNIT" GET "$API/endpoints" | ssh_ "$UNIT" "python3 -c 'import json,sys; print(sorted(e[\"id\"] for e in json.load(sys.stdin)[\"endpoints\"]))'")"
assert_eq "$after_ids" "$existing_ids" "pre-existing endpoints untouched by the CRUD cycle"

finish
