#!/usr/bin/env python3
"""
Plum-Audio — Spotify Connect multi-instance config generation (go-librespot).

Renders one go-librespot config.yml per Spotify endpoint (from settings.json
integrations.spotify.endpoints) and resolves them to SpotifyInstances. One endpoint → one
go-librespot process → one instance FIFO → one Sendspin source, plus a loopback HTTP+WebSocket
control API per instance (see spotify_golibrespot.py).

We use go-librespot rather than spotifyd: spotifyd 0.4.x dropped standard MPRIS (its D-Bus interface
is now only TransferPlayback/volume, no metadata/transport), and it has no arm64 build with full
MPRIS. go-librespot ships a native arm64 binary and exposes richer metadata/transport over an
HTTP+WS API — the same "use what the daemon natively provides" approach the original project took
with spotifyd's then-current MPRIS. DROPPED vs the Plum-Snapcast port: the per-instance fifo-keeper
and stream-lifecycle-manager (the in-process feeder owns the FIFO + source lifecycle), all D-Bus
(go-librespot needs none), AND the generated supervisord include — spotify_manager.py spawns and
supervises the daemons itself so endpoint edits apply live, identically on the rig and in the
container.

Each instance gets its own config dir (go-librespot -config_dir), a unique zeroconf port, and a
unique loopback API port.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from . import endpoint_config
from .config_render import escape_yaml_double_quoted, write_atomic

logger = logging.getLogger("plum.spotify_config")

# Defaults are container paths (Binhex /app layout); every path is overridable — by argument or the
# PLUM_SPOTIFY_* env vars — so the Pi rig and tests can point them at a scratch dir.
DEFAULT_TEMPLATE = os.environ.get("PLUM_SPOTIFY_TEMPLATE", "/app/config/go-librespot.yml.template")
DEFAULT_CONFIG_ROOT = os.environ.get("PLUM_SPOTIFY_CONFIG_DIR", "/data/go-librespot")
DEFAULT_GOLIBRESPOT_BIN = os.environ.get("PLUM_GOLIBRESPOT_BIN", "/usr/local/bin/go-librespot")
MAX_ENDPOINTS = 10
DEFAULT_BITRATE = 320
API_PORT_BASE = 3678  # go-librespot loopback control API; per-instance = base + (id - 1)


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
    # `or {}` matches airplay_config/bluetooth_config: a section present but null (which a raw
    # POST /api/settings can write) makes .get return None, and the next .get raises AttributeError.
    spotify = settings.get("integrations", {}).get("spotify", {}) or {}
    endpoints = spotify.get("endpoints", []) or []
    if not isinstance(endpoints, list):
        return []  # a raw settings write can put anything here
    return [e for e in endpoints[:MAX_ENDPOINTS] if isinstance(e, dict) and e.get("enabled")]


def _instance(endpoint: dict, config_root: str) -> SpotifyInstance:
    """Resolve one endpoint, or raise `endpoint_config.InvalidEndpoint`.

    The id names `config_dir` and the FIFO, and it lands unescaped in the YAML template with the
    zeroconf port. Every sink below uses the CHECKED values, never `endpoint[...]` again.
    """
    instance_id = endpoint_config.checked_id(endpoint.get("id"))
    return SpotifyInstance(
        instance_id=instance_id,
        device_name=endpoint_config.checked_name(endpoint.get("deviceName"), f"Plum Audio {instance_id}"),
        fifo_path=fifo_path_for(instance_id),
        zeroconf_port=endpoint_config.checked_port(endpoint.get("zeroconfPort") or 5354, "zeroconfPort"),
        api_port=api_port_for(instance_id),
        config_dir=os.path.join(config_root, instance_id),
    )


def instances_from_settings(settings: dict, *, config_root: str = DEFAULT_CONFIG_ROOT) -> list[SpotifyInstance]:
    """Resolve enabled endpoints to SpotifyInstances WITHOUT writing any files.

    Used by spotify_manager to decide which sources to bring up; config rendering and the daemon
    launch are separate concerns, so this stays pure. An endpoint that fails `endpoint_config` is
    skipped with a warning; the others still start.
    """
    return endpoint_config.resolve("spotify", enabled_endpoints(settings), lambda e: _instance(e, config_root))


def render_configs(
    settings: dict,
    *,
    template_path: str = DEFAULT_TEMPLATE,
    config_root: str = DEFAULT_CONFIG_ROOT,
) -> list[SpotifyInstance]:
    """Write a config.yml into each enabled endpoint's config dir; return the instances to bring up."""
    spotify = settings.get("integrations", {}).get("spotify", {}) or {}
    # The bitrate lands unescaped in the template. Anything that is not a real bitrate becomes the
    # default: one bad section value must not stop every endpoint.
    bitrate = spotify.get("bitrate", DEFAULT_BITRATE)
    if isinstance(bitrate, bool) or bitrate not in endpoint_config.SPOTIFY_BITRATES:
        logger.warning("spotify bitrate %r is not valid; using %d", bitrate, DEFAULT_BITRATE)
        bitrate = DEFAULT_BITRATE
    with open(template_path, encoding="utf-8") as f:
        template = f.read()

    instances: list[SpotifyInstance] = []
    for inst in instances_from_settings(settings, config_root=config_root):
        os.makedirs(inst.config_dir, exist_ok=True)

        # Escaped: this lands inside a double-quoted YAML scalar, where a raw quote or newline
        # lets the name become configuration (e.g. redirecting audio_output_pipe).
        rendered = (
            template.replace("SPOTIFY_NAME", escape_yaml_double_quoted(inst.device_name))
            .replace("SPOTIFY_ZEROCONF_PORT", str(inst.zeroconf_port))
            .replace("SPOTIFY_API_PORT", str(inst.api_port))
            .replace("SPOTIFY_BITRATE", str(bitrate))
            .replace("INSTANCE_ID", inst.instance_id)
        )
        write_atomic(os.path.join(inst.config_dir, "config.yml"), rendered)

        instances.append(inst)
        logger.info(
            "rendered go-librespot config %s/config.yml (name=%r zc=%d api=%d)",
            inst.config_dir,
            inst.device_name,
            inst.zeroconf_port,
            inst.api_port,
        )

    return instances
