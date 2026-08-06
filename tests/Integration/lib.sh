#!/usr/bin/env bash
# Shared harness for Plum-Audio hardware integration tests.
#
# These run against real Pis (and, for tier 4, real third-party devices), so every script sources
# this, declares which host(s) it needs as arguments, prints PASS/FAIL per assertion, and exits
# non-zero if anything failed. The cardinal rule for tier 4: leave the rig as you found it — tests
# run against live devices in someone's home. Use `defer` for teardown that must always run.
#
# Usage in a test script:
#     source "$(dirname "$0")/lib.sh"
#     UNIT="${1:?usage: t2_x.sh <unit-host>}"
#     ssh_json "$UNIT" http://127.0.0.1:5001/api/mesh/view
#     assert_eq "$(...)" expected "some source is active"
#     finish   # prints the summary and exits with the right code

set -uo pipefail

# The rig credential is NOT stored in this repo. Export PLUM_TEST_PW, or put it in
# docker/.deploy.env (gitignored), which is sourced here when it exists.
_PLUM_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
[[ -f "${_PLUM_ROOT}/docker/.deploy.env" ]] && source "${_PLUM_ROOT}/docker/.deploy.env"
PW="${PLUM_TEST_PW:?not set — export it, or create docker/.deploy.env containing PLUM_TEST_PW=<rig password>}"
USER_="${PLUM_TEST_USER:-plum-admin}"
SSH_OPTS="-o ConnectTimeout=20 -o StrictHostKeyChecking=no -o LogLevel=ERROR"

_PASS=0
_FAIL=0
_DEFERS=()

# --- ssh / curl helpers -----------------------------------------------------------------------

# ssh_ <host> <command...> — run a command on a Pi, stdout passed through.
ssh_() {
    local host="$1"; shift
    sshpass -p "$PW" ssh $SSH_OPTS "${USER_}@${host}" "$@"
}

# curl_ <host> <method> <path-or-url> [json-body] — curl from ON the Pi (loopback APIs).
curl_() {
    local host="$1" method="$2" url="$3" body="${4:-}"
    if [[ "$url" != http* ]]; then url="http://127.0.0.1${url}"; fi
    if [[ -n "$body" ]]; then
        ssh_ "$host" "curl -s -m 20 -X $method '$url' -H 'Content-Type: application/json' -d '$body'"
    else
        ssh_ "$host" "curl -s -m 20 -X $method '$url'"
    fi
}

# ssh_json <host> <url> [jq-ish python expr on the parsed dict as `d`] — GET + optional extract.
# With no expr, prints the raw JSON. With an expr, prints `python -c` evaluating it against `d`.
ssh_json() {
    local host="$1" url="$2" expr="${3:-}"
    local raw; raw="$(curl_ "$host" GET "$url")"
    if [[ -z "$expr" ]]; then printf '%s' "$raw"; return; fi
    printf '%s' "$raw" | ssh_ "$host" "python3 -c 'import json,sys; d=json.load(sys.stdin); print($expr)'"
}

# --- assertions -------------------------------------------------------------------------------

_ok()   { _PASS=$((_PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
_no()   { _FAIL=$((_FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; [[ -n "${2:-}" ]] && printf '        %s\n' "$2"; }

assert_eq() {  # assert_eq <actual> <expected> <label>
    if [[ "$1" == "$2" ]]; then _ok "$3"; else _no "$3" "expected [$2], got [$1]"; fi
}
assert_contains() {  # assert_contains <haystack> <needle> <label>
    if [[ "$1" == *"$2"* ]]; then _ok "$3"; else _no "$3" "[$2] not found in [$1]"; fi
}
assert_not_contains() {
    if [[ "$1" != *"$2"* ]]; then _ok "$3"; else _no "$3" "[$2] unexpectedly present"; fi
}

# --- wait_for: poll a command until it prints the expected value (or time out) -----------------
# wait_for <expected> <timeout-s> <cmd...>
wait_for() {
    local expected="$1" timeout="$2"; shift 2
    local deadline=$((SECONDS + timeout)) got=""
    while (( SECONDS < deadline )); do
        got="$("$@" 2>/dev/null)"
        [[ "$got" == "$expected" ]] && { printf '%s' "$got"; return 0; }
        sleep 2
    done
    printf '%s' "$got"; return 1
}

# wait_for_nonempty <timeout-s> <cmd...> — poll until the command prints something non-empty.
wait_for_nonempty() {
    local timeout="$1"; shift
    local deadline=$((SECONDS + timeout)) got=""
    while (( SECONDS < deadline )); do
        got="$("$@" 2>/dev/null)"
        [[ -n "$got" ]] && { printf '%s' "$got"; return 0; }
        sleep 1
    done
    printf '%s' "$got"; return 1
}

# --- teardown: registered LIFO, always runs on exit -------------------------------------------
defer() { _DEFERS+=("$*"); }
_run_defers() { local i; for (( i=${#_DEFERS[@]}-1; i>=0; i-- )); do eval "${_DEFERS[$i]}"; done; }
trap _run_defers EXIT

finish() {
    printf '\n  %d passed, %d failed\n' "$_PASS" "$_FAIL"
    [[ "$_FAIL" -eq 0 ]]
}
