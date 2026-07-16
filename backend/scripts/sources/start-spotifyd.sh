#!/bin/bash
# Plum-Audio spotifyd launcher (one per Spotify Connect instance).
# Waits for Avahi (zeroconf/mDNS) to be ready so Spotify Connect discovery advertises reliably,
# then execs spotifyd with the instance's rendered config. Called by supervisord as:
#   start-spotifyd.sh <instance_id>
# Env overrides: PLUM_SPOTIFYD_BIN (default /usr/local/bin/spotifyd),
#                PLUM_SPOTIFY_CONFIG_DIR (default /app/config).

set -u

INSTANCE_ID="${1:-}"
if [ -z "$INSTANCE_ID" ]; then
    echo "[spotifyd] ERROR: instance id required as first argument" >&2
    exit 1
fi

SPOTIFYD_BIN="${PLUM_SPOTIFYD_BIN:-/usr/local/bin/spotifyd}"
CONFIG_DIR="${PLUM_SPOTIFY_CONFIG_DIR:-/app/config}"
CONFIG_PATH="${CONFIG_DIR}/spotifyd-${INSTANCE_ID}.conf"

if [ ! -f "$CONFIG_PATH" ]; then
    echo "[spotifyd-${INSTANCE_ID}] ERROR: config not found: $CONFIG_PATH" >&2
    exit 1
fi

# Wait up to ~30s for Avahi D-Bus to answer, so zeroconf registration doesn't race the daemon.
for i in $(seq 1 30); do
    if timeout 2 avahi-browse -a -t 2>/dev/null | grep -q "^[+=-]"; then
        echo "[spotifyd-${INSTANCE_ID}] Avahi ready" >&2
        break
    fi
    echo "[spotifyd-${INSTANCE_ID}] waiting for Avahi... ($i/30)" >&2
    sleep 1
done
sleep 2  # grace for Avahi registration readiness

echo "[spotifyd-${INSTANCE_ID}] starting: $SPOTIFYD_BIN --config-path $CONFIG_PATH" >&2
exec "$SPOTIFYD_BIN" --no-daemon --config-path "$CONFIG_PATH"
