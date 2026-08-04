# Plum-Audio — one container per unit: Sendspin server/player, the source daemons, the two API
# hosts and the web GUI, all under supervisord. Debian-slim (glibc), multi-arch amd64/arm64.
# glibc (NOT Alpine/musl) makes PyAV / PortAudio / numpy wheels trivial. Do not switch to Alpine.
# Remaining sources (gmrender, Plexamp) land as their slices do. See docs/ARCHITECTURE.md.
#
# BASE = TRIXIE, DELIBERATELY. The R&D units are Debian 13, and two source integrations are pinned
# to exactly what trixie ships: bluez-alsa 4.3.1-3 still installs its daemon as `bluealsa` (upstream
# renamed it `bluealsad` in 4.0 — see sources/bluetooth_config.py), and shairport-sync 4.3.7 is the
# build multi-endpoint AirPlay's MPRIS-on-a-private-session-bus behaviour was verified against.
# Bookworm would hand us shairport 4.1 and a different bluez-alsa; keep image and host on one
# Debian release.

# --- Frontend build: the React GUI nginx serves from /app/www ------------------------------------
FROM node:22-alpine AS webui
WORKDIR /ui
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
# No VITE_* host is baked in: the app calls same-origin /api/* (nginx proxies them), so one build
# serves every unit.
RUN npm run build

FROM python:3.13-slim-trixie AS base

ENV PUID=99 \
    PGID=100 \
    UMASK=002 \
    TZ=UTC \
    DEBUG=false \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    PLUM_PROC_ASOUND=/host/asound

# Runtime, by consumer:
#   libportaudio2      sendspin_player -> onboard DAC
#   alsa-utils         arecord (the Bluetooth capture path) + aplay -l for debugging
#   shairport-sync     AirPlay; pulls libavahi-client so it advertises via the HOST Avahi
#   bluez-alsa-utils   the bluealsa daemon AND the ALSA `bluealsa:` PCM plugin arecord reads from
#   bluez-obexd        AVRCP cover art — one private-bus obexd per Bluetooth endpoint
#   dbus               dbus-daemon: a PRIVATE session bus per AirPlay/Bluetooth endpoint, because
#                      shairport's MPRIS name and obexd's org.bluez.obex are both fixed
#   iproute2           the entrypoint derives this unit's advertised player URL from the LAN address
#   nginx/supervisor/curl   per-unit GUI, the process tree, the healthcheck
# The Avahi and SYSTEM D-Bus daemons run on the HOST (host networking + mounted sockets), as does
# bluetoothd — the host owns the radio and the AVCTP channel. We run no duplicates here.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libportaudio2 \
        alsa-utils \
        shairport-sync \
        bluez-alsa-utils \
        bluez-obexd \
        dbus \
        iproute2 \
        nginx \
        supervisor \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Fail the BUILD, not the rig, if a distro bump ever drops one of these. Each failure is invisible
# at runtime: no MPRIS interface = AirPlay transport controls that silently do nothing; a renamed
# bluealsa or a moved obexd = a Bluetooth source that never appears, or cover art that never loads,
# with nothing in the log either way (see docs/ARCHITECTURE.md §8 and the bluetooth memory notes).
RUN shairport-sync -V | grep -q -- '-mpris' \
    && test -x /usr/bin/bluealsa \
    && test -x /usr/libexec/bluetooth/obexd

# go-librespot (Spotify Connect): one process per endpoint, spawned by sources/spotify_manager.py —
# deliberately NOT a supervisord program, so endpoint edits apply live. Native binary, no runtime.
ARG GO_LIBRESPOT_VERSION=0.7.4
RUN set -eux; \
    case "$(dpkg --print-architecture)" in \
        arm64) GLR_ARCH=arm64 ;; \
        amd64) GLR_ARCH=x86_64 ;; \
        *) echo "unsupported arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/glr.tar.gz \
        "https://github.com/devgianlu/go-librespot/releases/download/v${GO_LIBRESPOT_VERSION}/go-librespot_linux_${GLR_ARCH}.tar.gz"; \
    tar -xzf /tmp/glr.tar.gz -C /usr/local/bin go-librespot; \
    rm /tmp/glr.tar.gz; \
    chmod +x /usr/local/bin/go-librespot

WORKDIR /app

# Dependencies before code (layer caching)
COPY backend/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

# Application code (scripts/, config/, nginx/, supervisord/, entrypoint.sh)
COPY backend/ /app/
RUN chmod +x /app/entrypoint.sh

# Web GUI: built assets + the nginx site that serves them and proxies :5001/:5002.
COPY --from=webui /ui/dist /app/www
RUN rm -f /etc/nginx/sites-enabled/default \
    && cp /app/nginx/plum-audio.conf /etc/nginx/conf.d/plum-audio.conf

VOLUME ["/config", "/data", "/media"]
RUN mkdir -p /config /data /media

# EXPOSE: web GUI 80, mesh API 5001, config API 5002, sendspin server 8927 / player 8928 / mesh
# beacon 8929, AirPlay 5050-5059 + UDP blocks, Spotify zeroconf 5354-5363. Host networking is used
# in practice (mDNS needs L2); EXPOSE is documentation here.
EXPOSE 80 5001 5002 8927 8928 8929 5050 5354

# The config API is the cheapest liveness proof for the whole tree: it is a separate process from
# the audio loop, so a green healthcheck means supervisord itself is up and serving — the audio
# side's own health is the mesh API's business (/api/mesh/view), not Docker's.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PLUM_CONFIG_API_PORT:-5002}/api/settings" >/dev/null || exit 1

CMD ["/app/entrypoint.sh"]
