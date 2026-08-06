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

INVOKED_FROM="$PWD"          # captured BEFORE the cd — see the --tarball resolution below
cd "$(dirname "$0")"
HERE="$PWD"

USER_="${PLUM_TEST_USER:-plum-admin}"
PW="${PLUM_TEST_PW:-REDACTED-USE-PLUM_TEST_PW}"
# UserKnownHostsFile=/dev/null, not just StrictHostKeyChecking=no: a REIMAGED unit presents a new
# host key, and a conflicting known_hosts entry makes ssh refuse the connection outright — password
# auth is disabled in that state, so the deploy fails on the very first ssh of every unit with a
# man-in-the-middle warning. StrictHostKeyChecking=no only covers a host ssh has never seen. Since
# the password is in this file there is no key-pinning posture to preserve, and the alternative is
# telling the operator to run ssh-keygen -R by hand on exactly the runs (a fresh image) where they
# are least expecting the failure. Found re-imaging the .201 units for the alpha, 2026-08-06.
SSH_OPTS="-o ConnectTimeout=20 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"
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
elif [[ "$TARBALL" != /* ]]; then
    # A relative --tarball has to be resolved against the caller's cwd, not this script's. We have
    # already cd'd into docker/, so `--tarball dist/plum-audio-<tag>-arm64.tar.gz` — the form
    # docs/OPERATIONS.md documents, run from the repo root — would look for docker/dist/... and fail
    # with "no image tarball (run build.sh first)" about a file that is plainly there.
    for cand in "${INVOKED_FROM}/${TARBALL}" "${HERE}/../${TARBALL}" "${TARBALL}"; do
        [[ -f "$cand" ]] && { TARBALL="$cand"; break; }
    done
fi
[[ -f "$TARBALL" ]] || { echo "no image tarball at '${TARBALL:-<none>}' (run docker/build.sh first)" >&2; exit 1; }
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

# units.conf is the ONLY thing that makes units distinguishable, and nothing else checks it.
#
# The unit-name column becomes PLUM_UNIT_NAME, which a unit with no settings.json yet adopts as its
# own display name AND as the name of every source endpoint it offers — so two rows sharing a name
# produce two units that are indistinguishable in the mesh view, in the GUI's unit cards, and to an
# AirPlay sender picking a speaker. That is the confusion the greenfield alpha hit on 2026-08-06. The
# ids are worse than cosmetic: the mesh keys routing, group membership and per-player volume off them,
# so two units claiming one unit_id corrupt each other's routing rather than merely looking alike.
#
# A clash DISAMBIGUATES, it does not stop the deploy. Refusing was the first attempt and it was the
# wrong trade: it turns a cosmetic naming slip into a rig that will not deploy at all, and the failure
# lands after someone has already re-flashed a card. So a duplicated field gets `-<token>` appended and
# a loud warning, which is recoverable in the GUI at any time. Rename in Settings to take control.
#
# Computed once for the WHOLE table rather than per selected host: a clash is a property of the file,
# and `deploy.sh <one-host>` must suffix identically to `deploy.sh all` or the two disagree.
dup_in_column() {  # dup_in_column <1-based field>
    grep -vE '^\s*(#|$)' "$UNITS_FILE" \
        | awk -F'|' -v f="$1" '{v=$f; gsub(/^[ \t]+|[ \t]+$/,"",v); if (v!="") print v}' \
        | sort | uniq -d
}
DUP_HOST="$(dup_in_column 1)"
DUP_UNIT_ID="$(dup_in_column 2)"
DUP_UNIT_NAME="$(dup_in_column 3)"
DUP_PLAYER_ID="$(dup_in_column 4)"
DUP_PLAYER_NAME="$(dup_in_column 5)"

if [[ -n "$DUP_HOST$DUP_UNIT_ID$DUP_UNIT_NAME$DUP_PLAYER_ID$DUP_PLAYER_NAME" ]]; then
    printf '\033[33m!! %s has duplicate values; they will be suffixed per unit:\033[0m\n' "${UNITS_FILE##*/}"
    for pair in "host:$DUP_HOST" "unit_id:$DUP_UNIT_ID" "unit_name:$DUP_UNIT_NAME" \
                "player_id:$DUP_PLAYER_ID" "player_name:$DUP_PLAYER_NAME"; do
        [[ -n "${pair#*:}" ]] && printf '     %-12s %s\n' "${pair%%:*}" "$(echo "${pair#*:}" | tr '\n' ' ')"
    done
    # A duplicated HOST is the one case a suffix cannot help — it is the same box twice, so the second
    # pass simply overwrites the first. Say so; the run is still harmless.
    [[ -n "$DUP_HOST" ]] && printf '\033[33m     (a repeated host just deploys twice to the same box)\033[0m\n'
fi

# A short, stable, per-unit token used only to break a units.conf clash.
#
# The Pi's SoC serial, not a MAC: it survives a NIC swap, a reflash and every reboot, whereas a
# default-route MAC moves the moment a unit is put on wlan0 instead of eth0 — which would silently
# RENAME the unit on its next deploy, the exact problem this is here to prevent. MACs are the fallback
# for non-Pi hosts, lowest-first so enumeration order cannot change the answer.
unit_token() {  # unit_token <host>
    ssh_ "$1" "bash -s" <<'EOS'
set -uo pipefail
t="$(sed -n 's/^Serial[[:space:]]*:[[:space:]]*//p' /proc/cpuinfo 2>/dev/null | tail -1 | tr -d ' ')"
if [[ -z "$t" ]]; then
    macs=""
    for n in /sys/class/net/*; do
        i="${n##*/}"
        case "$i" in lo|docker*|veth*|br-*|dummy*) continue ;; esac
        [[ -e "$n/device" ]] || continue          # physical only
        macs="${macs}$(cat "$n/address" 2>/dev/null | tr -d ':')
"
    done
    t="$(printf '%s' "$macs" | grep -v '^$' | sort | head -1)"
fi
# Last four hex, uppercased. Short enough to read out over the phone, long enough that a rig would
# need ~65k units before a collision is likely.
printf '%s' "$t" | tr -cd '0-9A-Fa-f' | tail -c 4 | tr '[:lower:]' '[:upper:]'
EOS
}

