#!/usr/bin/env python3
"""
Plum-Audio — Spotify Connect multi-instance config generation.

Renders one spotifyd.conf per Spotify endpoint (from settings.json integrations.spotify.endpoints)
and, in the container, the supervisord program sections that run them. One endpoint → one spotifyd
process → one instance FIFO → one Sendspin source.

Ported from Plum-Snapcast's setup-spotify-multi-instance.sh + generate-spotify-supervisord-config.py,
collapsed into one importable module. DROPPED from the port: the per-instance fifo-keeper and
stream-lifecycle-manager processes — in Plum-Audio the in-process SendspinServer feeder owns the
FIFO read end and the source→group lifecycle, so neither is needed (see the porting map in CLAUDE.md).

The MPRIS name-race stagger is preserved: spotifyd instances start sequentially (priority 40, 70,
100, …) so instance 1 claims the base MPRIS name before instance 2 comes up (later instances then
get PID-suffixed names — see spotify_mpris.py).
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger("plum.spotify_config")

# Defaults are container paths (Binhex /app layout); every path is overridable — by argument or the
# PLUM_SPOTIFY_* env vars — so the Pi rig and tests can point them at a scratch dir.
DEFAULT_TEMPLATE = os.environ.get("PLUM_SPOTIFY_TEMPLATE", "/app/config/spotifyd.conf.template")
DEFAULT_CONFIG_DIR = os.environ.get("PLUM_SPOTIFY_CONFIG_DIR", "/app/config")
DEFAULT_SUPERVISOR_OUT = os.environ.get(
    "PLUM_SPOTIFY_SUPERVISOR_OUT", "/app/supervisord/conf.d/spotify-multi-instance.ini"
)
DEFAULT_SPOTIFYD_BIN = os.environ.get("PLUM_SPOTIFYD_BIN", "/usr/local/bin/spotifyd")
DEFAULT_START_SCRIPT = os.environ.get(
    "PLUM_SPOTIFY_START_SCRIPT", "/app/scripts/sources/start-spotifyd.sh"
)

MAX_ENDPOINTS = 10
DEFAULT_BITRATE = 320
PRIORITY_BASE = 40
PRIORITY_STEP = 30  # gap between instances so each claims its MPRIS name before the next starts


@dataclass(frozen=True)
class SpotifyInstance:
    """A single resolved Spotify Connect endpoint the server should bring up as a source."""

    instance_id: str
    device_name: str
    fifo_path: str
    zeroconf_port: int
    config_path: str

    @property
    def source_id(self) -> str:
        return f"spotify-{self.instance_id}"


def _device_id(device_name: str) -> str:
    """Deterministic 40-char Spotify device_id from the endpoint name (stable across restarts)."""
    return hashlib.sha256(device_name.encode("utf-8")).hexdigest()[:40]


def fifo_path_for(instance_id: str) -> str:
    return f"/tmp/spotify-{instance_id}-fifo"


def enabled_endpoints(settings: dict) -> list[dict]:
    """Enabled Spotify endpoints from a settings dict (endpoints-array shape), capped at MAX_ENDPOINTS."""
    spotify = settings.get("integrations", {}).get("spotify", {})
    endpoints = spotify.get("endpoints", []) or []
    return [e for e in endpoints[:MAX_ENDPOINTS] if e.get("enabled")]


def instances_from_settings(settings: dict, *, config_dir: str = DEFAULT_CONFIG_DIR) -> list[SpotifyInstance]:
    """Resolve enabled endpoints to SpotifyInstances WITHOUT writing any files.

    Used by the server to decide which Spotify sources to bring up. Config rendering + spotifyd
    launch are a separate concern (render_configs / supervisord / the Pi rig), so this stays pure.
    """
    instances: list[SpotifyInstance] = []
    for endpoint in enabled_endpoints(settings):
        instance_id = str(endpoint.get("id"))
        device_name = endpoint.get("deviceName", f"Plum Audio {instance_id}")
        instances.append(
            SpotifyInstance(
                instance_id=instance_id,
                device_name=device_name,
                fifo_path=fifo_path_for(instance_id),
                zeroconf_port=int(endpoint.get("zeroconfPort", 5354)),
                config_path=os.path.join(config_dir, f"spotifyd-{instance_id}.conf"),
            )
        )
    return instances


def render_configs(
    settings: dict,
    *,
    template_path: str = DEFAULT_TEMPLATE,
    config_dir: str = DEFAULT_CONFIG_DIR,
) -> list[SpotifyInstance]:
    """Write a spotifyd-<id>.conf for each enabled endpoint; return the instances to bring up."""
    spotify = settings.get("integrations", {}).get("spotify", {})
    bitrate = int(spotify.get("bitrate", DEFAULT_BITRATE))
    with open(template_path, encoding="utf-8") as f:
        template = f.read()

    instances: list[SpotifyInstance] = []
    os.makedirs(config_dir, exist_ok=True)
    for endpoint in enabled_endpoints(settings):
        instance_id = str(endpoint.get("id"))
        device_name = endpoint.get("deviceName", f"Plum Audio {instance_id}")
        zeroconf_port = int(endpoint.get("zeroconfPort", 5354))
        config_path = os.path.join(config_dir, f"spotifyd-{instance_id}.conf")

        rendered = (
            template.replace("SPOTIFY_NAME", device_name)
            .replace("SPOTIFY_DEVICE_ID", _device_id(device_name))
            .replace("SPOTIFY_ZEROCONF_PORT", str(zeroconf_port))
            .replace("SPOTIFY_BITRATE", str(bitrate))
            .replace("INSTANCE_ID", instance_id)
        )
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(rendered)

        instances.append(
            SpotifyInstance(
                instance_id=instance_id,
                device_name=device_name,
                fifo_path=fifo_path_for(instance_id),
                zeroconf_port=zeroconf_port,
                config_path=config_path,
            )
        )
        logger.info("rendered spotifyd config %s (name=%r port=%d)", config_path, device_name, zeroconf_port)

    return instances


_SUPERVISOR_PROGRAM = """
[program:spotifyd-{instance_id}]
command=/bin/bash {start_script} {instance_id}
directory=/app
priority={priority}
autostart=true
autorestart=true
startsecs=10
startretries=3
stopasgroup=true
killasgroup=true
stdout_logfile=/config/spotifyd-{instance_id}.log
stdout_logfile_maxbytes=50MB
stderr_logfile=/config/spotifyd-{instance_id}_err.log
stderr_logfile_maxbytes=10MB
"""


def generate_supervisord(
    instances: list[SpotifyInstance],
    *,
    output_path: str = DEFAULT_SUPERVISOR_OUT,
    start_script: str = DEFAULT_START_SCRIPT,
) -> str:
    """Write the supervisord include with one spotifyd program per instance. Returns the path."""
    lines = [
        "# Multi-instance Spotify Connect — generated by spotify_config.generate_supervisord().",
        f"# {len(instances)} endpoint(s). Do not edit by hand; rerun after changing endpoints.",
    ]
    for idx, inst in enumerate(instances):
        priority = PRIORITY_BASE + idx * PRIORITY_STEP
        lines.append(
            _SUPERVISOR_PROGRAM.format(
                instance_id=inst.instance_id, start_script=start_script, priority=priority
            )
        )
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    logger.info("wrote supervisord include %s (%d instance(s))", output_path, len(instances))
    return output_path
