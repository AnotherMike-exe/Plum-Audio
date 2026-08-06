#!/usr/bin/env python3
"""
Plum-Audio — Spotify Connect endpoints: the reconciling manager.

One go-librespot process per enabled endpoint, each feeding its own Sendspin source. All of the
polling/reconcile/process-supervision machinery lives in SourceManagerBase; this file is just the
Spotify-specific parts. See source_manager.py for the why.

Respool triggers (the signature): device name, zeroconf port, bitrate. NOT the endpoint id — that
keys the Sendspin source and its loopback API port, both of which survive an edit.
"""

from __future__ import annotations

import logging
import os

from sources import spotify_config
from sources.source_manager import DaemonSpec, SourceManagerBase

logger = logging.getLogger("plum.spotify_manager")


class SpotifyManager(SourceManagerBase):
    def __init__(
        self,
        server,
        *,
        settings_file: str | None = None,
        template_path: str = spotify_config.DEFAULT_TEMPLATE,
        config_root: str = spotify_config.DEFAULT_CONFIG_ROOT,
        binary: str = spotify_config.DEFAULT_GOLIBRESPOT_BIN,
        **kwargs,
    ) -> None:
        super().__init__(server, name="spotify", settings_file=settings_file, **kwargs)
        self.template_path = template_path
        self.config_root = config_root
        self.binary = binary
        # go-librespot holds a lock per config_dir, so a survivor of a crashed run would block the
        # fresh instance for that endpoint. Scoped to our config root: unrelated instances are safe.
        self.stale_pattern = f"go-librespot.*{config_root}"

    def desired(self, settings: dict) -> dict[str, tuple[tuple, object]]:
        spotify = settings.get("integrations", {}).get("spotify", {}) or {}
        bitrate = int(spotify.get("bitrate", spotify_config.DEFAULT_BITRATE))
        endpoints = spotify_config.enabled_endpoints(settings)
        instances = spotify_config.instances_from_settings(settings, config_root=self.config_root)
        return {
            str(e.get("id")): ((e.get("deviceName"), int(e.get("zeroconfPort", 5354)), bitrate), inst)
            # strict: instances_from_settings is one _instance per enabled_endpoints entry, from the
            # same settings dict — a length mismatch means that invariant broke and must not be silent.
            for e, inst in zip(endpoints, instances, strict=True)
        }

    def render(self, settings: dict) -> None:
        spotify_config.render_configs(settings, template_path=self.template_path, config_root=self.config_root)

    def fifos(self, instance) -> list[str]:
        return [instance.fifo_path]

    def daemons(self, instance) -> list[DaemonSpec]:
        config_yml = os.path.join(instance.config_dir, "config.yml")
        if not os.path.exists(config_yml):
            logger.warning("[%s] no config at %s; not starting daemon", instance.source_id, config_yml)
            return []
        return [
            DaemonSpec(
                argv=[self.binary, "--config_dir", instance.config_dir],
                log_path=os.path.join(instance.config_dir, "go-librespot.log"),
            )
        ]

    async def start_source(self, instance) -> None:
        await self.server.start_spotify_source(instance)

    async def stop_source(self, instance) -> None:
        await self.server.stop_spotify_source(instance.source_id)
