#!/usr/bin/env python3
"""
Plum-Audio — Spotify Connect supervisor/reconciler (go-librespot).

Owns BOTH halves of a Spotify endpoint's lifecycle from inside the audio event loop:
  * the Sendspin source (group + FIFO feeder + metadata/transport monitor), and
  * the go-librespot daemon process that feeds it.

It polls settings.json and reconciles the running set against the enabled endpoints, so the GUI's
add/rename/enable/disable/remove take effect live — no server restart, no supervisord round-trip.
That single owner is deliberate: the integrations API runs in a *separate* Flask process and can't
reach the audio loop, and the Pi rig has no supervisord at all. Reconciling from the persisted
settings file makes the rig and the container behave identically, and makes the API's job pure
persistence.

Reconcile rules per cycle:
  - render every enabled endpoint's config.yml (idempotent; picks up name/port/bitrate edits)
  - desired - running  → start the Sendspin source FIRST (its feeder creates the FIFO), then spawn
    go-librespot (its `pipe` backend opens the FIFO for write at startup)
  - running - desired   → kill go-librespot, then tear the Sendspin source down
  - signature change (name / zeroconf port / bitrate) → respawn go-librespot only; the source, its
    group and its loopback API port are keyed on the stable endpoint id and survive the edit
  - a go-librespot that exited on its own → respawn it after a short backoff (autorestart)
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import logging
import os
import signal
import subprocess
import time

from sources import spotify_config
from sources.spotify_config import SpotifyInstance

logger = logging.getLogger("plum.spotify_manager")

POLL_INTERVAL = 3.0  # seconds between settings reconciles
RESPAWN_BACKOFF = 5.0  # seconds before restarting a go-librespot that exited on its own
STOP_GRACE = 5.0  # seconds to wait for SIGTERM before SIGKILL


class _Running:
    """A live endpoint: its resolved instance, signature, and go-librespot process."""

    __slots__ = ("instance", "signature", "proc", "log", "next_spawn_at")

    def __init__(self, instance: SpotifyInstance, signature: tuple) -> None:
        self.instance = instance
        self.signature = signature
        self.proc: asyncio.subprocess.Process | None = None
        self.log = None  # open file object for the daemon's stderr
        self.next_spawn_at = 0.0  # monotonic deadline gating (re)spawn


class SpotifyManager:
    """Reconciles Spotify sources + go-librespot processes from settings.json, live."""

    def __init__(
        self,
        server,  # PlumSendspinServer — start_spotify_source / stop_spotify_source
        *,
        settings_file: str | None = None,
        template_path: str = spotify_config.DEFAULT_TEMPLATE,
        config_root: str = spotify_config.DEFAULT_CONFIG_ROOT,
        binary: str = spotify_config.DEFAULT_GOLIBRESPOT_BIN,
        poll_interval: float = POLL_INTERVAL,
    ) -> None:
        self.server = server
        self.settings_file = settings_file or os.environ.get("PLUM_SETTINGS_FILE", "/data/settings.json")
        self.template_path = template_path
        self.config_root = config_root
        self.binary = binary
        self.poll_interval = poll_interval
        self._running: dict[str, _Running] = {}  # instance_id -> _Running
        self._last_desired: dict[str, tuple] = {}  # instance_id -> signature, for change detection
        self._task: asyncio.Task | None = None
        self._stop_evt = asyncio.Event()

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._task is None:
            self._kill_stale_daemons()
            self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        self._stop_evt.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        for instance_id in list(self._running):
            await self._stop_instance(instance_id)

    async def _run(self) -> None:
        while not self._stop_evt.is_set():
            try:
                await self._reconcile()
            except Exception:  # noqa: BLE001 - a bad cycle must never kill the loop
                logger.exception("spotify reconcile failed; retrying next cycle")
            await asyncio.sleep(self.poll_interval)

    # -- reconcile -----------------------------------------------------------

    def _read_settings(self) -> dict | None:
        """Parsed settings, or None if unreadable/torn — the caller skips that cycle."""
        try:
            with open(self.settings_file, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    @staticmethod
    def _signature(endpoint: dict, bitrate: int) -> tuple:
        """What forces a go-librespot respawn. NOT the id (that keys the source, which survives)."""
        return (endpoint.get("deviceName"), int(endpoint.get("zeroconfPort", 5354)), bitrate)

    async def _reconcile(self) -> None:
        settings = self._read_settings()
        if settings is None:
            return
        spotify = settings.get("integrations", {}).get("spotify", {}) or {}
        bitrate = int(spotify.get("bitrate", spotify_config.DEFAULT_BITRATE))
        endpoints = spotify_config.enabled_endpoints(settings)
        desired = {
            str(e.get("id")): (self._signature(e, bitrate), inst)
            for e, inst in zip(
                endpoints,
                spotify_config.instances_from_settings(settings, config_root=self.config_root),
            )
        }

        for instance_id in [i for i in self._running if i not in desired]:
            await self._stop_instance(instance_id)

        signatures = {i: sig for i, (sig, _) in desired.items()}
        if signatures != self._last_desired:
            # Only on a real change: rendering is idempotent but chatty, and this is the one place
            # configs are written, so an edit always lands on disk before the respawn below.
            self._last_desired = signatures
            if desired:
                with contextlib.suppress(OSError):
                    spotify_config.render_configs(
                        settings, template_path=self.template_path, config_root=self.config_root
                    )

        for instance_id, (signature, instance) in desired.items():
            running = self._running.get(instance_id)
            if running is None:
                await self._start_instance(instance, signature)
            elif running.signature != signature:
                logger.info("[spotify-%s] config changed; respooling daemon", instance_id)
                running.instance = instance
                running.signature = signature
                await self._kill_daemon(running)
                await self._spawn_daemon(running)
            elif running.proc is not None and running.proc.returncode is not None:
                await self._reap_and_respawn(running)

    async def _start_instance(self, instance: SpotifyInstance, signature: tuple) -> None:
        """Bring up the Sendspin source, then the daemon that feeds it (order matters: FIFO first)."""
        running = _Running(instance, signature)
        self._running[instance.instance_id] = running
        await self.server.start_spotify_source(instance)
        self._ensure_fifo(instance.fifo_path)
        await self._spawn_daemon(running)
        logger.info("[%s] endpoint up (name=%r)", instance.source_id, instance.device_name)

    async def _stop_instance(self, instance_id: str) -> None:
        running = self._running.pop(instance_id, None)
        if running is None:
            return
        await self._kill_daemon(running)
        await self.server.stop_spotify_source(running.instance.source_id)
        logger.info("[%s] endpoint down", running.instance.source_id)

    # -- go-librespot process ------------------------------------------------

    @staticmethod
    def _ensure_fifo(path: str) -> None:
        """The feeder creates this too; do it here as well so the daemon never races an absent FIFO."""
        try:
            os.mkfifo(path, 0o666)
        except OSError as e:
            if e.errno != errno.EEXIST:
                logger.warning("mkfifo %s failed: %s", path, e)

    async def _spawn_daemon(self, running: _Running) -> None:
        inst = running.instance
        if time.monotonic() < running.next_spawn_at:
            return
        config_yml = os.path.join(inst.config_dir, "config.yml")
        if not os.path.exists(config_yml):
            logger.warning("[%s] no config at %s; not starting daemon", inst.source_id, config_yml)
            return
        try:
            running.log = open(os.path.join(inst.config_dir, "go-librespot.log"), "ab", buffering=0)
            running.proc = await asyncio.create_subprocess_exec(
                self.binary, "--config_dir", inst.config_dir,
                stdout=running.log, stderr=running.log, stdin=subprocess.DEVNULL,
                start_new_session=True,  # own process group: kill it without touching the server
            )
        except OSError as e:
            # No binary on this host (dev laptop / partial rig): the source still exists, it just
            # has nothing feeding it. Back off so we don't spin on every cycle.
            logger.warning("[%s] go-librespot launch failed: %s", inst.source_id, e)
            self._close_log(running)
            running.proc = None
            running.next_spawn_at = time.monotonic() + RESPAWN_BACKOFF
            return
        logger.info("[%s] go-librespot pid=%d (%s)", inst.source_id, running.proc.pid, inst.config_dir)

    async def _reap_and_respawn(self, running: _Running) -> None:
        code = running.proc.returncode if running.proc else None
        logger.warning("[%s] go-librespot exited (rc=%s); restarting", running.instance.source_id, code)
        self._close_log(running)
        running.proc = None
        running.next_spawn_at = time.monotonic() + RESPAWN_BACKOFF
        await self._spawn_daemon(running)

    async def _kill_daemon(self, running: _Running) -> None:
        proc = running.proc
        running.proc = None
        running.next_spawn_at = 0.0
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=STOP_GRACE)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                with contextlib.suppress(Exception):
                    await proc.wait()
        self._close_log(running)

    @staticmethod
    def _close_log(running: _Running) -> None:
        if running.log is not None:
            with contextlib.suppress(Exception):
                running.log.close()
            running.log = None

    def _kill_stale_daemons(self) -> None:
        """Kill go-librespot processes left behind by a previous server run.

        go-librespot holds a lock per config_dir, so a survivor from a crashed run would block the
        fresh instance for that endpoint. Matching on our config root keeps unrelated instances safe.
        """
        try:
            subprocess.run(
                ["pkill", "-f", f"go-librespot.*{self.config_root}"],
                capture_output=True, timeout=10, check=False,
            )
        except (OSError, subprocess.SubprocessError) as e:
            logger.debug("stale go-librespot sweep skipped: %s", e)
