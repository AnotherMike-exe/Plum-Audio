#!/usr/bin/env python3
"""
Plum-Audio — Spotify Connect multi-instance config generation (go-librespot).

Renders one go-librespot config.yml per Spotify endpoint (from settings.json
integrations.spotify.endpoints) and, in the container, the supervisord program sections that run
them. One endpoint → one go-librespot process → one instance FIFO → one Sendspin source, plus a
loopback HTTP+WebSocket control API per instance (see spotify_golibrespot.py).

We use go-librespot rather than spotifyd: spotifyd 0.4.x dropped standard MPRIS (its D-Bus interface
is now only TransferPlayback/volume, no metadata/transport), and it has no arm64 build with full
MPRIS. go-librespot ships a native arm64 binary and exposes richer metadata/transport over an
HTTP+WS API — the same "use what the daemon natively provides" approach the original project took
with spotifyd's then-current MPRIS. DROPPED vs the Plum-Snapcast port: the per-instance fifo-keeper
and stream-lifecycle-manager (the in-process feeder owns the FIFO + source lifecycle) AND all D-Bus
(go-librespot needs none).

Each instance gets its own config dir (go-librespot -config_dir), a unique zeroconf port, and a
unique loopback API port.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger("plum.spotify_config")

# Defaults are container paths (Binhex /app layout); every path is overridable — by argument or the
# PLUM_SPOTIFY_* env vars — so the Pi rig and tests can point them at a scratch dir.
DEFAULT_TEMPLATE = os.environ.get("PLUM_SPOTIFY_TEMPLATE", "/app/config/go-librespot.yml.template")
DEFAULT_CONFIG_ROOT = os.environ.get("PLUM_SPOTIFY_CONFIG_DIR", "/data/go-librespot")
DEFAULT_SUPERVISOR_OUT = os.environ.get(
    "PLUM_SPOTIFY_SUPERVISOR_OUT", "/app/supervisord/conf.d/spotify-multi-instance.ini"
)
DEFAULT_GOLIBRESPOT_BIN = os.environ.get("PLUM_GOLIBRESPOT_BIN", "/usr/local/bin/go-librespot")
DEFAULT_START_SCRIPT = os.environ.get(
    "PLUM_SPOTIFY_START_SCRIPT", "/app/scripts/sources/start-spotify.sh"
)

MAX_ENDPOINTS = 10
DEFAULT_BITRATE = 320
API_PORT_BASE = 3678  # go-librespot loopback control API; per-instance = base + (id - 1)
PRIORITY_BASE = 40
PRIORITY_STEP = 10


@dataclass(frozen=True)
class SpotifyInstance:
    """A single resolved Spotify Connect endpoint the server should bring up as a source."""

    instance_id: str
    device_name: str
    fifo_path: str
    zeroconf_port: int
    api_port: int
    config_dir: str

    @property
    def source_id(self) -> str:
        return f"spotify-{self.instance_id}"

    @property
    def api_base(self) -> str:
        return f"http://127.0.0.1:{self.api_port}"


def fifo_path_for(instance_id: str) -> str:
    return f"/tmp/spotify-{instance_id}-fifo"


def api_port_for(instance_id: str) -> int:
    try:
        return API_PORT_BASE + int(instance_id) - 1
    except (TypeError, ValueError):
        return API_PORT_BASE


def enabled_endpoints(settings: dict) -> list[dict]:
    """Enabled Spotify endpoints from a settings dict (endpoints-array shape), capped at MAX_ENDPOINTS."""
    spotify = settings.get("integrations", {}).get("spotify", {})
    endpoints = spotify.get("endpoints", []) or []
    return [e for e in endpoints[:MAX_ENDPOINTS] if e.get("enabled")]


def _instance(endpoint: dict, config_root: str) -> SpotifyInstance:
    instance_id = str(endpoint.get("id"))
    return SpotifyInstance(
        instance_id=instance_id,
        device_name=endpoint.get("deviceName", f"Plum Audio {instance_id}"),
        fifo_path=fifo_path_for(instance_id),
        zeroconf_port=int(endpoint.get("zeroconfPort", 5354)),
        api_port=api_port_for(instance_id),
        config_dir=os.path.join(config_root, instance_id),
    )


def instances_from_settings(settings: dict, *, config_root: str = DEFAULT_CONFIG_ROOT) -> list[SpotifyInstance]:
    """Resolve enabled endpoints to SpotifyInstances WITHOUT writing any files.

    Used by the server to decide which Spotify sources to bring up. Config rendering + go-librespot
    launch are a separate concern (render_configs / supervisord / the Pi rig), so this stays pure.
    """
    return [_instance(e, config_root) for e in enabled_endpoints(settings)]


def render_configs(
    settings: dict,
    *,
    template_path: str = DEFAULT_TEMPLATE,
    config_root: str = DEFAULT_CONFIG_ROOT,
) -> list[SpotifyInstance]:
    """Write a config.yml into each enabled endpoint's config dir; return the instances to bring up."""
    spotify = settings.get("integrations", {}).get("spotify", {})
    bitrate = int(spotify.get("bitrate", DEFAULT_BITRATE))
    with open(template_path, encoding="utf-8") as f:
        template = f.read()

    instances: list[SpotifyInstance] = []
    for endpoint in enabled_endpoints(settings):
        inst = _instance(endpoint, config_root)
        os.makedirs(inst.config_dir, exist_ok=True)

        rendered = (
            template.replace("SPOTIFY_NAME", inst.device_name)
            .replace("SPOTIFY_ZEROCONF_PORT", str(inst.zeroconf_port))
            .replace("SPOTIFY_API_PORT", str(inst.api_port))
            .replace("SPOTIFY_BITRATE", str(bitrate))
            .replace("INSTANCE_ID", inst.instance_id)
        )
        with open(os.path.join(inst.config_dir, "config.yml"), "w", encoding="utf-8") as f:
            f.write(rendered)

        instances.append(inst)
        logger.info(
            "rendered go-librespot config %s/config.yml (name=%r zc=%d api=%d)",
            inst.config_dir, inst.device_name, inst.zeroconf_port, inst.api_port,
        )

    return instances


_SUPERVISOR_PROGRAM = """
[program:spotify-{instance_id}]
command=/bin/bash {start_script} {instance_id}
directory=/app
priority={priority}
autostart=true
autorestart=true
startsecs=10
startretries=3
stopasgroup=true
killasgroup=true
stdout_logfile=/config/spotify-{instance_id}.log
stdout_logfile_maxbytes=50MB
stderr_logfile=/config/spotify-{instance_id}_err.log
stderr_logfile_maxbytes=10MB
"""


def generate_supervisord(
    instances: list[SpotifyInstance],
    *,
    output_path: str = DEFAULT_SUPERVISOR_OUT,
    start_script: str = DEFAULT_START_SCRIPT,
) -> str:
    """Write the supervisord include with one go-librespot program per instance. Returns the path."""
    lines = [
        "# Multi-instance Spotify Connect (go-librespot) — generated by spotify_config.generate_supervisord().",
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
