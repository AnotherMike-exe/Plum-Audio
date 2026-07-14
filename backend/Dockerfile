# Plum-Audio backend — Debian-slim (glibc), multi-arch amd64/arm64.
# glibc (NOT Alpine/musl) makes PyAV / PortAudio / numpy wheels trivial. Do not switch to Alpine.
# Phase 1: AirPlay (shairport-sync) + in-process Sendspin server/player under supervisord.
# Other source services (spotifyd, gmrender, bluez-alsa) land in Phase 3. See docs/ARCHITECTURE.md.

FROM python:3.13-slim-bookworm AS base

ENV PUID=99 \
    PGID=100 \
    UMASK=002 \
    TZ=UTC \
    DEBUG=false \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1

# Runtime: PortAudio (player DAC out), shairport-sync (AirPlay, pulls libavahi-client to reach the
# host Avahi), ALSA utils for debugging (aplay -l), and supervisord. Avahi/D-Bus daemons run on the
# HOST (host networking + mounted sockets) — we do not run duplicate daemons in the container.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libportaudio2 \
        alsa-utils \
        shairport-sync \
        supervisor \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies before code (layer caching)
COPY backend/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

# Application code (scripts/, config/, supervisord/, entrypoint.sh)
COPY backend/ /app/
RUN chmod +x /app/entrypoint.sh

VOLUME ["/config", "/data", "/media"]
RUN mkdir -p /config /data /media

# EXPOSE: sendspin server 8927, player 8928, mesh/APIs 5001+, AirPlay 5000 (docs note 5050-5059
# for multi-instance). Host networking is used in practice; EXPOSE is documentation here.
EXPOSE 8927 8928 5000 5001

CMD ["/app/entrypoint.sh"]
