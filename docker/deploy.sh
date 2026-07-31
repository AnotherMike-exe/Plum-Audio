#!/usr/bin/env bash
# Deploy the Plum-Audio unit container to the R&D Pis.
#
#   ./deploy.sh all                          # every unit in units.conf
#   ./deploy.sh 192.0.2.10              # one unit
#   ./deploy.sh all --tarball dist/x.tar.gz  # a specific build (default: newest in dist/)
#   ./deploy.sh all --no-migrate             # skip importing the ~/plum-test state
#
# What it does per unit, in order: preflight -> ensure Docker -> STOP the pre-container dev stack
# and any old containers -> create /opt/plum-audio -> import existing rig state on first deploy ->
# load the image -> install compose + a per-unit env -> up -> verify.
#
# It is deliberately re-runnable: a second deploy of a new build stops at "load + up" and leaves
# /opt/plum-audio/{config,data} alone, so settings.json and the Spotify authorisations survive.
#
# The ~/plum-test tree is left ON DISK, only its processes are stopped — reverting to the dev stack
# is `docker compose down` on the unit plus ~/plum-test/run_*.sh, with no restore step.
set -euo pipefail

cd "$(dirname "$0")"
HERE="$PWD"

USER_="${PLUM_TEST_USER:-plum-admin}"
PW="${PLUM_TEST_PW:-REDACTED-USE-PLUM_TEST_PW}"
SSH_OPTS="-o ConnectTimeout=20 -o StrictHostKeyChecking=no -o LogLevel=ERROR"
REMOTE_ROOT="/opt/plum-audio"
UNITS_FILE="${HERE}/units.conf"

MIGRATE=1
TARBALL=""
HOSTS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        all)          HOSTS+=("all"); shift ;;
        --tarball)    TARBALL="$2"; shift 2 ;;
        --no-migrate) MIGRATE=0; shift ;;
        -h|--help)    sed -n '2,20p' "$0"; exit 0 ;;
        -*)           echo "unknown flag $1" >&2; exit 2 ;;
        *)            HOSTS+=("$1"); shift ;;
    esac
