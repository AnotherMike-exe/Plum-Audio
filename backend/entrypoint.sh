#!/bin/sh
# Plum-Audio container entrypoint. Three jobs, then hand off to supervisord:
#   1. create the runtime directories (a build-time mkdir is masked by the /config and /data mounts)
#   2. apply the Binhex PUID/PGID/UMASK contract to the volumes
#   3. fill in this unit's mesh identity when the operator did not
#
# The process tree runs as ROOT and that is deliberate: it opens /dev/snd, talks to the HOST system
# D-Bus (bluetoothd, Avahi) and wants realtime scheduling for the audio path. PUID/PGID govern the
# ownership of what we WRITE, so /config and /data stay readable and editable on the host.
set -e

umask "${UMASK:-002}"

PUID="${PUID:-99}"
PGID="${PGID:-100}"

mkdir -p /config/logs /data /media /tmp/airplay-covers

# Per-source state written by the source managers: rendered daemon configs, per-endpoint logs, and
# — the part worth keeping across container replacement — go-librespot's state.json, which holds
# each Spotify endpoint's authorisation. Pre-creating them means the first write already has the
# right ownership rather than root's.
mkdir -p /data/shairport /data/go-librespot /data/bluetooth

# Best-effort: a volume the operator pre-populated as another user is theirs to keep, and a failed
# chown must not stop the unit from playing music.
chown -R "${PUID}:${PGID}" /config /data 2>/dev/null || true

# --- Mesh identity ------------------------------------------------------------------------------
# Every unit in the mesh must be distinct, so the bare defaults in sendspin_server.py ("unit-local")
# are wrong for anything but a single box. Derive from the hostname when the operator did not say.
HOST_SHORT="$(hostname -s 2>/dev/null || cat /etc/hostname 2>/dev/null || echo plum)"
: "${PLUM_UNIT_ID:=unit-$(echo "$HOST_SHORT" | tr '[:upper:]' '[:lower:]')}"
: "${PLUM_UNIT_NAME:=${HOST_SHORT}}"
# The server registers PLUM_LOCAL_PLAYER_ID and the player answers to PLUM_PLAYER_ID: they are the
# same endpoint under two names, and a mismatch means the server dials a player that never claims to
# be the one it registered. Whichever the operator set wins; set neither and both derive from the
# unit id.
: "${PLUM_LOCAL_PLAYER_ID:=${PLUM_PLAYER_ID:-${PLUM_UNIT_ID}-player}}"
: "${PLUM_PLAYER_ID:=${PLUM_LOCAL_PLAYER_ID}}"
: "${PLUM_PLAYER_NAME:=${PLUM_UNIT_NAME}}"

# The player URL is not just ours to dial — we REGISTER it so peer units can reclaim this player
# when routing audio here (mesh/router.py). A 127.0.0.1 default would advertise an endpoint no peer
# can reach, and the failure is a roam that silently never lands. Derive the real LAN address.
if [ -z "${PLUM_LOCAL_PLAYER_URL:-}" ]; then
    LAN_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1; i<=NF; i++) if ($i == "src") {print $(i+1); exit}}')"
    if [ -z "$LAN_IP" ]; then
        echo "entrypoint: WARNING no LAN address found; the mesh cannot reclaim this player" >&2
        LAN_IP=127.0.0.1
    fi
    PLUM_LOCAL_PLAYER_URL="ws://${LAN_IP}:${PLUM_PLAYER_PORT:-8928}/sendspin"
fi

export PLUM_UNIT_ID PLUM_UNIT_NAME PLUM_LOCAL_PLAYER_ID PLUM_LOCAL_PLAYER_URL PLUM_PLAYER_ID PLUM_PLAYER_NAME

echo "entrypoint: unit=${PLUM_UNIT_ID} (${PLUM_UNIT_NAME}) player=${PLUM_LOCAL_PLAYER_ID} at ${PLUM_LOCAL_PLAYER_URL}"

exec supervisord -c /app/supervisord/supervisord.conf
