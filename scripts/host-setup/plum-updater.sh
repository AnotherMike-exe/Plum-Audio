#!/usr/bin/env bash
# Plum-Audio update agent — the HOST's half of the pair.
#
# The container cannot replace itself: the process that would pull a new image runs inside the thing
# being replaced. This script is what actually runs `docker compose pull` and `up -d`, and it lives
# on the host precisely so no Docker socket has to be mounted into a container whose APIs are
# unauthenticated on 0.0.0.0. The container's half is backend/scripts/updater.py.
#
#   container writes  $CONFIG/update.request     {channel, checkOnly, requestedAt}
#   this script reads it, acts, and writes
#                     $CONFIG/update.state       {phase, digest, available, lastUpdate, ...}
#
# Installed by scripts/host-setup/provision.sh and scripts/plum-init.sh, driven by a systemd .path
# unit watching the request file. It also runs on a timer for the periodic CHECK, which never
# installs anything.
#
#   plum-updater.sh watch      consume a request, if one is there  (the .path unit calls this)
#   plum-updater.sh check      refresh "what is available", install nothing  (the .timer calls this)
#   plum-updater.sh update     pull and recreate now  (what a human runs to test it by hand)
#   plum-updater.sh state      print the state file
#
# WHY THE ORDER IS WHAT IT IS. `pull` runs first and `up -d` runs ONLY if it succeeded. A failed
# pull — no internet on an isolated AV VLAN is the normal case, not the exotic one — must leave the
# unit playing exactly what it was playing. The reverse order stops a working container to find out
# whether a replacement exists.
set -uo pipefail

AGENT_VERSION="1.0.0"

ROOT="${PLUM_ROOT:-/opt/plum-audio}"
CONFIG="${PLUM_CONFIG:-${ROOT}/config}"
REQUEST="${CONFIG}/update.request"
STATE="${CONFIG}/update.state"
LOCK="${ROOT}/.update.lock"
ENV_FILE="${ROOT}/.env"

DEFAULT_REGISTRY="ghcr.io/anothermike-exe/plum-audio"

# ISO-8601 timestamp. `date -Is` is GNU-only and a BSD date (a macOS workstation) rejects it, so
# fall back rather than writing an empty string into the state file the GUI renders. The units are
# Debian and always take the first branch.
now_iso() { date -Is 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%S+00:00; }

log() { printf '%s plum-updater: %s\n' "$(now_iso)" "$*"; }

# --- state ----------------------------------------------------------------------------------------
# One JSON object, written atomically, read by the container through the bind mount. Atomic because
# the container polls this file straight through the window where it is being recreated, so a reader
# landing mid-write is a real case and not a hypothetical.
#
# Every field is written on every save. Passing the previous value through is what lets `phase` move
# while `available` and `lastUpdate` stay put, so the GUI can show "pulling" without losing the
# result of the last run.
S_PHASE="idle"
S_CHANNEL=""
S_IMAGE=""
S_DIGEST=""
S_AVAILABLE=""
S_LAST_CHECK=""
S_LAST_UPDATE=""
S_PREVIOUS=""

load_state() {
    [[ -f "$STATE" ]] || return 0
    # Read with python3 rather than jq: jq is not on a Pi OS Lite image and python3 always is.
    local dump
    dump="$(python3 - "$STATE" <<'PY' 2>/dev/null || true
import json, shlex, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        d = json.load(fh)
except Exception:
    sys.exit(0)
if not isinstance(d, dict):
    sys.exit(0)
def emit(var, key):
    v = d.get(key)
    if isinstance(v, (dict, list)):
        v = json.dumps(v)
    print(f"{var}={shlex.quote('' if v is None else str(v))}")
emit("S_CHANNEL", "channel")
emit("S_IMAGE", "image")
emit("S_DIGEST", "digest")
emit("S_AVAILABLE", "available")
emit("S_LAST_CHECK", "lastCheck")
emit("S_LAST_UPDATE", "lastUpdate")
emit("S_PREVIOUS", "previousDigest")
PY
)"
    [[ -n "$dump" ]] && eval "$dump"
    return 0
}