# Echo $1, suffixed with the unit's token when $2 (a newline-separated duplicate list) contains it.
# Warnings go to STDERR: this runs inside a command substitution, so anything on stdout becomes part
# of the value and would end up in plum-audio.env.
disambiguate() {  # disambiguate <value> <dup-list> <label> <host>
    local v="$1" dups="$2" label="$3" host="$4"
    if [[ -n "$dups" ]] && printf '%s\n' "$dups" | grep -qxF -- "$v"; then
        [[ -n "${TOKEN:-}" ]] || TOKEN="$(unit_token "$host")"
        if [[ -n "$TOKEN" ]]; then
            printf '\033[33m    !! %s %s is duplicated in units.conf -> using %s-%s\033[0m\n' \
                "$label" "$v" "$v" "$TOKEN" >&2
            printf '%s-%s' "$v" "$TOKEN"
            return
        fi
        printf '\033[33m    !! %s %s is duplicated and no token could be derived\033[0m\n' "$label" "$v" >&2
    fi
    printf '%s' "$v"
}

if [[ " ${HOSTS[*]} " == *" all "* ]]; then
    # Plain read loop, not `mapfile`: this is normally run from macOS, whose /bin/bash is 3.2 and
    # has no mapfile. The failure only ever showed on `deploy.sh all`, since single-host runs skip it.
    HOSTS=()
    while IFS= read -r _h; do
        [[ -n "$_h" ]] && HOSTS+=("$_h")
    done < <(units_all)
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

    # Break any units.conf clash before the values reach plum-audio.env. TOKEN is fetched at most once
    # per unit, and only when there is actually a clash — an unambiguous table costs no extra ssh.
    local TOKEN=""
    unit_id="$(disambiguate "$unit_id" "$DUP_UNIT_ID" unit_id "$host")"
    unit_name="$(disambiguate "$unit_name" "$DUP_UNIT_NAME" unit_name "$host")"
    player_id="$(disambiguate "$player_id" "$DUP_PLAYER_ID" player_id "$host")"
    player_name="$(disambiguate "$player_name" "$DUP_PLAYER_NAME" player_name "$host")"

    # A DAC column of `none` means this host has no audio output: no player process, no /dev/snd, and
    # the headless compose profile (the audio one cannot even be CREATED without /dev/snd).
    local profile player_enabled expected_programs
    # `tr`, not ${dac,,}: that is bash 4+, and macOS — where this script is RUN — ships bash 3.2,
    # so the parameter expansion is a hard syntax error before any unit is contacted.
    if [[ "$(printf '%s' "$dac" | tr '[:upper:]' '[:lower:]')" == "none" ]]; then
        profile="headless"; player_enabled=0; expected_programs=3
    else
        profile="audio";    player_enabled=1; expected_programs=4
    fi

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
    ssh_ "$host" "bash -s -- '$PW' '$REMOTE_ROOT' '$unit_id' '$unit_name' '$player_id' '$player_name' '$dac' '$profile' '$player_enabled'" <<'EOS' || return 1
