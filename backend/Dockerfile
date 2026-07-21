# Plum-Audio — one container per unit: Sendspin server/player, the source daemons, the two API
# hosts and the web GUI, all under supervisord. Debian-slim (glibc), multi-arch amd64/arm64.
# glibc (NOT Alpine/musl) makes PyAV / PortAudio / numpy wheels trivial. Do not switch to Alpine.
# Remaining sources (gmrender, bluez-alsa, Plexamp) land as their slices do. See docs/ARCHITECTURE.md.

# --- Frontend build: the React GUI nginx serves from /app/www ------------------------------------
FROM node:20-alpine AS webui
WORKDIR /ui
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
# No VITE_* host is baked in: the app calls same-origin /api/* (nginx proxies them), so one build
# serves every unit.
RUN npm run build

FROM python:3.13-slim-bookworm AS base

ENV PUID=99 \
    PGID=100 \
    UMASK=002 \
    TZ=UTC \
    DEBUG=false \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1

# Runtime: PortAudio (player DAC out), shairport-sync (AirPlay, pulls libavahi-client to reach the
# host Avahi), dbus-daemon (a PRIVATE session bus per AirPlay endpoint — shairport's MPRIS name is
# fixed, so instances would otherwise fight over it; see sources/airplay_manager.py), ALSA utils for
# debugging (aplay -l), nginx (per-unit GUI) and supervisord. The Avahi and SYSTEM D-Bus daemons run
# on the HOST (host networking + mounted sockets) — we do not run duplicate daemons here.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libportaudio2 \
        alsa-utils \
        shairport-sync \
        dbus \
        nginx \
        supervisor \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

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

# EXPOSE: web GUI 80, mesh API 5001, config API 5002, sendspin server 8927 / player 8928, AirPlay
# 5050-5059 + UDP blocks, Spotify zeroconf 5354-5363. Host networking is used in practice (mDNS
# needs L2); EXPOSE is documentation here.
EXPOSE 80 5001 5002 8927 8928 5050 5354

CMD ["/app/entrypoint.sh"]