save_state() {
    mkdir -p "$CONFIG" 2>/dev/null || true
    local tmp="${STATE}.$$.tmp"
    # Values go in as ARGV, never interpolated into the script body. lastUpdate is itself a JSON
    # document built by record_result, so interpolating it would put quotes and backslashes straight
    # into Python source — a message containing an apostrophe would be a syntax error, and the state
    # file would silently stop updating mid-run.
    python3 - "$tmp" "$AGENT_VERSION" "$(now_iso)" "$S_PHASE" "$S_CHANNEL" "$S_IMAGE" \
        "$S_DIGEST" "$S_AVAILABLE" "$S_LAST_CHECK" "$S_PREVIOUS" "$S_LAST_UPDATE" <<'PY' || return 1
import json, sys
(out, agent, written, phase, channel, image,
 digest, available, last_check, previous, last_update) = sys.argv[1:12]
payload = {
    "agentVersion": agent,
    "writtenAt": written,
    "phase": phase,
    "channel": channel or None,
    "image": image or None,
    "digest": digest or None,
    "available": available or None,
    "lastCheck": last_check or None,
    "previousDigest": previous or None,
}
try:
    payload["lastUpdate"] = json.loads(last_update) if last_update.strip() else None
except ValueError:
    payload["lastUpdate"] = None
with open(out, "w", encoding="utf-8") as fh:
    json.dump(payload, fh, indent=2)
    fh.write("\n")
PY
    mv -f "$tmp" "$STATE" || return 1
    # The container runs as a non-root user and only READS this, but /config is its own mount and a
    # root-owned 0600 file there is invisible to it. World-readable is correct: it holds a version
    # and a digest, nothing secret.
    chmod 0644 "$STATE" 2>/dev/null || true
    return 0
}

phase() { S_PHASE="$1"; save_state; log "phase=$1"; }

record_result() {
    # $1 ok|failed  $2 message
    S_LAST_UPDATE="$(python3 -c '
import json, sys
print(json.dumps({"result": sys.argv[1], "message": sys.argv[2], "at": sys.argv[3],
                  "fromDigest": sys.argv[4] or None, "toDigest": sys.argv[5] or None}))
' "$1" "$2" "$(now_iso)" "$S_PREVIOUS" "$S_DIGEST")"
}

# --- image reference ------------------------------------------------------------------------------
# PLUM_IMAGE/PLUM_TAG in $ROOT/.env are what compose interpolates, so they are the truth about what
# this unit runs — deploy.sh and plum-init.sh both write them. A channel only ever changes the TAG:
# the registry path stays whatever provisioned the unit, so an unauthenticated request cannot point
# a unit at an arbitrary image.
image_name() {
    local name=""
    [[ -f "$ENV_FILE" ]] && name="$(grep -E '^PLUM_IMAGE=' "$ENV_FILE" | tail -1 | cut -d= -f2-)"
    [[ -n "$name" && "$name" != "plum-audio" ]] || name="$DEFAULT_REGISTRY"
    printf '%s' "$name"
}

current_tag() {
    local tag=""
    [[ -f "$ENV_FILE" ]] && tag="$(grep -E '^PLUM_TAG=' "$ENV_FILE" | tail -1 | cut -d= -f2-)"
    printf '%s' "${tag:-latest}"
}

set_tag() {
    # Rewrite PLUM_TAG so a later plain `docker compose up -d` in that directory keeps using the
    # channel the operator chose. Without this the change lasts exactly one run.
    #
    # Filter to a temp file and move, rather than `sed -i`: BSD sed needs an argument after -i and
    # GNU sed refuses one, so an in-place edit is not portable — and a silent failure here means the
    # tag never moves and the unit quietly reinstalls the image it already had. The move is also
    # atomic, which matters because compose may read this file at any moment.
    local tag="$1" tmp
    [[ -f "$ENV_FILE" ]] || return 1
    tmp="${ENV_FILE}.$$.tmp"
    if grep -qE '^PLUM_TAG=' "$ENV_FILE"; then
        grep -vE '^PLUM_TAG=' "$ENV_FILE" > "$tmp" || { rm -f "$tmp"; return 1; }
    else
        cat "$ENV_FILE" > "$tmp" || { rm -f "$tmp"; return 1; }
    fi
    printf 'PLUM_TAG=%s\n' "$tag" >> "$tmp" || { rm -f "$tmp"; return 1; }
    mv -f "$tmp" "$ENV_FILE"
}

local_digest() {
    # The RepoDigest of the image compose would run. Empty for a locally loaded tarball image, which
    # has no registry digest at all — that is a legitimate state (deploy.sh's default path), not an
    # error, and it reads as "unknown" rather than "out of date".
    docker image inspect "$(image_name):$(current_tag)" \
        --format '{{range .RepoDigests}}{{println .}}{{end}}' 2>/dev/null \
        | head -1 | awk -F'@' '{print $2}'
}

remote_digest() {
    # The registry's digest for a tag, over the plain HTTP API — no jq, no skopeo, no docker login.
    # ghcr.io hands an anonymous pull token to anyone for a public package.
    #
    # A HEAD would be cheaper, but ghcr.io does not always return Docker-Content-Digest on one, so
    # this GETs the manifest and reads the header. Accept must list BOTH the OCI index and the
    # Docker v2 list types or a multi-arch tag answers 404.
    local name="$1" tag="$2" repo token
    repo="${name#*/}"
    token="$(curl -fsSL --max-time 15 "https://ghcr.io/token?scope=repository:${repo}:pull" 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("token",""))' 2>/dev/null)" || return 1
    [[ -n "$token" ]] || return 1
    curl -fsSL --max-time 20 -D - -o /dev/null \
        -H "Authorization: Bearer ${token}" \
        -H "Accept: application/vnd.oci.image.index.v1+json" \
        -H "Accept: application/vnd.docker.distribution.manifest.list.v2+json" \
        -H "Accept: application/vnd.oci.image.manifest.v1+json" \
        -H "Accept: application/vnd.docker.distribution.manifest.v2+json" \
        "https://ghcr.io/v2/${repo}/manifests/${tag}" 2>/dev/null \
        | awk 'tolower($1) == "docker-content-digest:" {gsub(/\r/,"",$2); print $2; exit}'
}