set -euo pipefail
PW="$1"; ROOT="$2"; UNIT_ID="$3"; UNIT_NAME="$4"; PLAYER_ID="$5"; PLAYER_NAME="$6"; DAC="$7"
PROFILE="$8"; PLAYER_ENABLED="$9"
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
PLUM_PLAYER_ENABLED=${PLAYER_ENABLED}
PLUM_STATIC_DELAY_MS=150
PLUM_LOG_LEVEL=INFO
PLUM_MESH_ENABLED=1
PLEXAMP_ENABLED=0
ENV
# COMPOSE_PROFILES has to be here, not in plum-audio.env: env_file is container environment, while
# this is compose INTERPOLATION. Written beside the compose file so a bare `docker compose up -d` or
# `restart` run by hand in this directory selects the same service deploy.sh does.
cat > "$ROOT/.env" <<COMPOSEENV
# Generated by docker/deploy.sh — selects which service in docker-compose.yml applies to this host.
COMPOSE_PROFILES=${PROFILE}
COMPOSEENV
echo "    $ROOT/plum-audio.env  (tz=${TZ_HOST}, uid=$(id -u):$(id -g), profile=${PROFILE})"
EOS

    # 7 + 8. Up, then prove it actually serves — a running container says nothing about whether
    # supervisord's tree came up.
    say "$host — up"
    ssh_ "$host" "bash -s -- '$PW' '$REMOTE_ROOT' '$expected_programs' '$profile'" <<'EOS'
set -euo pipefail
PW="$1"; ROOT="$2"; WANT="$3"; PROFILE="$4"
# Belt and braces alongside $ROOT/.env: compose v1 (the Debian units) and v2 (the Docker-CE one)
# differ in how they pick .env up, and selecting no profile silently starts NOTHING rather than
# failing — a deploy that looks like it worked and left the unit down.
export COMPOSE_PROFILES="$PROFILE"
s() { echo "$PW" | sudo -S -p '' "$@"; }
cd "$ROOT"
# Plugin (`docker compose`) on the Docker-CE unit, standalone (`docker-compose`) on the Debian ones.
if s docker compose version >/dev/null 2>&1; then DC="docker compose"; else DC="docker-compose"; fi
# Through `env`, not an export: s() shells out via sudo, which strips the environment, so an exported
# COMPOSE_PROFILES would never reach compose — and an unset profile starts NOTHING while exiting 0.
s env COMPOSE_PROFILES="$PROFILE" $DC up -d 2>&1 | sed 's/^/    /'

# Wait on OUR process tree, not on a port. Under host networking a port can be answered by
# something that is not this container (that is exactly how a stale host nginx passed a GUI check),
# so supervisord's own view is the only honest readiness signal.
SUPCTL="supervisorctl -c /app/supervisord/supervisord.conf"
# Wait for every DECLARED program, not a hardcoded four: a headless unit has three (no
# sendspin_player), and `-ge 4` would spin the full 90s on every deploy and then pass anyway — slow
# enough to be mistaken for a real fault. Counting non-empty status lines rather than trusting WANT
# alone means an unexpected program count still resolves, and the strict check below still judges it.
for i in $(seq 1 45); do
    st="$(s docker exec plum-audio $SUPCTL status 2>/dev/null || true)"
    tot="$(grep -c . <<<"$st" || true)"
    run="$(grep -c RUNNING <<<"$st" || true)"
    [[ "$tot" -ge "$WANT" && "$run" -eq "$tot" ]] && break
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
if [[ "$WANT" -lt 4 ]]; then
    # No player by design — reporting NOT LISTENING here would cry wolf on every headless deploy.
    echo "    sendspin player :8928 not started (this unit has no audio output)"
else
    echo "    sendspin player :8928 $(s ss -ltn | grep -q ':8928' && echo listening || echo 'NOT LISTENING')"
fi
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