done
[[ ${#HOSTS[@]} -gt 0 ]] || { echo "usage: deploy.sh <all|host...> [--tarball f] [--no-migrate]" >&2; exit 2; }

command -v sshpass >/dev/null || { echo "sshpass required (brew install sshpass)" >&2; exit 1; }

# Newest tarball wins when none was named — the common case is "I just ran build.sh".
if [[ -z "$TARBALL" ]]; then
    TARBALL="$(ls -t ../dist/plum-audio-*.tar.gz 2>/dev/null | head -1 || true)"
fi
[[ -f "$TARBALL" ]] || { echo "no image tarball (run docker/build.sh first)" >&2; exit 1; }
TARBALL="$(cd "$(dirname "$TARBALL")" && pwd)/$(basename "$TARBALL")"

# A deploy opens a dozen authenticated connections per unit in quick succession, and sshd will
# occasionally refuse one ("Permission denied" on a password that is demonstrably correct). Retry
# rather than fail a whole unit on a transient auth refusal.
retry_() {
    local n=0
    until "$@"; do
        n=$((n + 1))
        [[ $n -ge 3 ]] && return 1
        sleep 3
    done
}
ssh_()  { retry_ sshpass -p "$PW" ssh $SSH_OPTS "${USER_}@$1" "${@:2}"; }
scp_()  { retry_ sshpass -p "$PW" scp $SSH_OPTS "$1" "${USER_}@$2:$3"; }
# Send a local file over an existing-style ssh session instead of a second scp auth. Used for the
# small config files; the image tarball still goes by scp (scp is far faster for 200 MB).
put_()  { retry_ sshpass -p "$PW" ssh $SSH_OPTS "${USER_}@$1" "cat > '$3'" < "$2"; }
say()   { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn()  { printf '\033[33m    !! %s\033[0m\n' "$*"; }

# --- unit table --------------------------------------------------------------------------------

units_all() { grep -vE '^\s*(#|$)' "$UNITS_FILE" | awk -F'|' '{gsub(/ /,"",$1); print $1}'; }

unit_field() {  # unit_field <host> <1-based field>
    grep -vE '^\s*(#|$)' "$UNITS_FILE" \
        | awk -F'|' -v h="$1" -v f="$2" '{gsub(/^[ \t]+|[ \t]+$/,"",$1); if ($1==h) {v=$f; gsub(/^[ \t]+|[ \t]+$/,"",v); print v}}'
}

if [[ " ${HOSTS[*]} " == *" all "* ]]; then
    mapfile -t HOSTS < <(units_all)
fi

# --- per-unit deploy ---------------------------------------------------------------------------

deploy_one() {
    local host="$1"
    local unit_id unit_name player_id player_name dac
    unit_id="$(unit_field "$host" 2)"
    unit_name="$(unit_field "$host" 3)"
    player_id="$(unit_field "$host" 4)"
    player_name="$(unit_field "$host" 5)"
    dac="$(unit_field "$host" 6)"
    [[ -n "$unit_id" ]] || { warn "$host is not in units.conf — skipping"; return 1; }

    say "$host — ${unit_id} (${unit_name})"

    # 1. Preflight: reachable, arm64, sudo, and the host daemons this container depends on.
    ssh_ "$host" "bash -s -- '$PW'" <<'EOS' || return 1
set -euo pipefail
PW="$1"
s() { echo "$PW" | sudo -S -p '' "$@"; }
echo "    host:  $(hostname) / $(dpkg --print-architecture) / $(. /etc/os-release; echo "$PRETTY_NAME")"
[[ "$(dpkg --print-architecture)" == arm64 ]] || { echo "    !! not arm64" >&2; exit 1; }
s true || { echo "    !! sudo failed" >&2; exit 1; }
# The container talks to BOTH of these over the host's system bus; without them AirPlay/Spotify
# never advertise and Bluetooth never sees the radio.
systemctl is-active --quiet avahi-daemon || echo "    !! avahi-daemon is NOT running on the host"
systemctl is-active --quiet bluetooth    || echo "    !! bluetoothd is NOT running on the host"
# Bluetooth scrub reporting needs the patched bluez (backend/config/bluez/). Informational: an
# unpatched unit still plays audio, it just cannot report position from the phone.
dpkg -l bluez 2>/dev/null | grep -q '+plum' \
    || echo "    !! host bluez is UNPATCHED (no AVRCP position; see backend/config/bluez/)"
EOS

    # 2. Docker. .113 shipped without it, and compose reaches the units two different ways: .122
    #    runs Docker CE from Docker's own apt repo (compose as a CLI plugin, `docker compose`),
    #    while the Debian-packaged units get trixie's `docker-compose` 2.26 standalone binary.
    #    Same compose file either way — so detect the invocation rather than forcing one repo
    #    layout onto every unit. NOTE: trixie has no `docker-compose-v2`; the package is
    #    `docker-compose` and it is compose v2, not the retired Python v1.
    say "$host — docker"
    ssh_ "$host" "bash -s -- '$PW'" <<'EOS' || return 1
set -euo pipefail
PW="$1"
s() { echo "$PW" | sudo -S -p '' "$@"; }
need=()
command -v docker >/dev/null || need+=(docker.io)
if ! s docker compose version >/dev/null 2>&1 && ! command -v docker-compose >/dev/null 2>&1; then
    need+=(docker-compose)
fi
if [[ ${#need[@]} -gt 0 ]]; then
    echo "    installing: ${need[*]}"
    s apt-get update -qq
    # `env` sets DEBIAN_FRONTEND: sudo does not take VAR=value before the command.
    s env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${need[@]}"
fi
s systemctl enable --now docker >/dev/null 2>&1 || true
# Convenience only (takes effect at next login); this script keeps using sudo regardless.
s usermod -aG docker "$USER" 2>/dev/null || true
if s docker compose version >/dev/null 2>&1; then dc="docker compose"; else dc="docker-compose"; fi
echo "    $(s docker --version 2>&1)  |  $(s $dc version 2>&1 | head -1)  [$dc]"
EOS

    # 3. Stop what would fight the container for ports, ALSA, D-Bus names and the radio: the
    #    pre-container dev stack, and any old container generation.
    say "$host — stopping the pre-container stack"
    ssh_ "$host" "bash -s -- '$PW'" <<'EOS' || return 1
set -uo pipefail
PW="$1"
s() { echo "$PW" | sudo -S -p '' "$@"; }

# Containers FIRST. A previous plum-audio generation runs the very same script names, and the host
# PID namespace shows them, so sweeping processes first would match the container's own tree and
# report it as leftover dev stack.
for c in plum-audio plum-snapcast-server plum-snapcast-frontend; do
    if s docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$c"; then
        echo "    removing container $c"
        s docker rm -f "$c" >/dev/null
    fi
done
for i in $(s docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -E 'plum-snapcast' || true); do
    echo "    removing image $i"
    s docker rmi -f "$i" >/dev/null 2>&1 || true
done

[[ -x ~/plum-test/stop_stack.sh ]] && ~/plum-test/stop_stack.sh >/dev/null 2>&1

# Patterns are scoped to plum-test / our own venv so nothing of the HOST's (bluetoothd, avahi, the
# system obexd if any) is caught by a stray match. `spin/bin/python` is the rig's venv: the config
# API runs as a bare `server.py` from its own cwd, so only the interpreter path identifies it —
# matching 'apis/server.py' alone silently misses it, and it keeps :5002 from the container.
DEV_PATTERNS=('sendspin_server\.py' 'sendspin_player\.py' 'apis/server\.py' 'spin/bin/python server\.py'
              'plum-test/shairport' 'plum-test/go-librespot' 'plum-test/bluetooth'
              'shairport-sync -c /home' 'go-librespot --config_dir /home'
              'bluealsa --profile' '/usr/libexec/bluetooth/obexd')
for pat in "${DEV_PATTERNS[@]}"; do
    pkill -f "$pat" 2>/dev/null
done
sleep 2
# SIGKILL every pattern, not just the Python ones. A shairport-sync whose private session bus has
# already been killed traps SIGTERM and hangs in shutdown — pkill reports success, the process
# stays, and it still holds AirPlay's RAOP port (5050), which is outside the port check below
# because endpoint ports are configurable. Escalating unconditionally is cheaper than reasoning
# about which daemon died cleanly.
for pat in "${DEV_PATTERNS[@]}"; do
    pkill -9 -f "$pat" 2>/dev/null
done
sleep 1

# Survivors are a hard failure, not a note: anything left here holds a source port or an ALSA
# device the container is about to want.
left="$(pgrep -af 'plum-test|spin/bin/python' | grep -v pgrep || true)"
if [[ -n "$left" ]]; then
    echo "    !! dev-stack processes survived SIGKILL:"
    echo "$left" | sed 's/^/      /'
    exit 1
fi

# The pre-container GUI was served by the HOST's nginx (/var/www/plum-audio, with the same proxy
# config the container now ships). The unit container serves :80 itself under host networking, so
# the host copy has to stand down — otherwise the container's nginx crash-loops on bind() while
# the host keeps answering, which reads as a WORKING GUI right up until you notice it is serving
# a stale build. Config and webroot are left on disk; only the service is stopped.
if systemctl is-active --quiet nginx 2>/dev/null; then
    echo "    stopping + disabling host nginx (the container now owns :80)"
    s systemctl disable --now nginx >/dev/null 2>&1 || true
fi

echo "    free disk: $(df -h / | awk 'NR==2{print $4}')"
# The container binds these under host networking. Anything still holding one is a HARD stop, not a
# warning: supervisord would crash-loop that program while the squatter keeps answering on the port,
# so the deploy would look healthy and serve the old thing. Name it and refuse.
busy="$(s ss -ltnp 2>/dev/null | awk '$4 ~ /:(80|5001|5002|8927|8928|8929)$/ {print $4, $6}')"
if [[ -n "$busy" ]]; then
    echo "    !! ports still bound after stopping the dev stack:"
    echo "$busy" | sed 's/^/      /'
    exit 1
fi
exit 0
EOS

    # 4. Volume layout + first-deploy state import.
    say "$host — ${REMOTE_ROOT} + state"
    ssh_ "$host" "bash -s -- '$PW' '$REMOTE_ROOT' '$MIGRATE'" <<'EOS' || return 1
set -euo pipefail
PW="$1"; ROOT="$2"; MIGRATE="$3"
s() { echo "$PW" | sudo -S -p '' "$@"; }
s mkdir -p "$ROOT"/{config,data,media}
s chown -R "$(id -u):$(id -g)" "$ROOT"

if [[ "$MIGRATE" == "1" && -d ~/plum-test && ! -f "$ROOT/data/settings.json" ]]; then
    echo "    first deploy — importing ~/plum-test state"
    # settings.json IS the unit's configuration: endpoints, device names, visualiser prefs, audio
    # output. Importing it means the container comes up as the same unit the rig has been testing,
    # not a factory-default one.
    [[ -f ~/plum-test/settings.json ]] && cp -a ~/plum-test/settings.json "$ROOT/data/settings.json"
    # go-librespot's state.json holds each Spotify endpoint's authorisation — lose it and every
    # endpoint has to be re-picked from a Spotify client before it will play.
    for d in go-librespot shairport bluetooth; do
        [[ -d ~/plum-test/$d ]] && cp -a ~/plum-test/$d "$ROOT/data/$d"
    done
    # Sockets and lockfiles are per-process liveness artefacts; a stale one makes dbus-daemon
    # refuse to bind or go-librespot think another instance owns the endpoint.
    find "$ROOT/data" \( -name '*.socket' -o -name 'lockfile' -o -name '*.pid' \) -delete 2>/dev/null || true
    echo "    imported: $(cd "$ROOT/data" && ls -d * 2>/dev/null | tr '\n' ' ')"
else
    echo "    keeping existing $ROOT/data (settings.json $( [[ -f "$ROOT/data/settings.json" ]] && echo present || echo absent ))"
fi
EOS

    # 5. Image.
    say "$host — loading $(basename "$TARBALL")"
    scp_ "$TARBALL" "$host" "/tmp/plum-audio-image.tar.gz" || return 1
    ssh_ "$host" "bash -s -- '$PW'" <<'EOS' || return 1
set -euo pipefail
PW="$1"
s() { echo "$PW" | sudo -S -p '' "$@"; }
s docker load -i /tmp/plum-audio-image.tar.gz | sed 's/^/    /'
rm -f /tmp/plum-audio-image.tar.gz
EOS

    # 6. compose + per-unit env. TZ and PUID/PGID are read from the unit itself so files under
    #    /opt/plum-audio stay owned by the account that administers it.
    say "$host — config"
    put_ "$host" "${HERE}/docker-compose.yml" "/tmp/plum-audio-compose.yml" || return 1
    ssh_ "$host" "bash -s -- '$PW' '$REMOTE_ROOT' '$unit_id' '$unit_name' '$player_id' '$player_name' '$dac'" <<'EOS' || return 1
set -euo pipefail
PW="$1"; ROOT="$2"; UNIT_ID="$3"; UNIT_NAME="$4"; PLAYER_ID="$5"; PLAYER_NAME="$6"; DAC="$7"
s() { echo "$PW" | sudo -S -p '' "$@"; }
mv /tmp/plum-audio-compose.yml "$ROOT/docker-compose.yml"
TZ_HOST="$(timedatectl show -p Timezone --value 2>/dev/null || echo UTC)"
cat > "$ROOT/plum-audio.env" <<ENV
# Generated by docker/deploy.sh — edit units.conf and redeploy rather than hand-editing.
PUID=$(id -u)
PGID=$(id -g)
UMASK=002
TZ=${TZ_HOST}
DEBUG=false

PLUM_UNIT_ID=${UNIT_ID}
PLUM_UNIT_NAME=${UNIT_NAME}
PLUM_LOCAL_PLAYER_ID=${PLAYER_ID}
PLUM_PLAYER_NAME=${PLAYER_NAME}

PLUM_DAC_DEVICE=${DAC}
PLUM_STATIC_DELAY_MS=150
PLUM_LOG_LEVEL=INFO
PLUM_MESH_ENABLED=1
PLEXAMP_ENABLED=0
ENV
echo "    $ROOT/plum-audio.env  (tz=${TZ_HOST}, uid=$(id -u):$(id -g))"
EOS

    # 7 + 8. Up, then prove it actually serves — a running container says nothing about whether
    # supervisord's tree came up.
    say "$host — up"
    ssh_ "$host" "bash -s -- '$PW' '$REMOTE_ROOT'" <<'EOS'
set -euo pipefail
PW="$1"; ROOT="$2"
s() { echo "$PW" | sudo -S -p '' "$@"; }
cd "$ROOT"
# Plugin (`docker compose`) on the Docker-CE unit, standalone (`docker-compose`) on the Debian ones.
if s docker compose version >/dev/null 2>&1; then DC="docker compose"; else DC="docker-compose"; fi
s $DC up -d 2>&1 | sed 's/^/    /'

# Wait on OUR process tree, not on a port. Under host networking a port can be answered by
# something that is not this container (that is exactly how a stale host nginx passed a GUI check),
# so supervisord's own view is the only honest readiness signal.
SUPCTL="supervisorctl -c /app/supervisord/supervisord.conf"
for i in $(seq 1 45); do
    st="$(s docker exec plum-audio $SUPCTL status 2>/dev/null || true)"
    [[ "$(grep -c RUNNING <<<"$st")" -ge 4 ]] && break
    sleep 2
done

fail=0
notrunning="$(s docker exec plum-audio $SUPCTL status 2>/dev/null | grep -v RUNNING || true)"
if [[ -n "$notrunning" ]]; then
    printf '    \033[31mFAIL\033[0m %-22s %s\n' "supervisord" "not all programs RUNNING"; fail=1
fi
chk() {  # chk <label> <url> [jq-ish grep]
    if out="$(curl -fsS -m 5 "$2" 2>&1)"; then
        printf '    \033[32mOK\033[0m   %-22s %s\n' "$1" "$(echo "$out" | head -c 90 | tr -d '\n')"
    else
        printf '    \033[31mFAIL\033[0m %-22s %s\n' "$1" "$out"; fail=1
    fi
}
chk "config API :5002"  "http://127.0.0.1:5002/api/settings"
chk "mesh API :5001"    "http://127.0.0.1:5001/api/mesh/view"
chk "web GUI :80"       "http://127.0.0.1/"
echo "    sendspin server :8927 $(s ss -ltn | grep -q ':8927' && echo listening || echo 'NOT LISTENING')"
echo "    sendspin player :8928 $(s ss -ltn | grep -q ':8928' && echo listening || echo 'NOT LISTENING')"
echo "    supervisord:"
s docker exec plum-audio $SUPCTL status 2>&1 | sed 's/^/      /'
if [[ $fail -ne 0 ]]; then
    for prog in $(echo "$notrunning" | awk '{print $1}'); do
        echo "    --- last 15 lines: $prog ---"
        s docker exec plum-audio tail -15 "/config/logs/${prog}.log" 2>&1 | sed 's/^/      /'
    done
    echo "    --- last 15 lines: sendspin_server ---"
    s docker exec plum-audio tail -15 /config/logs/sendspin_server.log 2>&1 | sed 's/^/      /'
fi
exit $fail
EOS
}

rc=0
for h in "${HOSTS[@]}"; do
    deploy_one "$h" || { rc=1; warn "$h FAILED"; }
done

say "done"
[[ $rc -eq 0 ]] && echo "all units deployed" || echo "one or more units failed — see above"
exit $rc