compose() {
    # deploy.sh writes COMPOSE_PROFILES into $ROOT/.env, so a bare invocation in that directory
    # selects the right profile with no flags. Trixie has the plugin; older hosts have the v1 binary.
    if docker compose version >/dev/null 2>&1; then
        (cd "$ROOT" && docker compose "$@")
    else
        (cd "$ROOT" && docker-compose "$@")
    fi
}

# --- actions --------------------------------------------------------------------------------------

do_check() {
    local name tag remote
    name="$(image_name)"; tag="${1:-$(current_tag)}"
    S_CHANNEL="$tag"; S_IMAGE="${name}:${tag}"; S_DIGEST="$(local_digest)"
    S_LAST_CHECK="$(now_iso)"
    if remote="$(remote_digest "$name" "$tag")" && [[ -n "$remote" ]]; then
        S_AVAILABLE="$remote"
        log "check: running=${S_DIGEST:-unknown} available=${remote}"
    else
        # No network is the normal state on an isolated AV VLAN. "Unknown" is the honest answer and
        # the GUI must render it as such — reporting "up to date" here would be a lie that hides a
        # unit stuck on an old image.
        S_AVAILABLE=""
        log "check: registry unreachable — availability unknown"
    fi
    save_state
}

do_update() {
    local channel="${1:-$(current_tag)}" name
    name="$(image_name)"
    S_CHANNEL="$channel"
    S_IMAGE="${name}:${channel}"
    S_PREVIOUS="$(local_digest)"
    set_tag "$channel"

    phase "pulling"
    if ! compose pull 2>&1 | sed 's/^/    /'; then
        # Nothing has been stopped at this point, so the unit is still playing. That is the whole
        # reason pull runs before up.
        S_PHASE="failed"
        record_result failed "docker compose pull failed — the unit is untouched and still running"
        save_state
        log "pull FAILED — unit untouched"
        return 1
    fi

    phase "restarting"
    if ! compose up -d 2>&1 | sed 's/^/    /'; then
        S_PHASE="failed"
        S_DIGEST="$(local_digest)"
        record_result failed "docker compose up -d failed — roll back with: PLUM_TAG=<old> docker compose up -d"
        save_state
        log "up -d FAILED"
        return 1
    fi

    S_DIGEST="$(local_digest)"
    S_PHASE="idle"
    if [[ -n "$S_PREVIOUS" && "$S_PREVIOUS" == "$S_DIGEST" ]]; then
        record_result ok "already up to date — no new image"
    else
        record_result ok "updated"
    fi
    save_state
    log "update complete: ${S_PREVIOUS:-unknown} -> ${S_DIGEST:-unknown}"
    return 0
}

do_watch() {
    [[ -f "$REQUEST" ]] || { log "watch: no request"; return 0; }
    local channel check_only
    channel="$(python3 -c '
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    d = {}
print(d.get("channel") or "")' "$REQUEST" 2>/dev/null)"
    check_only="$(python3 -c '
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    d = {}
print("1" if d.get("checkOnly") else "0")' "$REQUEST" 2>/dev/null)"

    # Consume the request BEFORE acting. `up -d` kills the container that wrote it, and a request
    # still on disk after a successful update would be replayed by the .path unit the moment the
    # container comes back — an update loop that looks like a unit restarting forever.
    rm -f "$REQUEST"

    if [[ "$check_only" == "1" ]]; then
        do_check "${channel:-$(current_tag)}"
    else
        do_update "${channel:-$(current_tag)}"
    fi
}

main() {
    local action="${1:-watch}"
    mkdir -p "$CONFIG" 2>/dev/null || true
    load_state

    # One at a time. Two clicks in the GUI, or a .path trigger landing on top of a timer check, must
    # not run two pulls against the same compose project.
    exec 9>"$LOCK"
    if ! flock -n 9; then
        log "another run holds the lock — skipping"
        return 0
    fi

    case "$action" in
        watch)  do_watch ;;
        check)  do_check "${2:-}" ;;
        update) do_update "${2:-}" ;;
        state)  [[ -f "$STATE" ]] && cat "$STATE" || echo '{}' ;;
        # An install writes the state file once with nothing in it but the agent version, which is
        # what tells the container an agent EXISTS at all. Without it the API refuses every request
        # and the GUI correctly reports the host as unprovisioned.
        init)   save_state; log "agent ${AGENT_VERSION} registered" ;;
        *)      echo "usage: plum-updater.sh [watch|check|update|state|init] [channel]" >&2; return 2 ;;
    esac
}

main "$@"
