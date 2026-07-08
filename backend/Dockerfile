# Plum-Audio backend — Debian-slim (glibc), multi-arch amd64/arm64.
# glibc (NOT Alpine/musl) makes PyAV / PortAudio / numpy wheels trivial. Do not switch to Alpine.
# NOTE: skeleton — audio source services (shairport-sync, spotifyd, gmrender, bluez-alsa),
# Avahi/D-Bus, and supervisord wiring are added in Phase 1. See docs/ARCHITECTURE.md.

FROM python:3.13-slim-bookworm AS base

ENV PUID=99 \
    PGID=100 \
    UMASK=002 \
    TZ=UTC \
    DEBUG=false \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1

# Runtime libs: PortAudio (player output), and (Phase 1) the source services + avahi/dbus.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libportaudio2 \
        supervisor \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies before code (layer caching)
COPY backend/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

# Application code
COPY backend/ /app/

VOLUME ["/config", "/data", "/media"]
RUN mkdir -p /config /data /media

# EXPOSE: sendspin server 8927, player 8928, mesh/APIs 5001+, frontend proxied separately
EXPOSE 8927 8928 5001

# Phase 1: replace with supervisord entrypoint managing all services
CMD ["python", "/app/scripts/sendspin_server.py"]
