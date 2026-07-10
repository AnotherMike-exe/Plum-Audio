#!/bin/sh
# Plum-Audio container entrypoint. /config is a runtime volume, so create the log dir here
# (a build-time mkdir would be masked by the mount), then hand off to supervisord.
set -e

mkdir -p /config/logs /tmp/airplay-covers
umask "${UMASK:-002}"

exec supervisord -c /app/supervisord/supervisord.conf
