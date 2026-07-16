#!/bin/bash
# Plum-Audio go-librespot launcher (one per Spotify Connect instance).
# Waits for Avahi (go-librespot's zeroconf_backend=avahi registers Connect discovery via system
# Avahi over D-Bus), then execs go-librespot with the instance's config dir. Called by supervisord:
#   start-spotify.sh <instance_id>
# Env overrides: PLUM_GOLIBRESPOT_BIN (default /usr/local/bin/go-librespot),
#                PLUM_SPOTIFY_CONFIG_DIR (config root; default /data/go-librespot).

set -u

INSTANCE_ID="${1:-}"
if [ -z "$INSTANCE_ID" ]; then
    echo "[spotify] ERROR: instance id required as first argument" >&2
    exit 1
fi

BIN="${PLUM_GOLIBRESPOT_BIN:-/usr/local/bin/go-librespot}"
CONFIG_ROOT="${PLUM_SPOTIFY_CONFIG_DIR:-/data/go-librespot}"
CONFIG_DIR="${CONFIG_ROOT}/${INSTANCE_ID}"

if [ ! -f "${CONFIG_DIR}/config.yml" ]; then
    echo "[spotify-${INSTANCE_ID}] ERROR: config not found: ${CONFIG_DIR}/config.yml" >&2
    exit 1
fi

# Wait up to ~30s for Avahi D-Bus so zeroconf registration doesn't race the daemon.
for i in $(seq 1 30); do
    if timeout 2 avahi-browse -a -t 2>/dev/null | grep -q "^[+=-]"; then
        echo "[spotify-${INSTANCE_ID}] Avahi ready" >&2
        break
    fi
    echo "[spotify-${INSTANCE_ID}] waiting for Avahi... ($i/30)" >&2
    sleep 1
done
sleep 2  # grace for Avahi registration readiness

echo "[spotify-${INSTANCE_ID}] starting: $BIN --config_dir $CONFIG_DIR" >&2
exec "$BIN" --config_dir "$CONFIG_DIR"
