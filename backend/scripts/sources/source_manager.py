#!/usr/bin/env python3
"""
Plum-Audio — reconciling source manager (base class for multi-instance sources).

One owner per source family. It polls settings.json from inside the audio event loop and reconciles
what is RUNNING against the endpoints the user has ENABLED, owning both halves of every endpoint:
its Sendspin source (group + FIFO feeder + metadata/transport monitor) and the daemon process(es)
that feed it. GUI edits therefore apply live, in seconds — no server restart, no supervisord.

Why one owner (see docs/ARCHITECTURE.md §8): the integrations API runs in a separate Flask process
and cannot reach the audio loop, and the dev rig has no supervisord at all. Reconciling from the
persisted settings file is the only arrangement that behaves identically on the rig and in the
container, and it keeps the API to pure persistence.

Subclasses supply the source-specific parts (see spotify_manager.py / airplay_manager.py):
  desired(settings)      -> {instance_id: (signature, instance)} for enabled endpoints
  render(settings)       -> write each enabled endpoint's daemon config (idempotent)
  daemons(instance)      -> the DaemonSpecs to run for one endpoint, in start order
  start_source(instance) -> bring up the Sendspin source (called BEFORE the daemons)
  stop_source(instance)  -> tear it down (called AFTER the daemons are killed)
  update_source(instance)-> reflect an edited endpoint on the LIVE source (default: rename it)

The `signature` is whatever forces a daemon respool (name, ports, bitrate…) — never the endpoint id,
which keys the Sendspin source and must survive an edit.
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
from dataclasses import dataclass, field

logger = logging.getLogger("plum.source_manager")

POLL_INTERVAL = 3.0  # seconds between settings reconciles
RESPAWN_BACKOFF = 5.0  # seconds before restarting a daemon set that exited on its own
STOP_GRACE = 5.0  # seconds to wait for SIGTERM before SIGKILL


@dataclass(frozen=True)
class DaemonSpec:
    """One process to run for an endpoint. Started in list order."""

    argv: list[str]
    log_path: str
    env: dict[str, str] = field(default_factory=dict)  # merged over os.environ
    settle_s: float = 0.0  # pause after spawn before starting the next daemon (e.g. a bus socket)


class _Running:
    """A live endpoint: its resolved instance, signature, and daemon processes."""

    __slots__ = ("instance", "signature", "procs", "logs", "next_spawn_at")

    def __init__(self, instance, signature: tuple) -> None:
        self.instance = instance
        self.signature = signature
        self.procs: list[asyncio.subprocess.Process] = []
        self.logs: list = []  # open file objects for daemon stdout/stderr
        self.next_spawn_at = 0.0  # monotonic deadline gating (re)spawn


class SourceManagerBase:
    """Reconciles Sendspin sources + their daemons from settings.json, live."""

    #: pkill pattern(s) matching this family's daemons, used to sweep survivors of a previous run.
    #: A single string, or a list when a family spawns more than one kind of daemon (e.g. AirPlay
    #: runs shairport-sync AND a private dbus-daemon — both must be swept or the daemon pile up).
    stale_pattern: str | list[str] | None = None

    def __init__(
        self,
        server,  # PlumSendspinServer
        *,
        name: str,
        settings_file: str | None = None,
        poll_interval: float = POLL_INTERVAL,
    ) -> None:
        self.server = server
        self.name = name
        self.settings_file = settings_file or os.environ.get("PLUM_SETTINGS_FILE", "/data/settings.json")
        self.poll_interval = poll_interval
        self._running: dict[str, _Running] = {}  # instance_id -> _Running
        self._last_desired: dict[str, tuple] = {}  # instance_id -> signature, for change detection
        self._task: asyncio.Task | None = None
        self._stop_evt = asyncio.Event()

    # -- subclass hooks ------------------------------------------------------

    def desired(self, settings: dict) -> dict[str, tuple[tuple, object]]:
        raise NotImplementedError

    def render(self, settings: dict) -> None:
        raise NotImplementedError

    def daemons(self, instance) -> list[DaemonSpec]:
        raise NotImplementedError

    async def start_source(self, instance) -> None:
        raise NotImplementedError

    async def stop_source(self, instance) -> None:
        raise NotImplementedError

    async def update_source(self, instance) -> None:
        """Apply an endpoint edit to the running source. The source itself survives an edit (it is
        keyed on the endpoint id), so only its GUI label needs to follow the new device name."""
        self.server.set_source_name(instance.source_id, getattr(instance, "device_name", ""))

    def fifos(self, instance) -> list[str]:
        """FIFOs to pre-create so a daemon never races an absent pipe. Feeders create theirs too."""
        return []

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
                logger.exception("[%s] reconcile failed; retrying next cycle", self.name)
            await asyncio.sleep(self.poll_interval)

    # -- reconcile -----------------------------------------------------------

    def _read_settings(self) -> dict | None:
        """Parsed settings, or None if unreadable/torn — the caller skips that cycle."""
        try:
            with open(self.settings_file, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    async def _reconcile(self) -> None:
        settings = self._read_settings()
        if settings is None:
            return
        desired = self.desired(settings)

        for instance_id in [i for i in self._running if i not in desired]:
            await self._stop_instance(instance_id)

        signatures = {i: sig for i, (sig, _) in desired.items()}
        if signatures != self._last_desired:
            # Only on a real change: rendering is idempotent but chatty, and this is the one place
            # configs are written, so an edit always lands on disk before the respool below.
            self._last_desired = signatures
            if desired:
                with contextlib.suppress(OSError):
                    self.render(settings)

        for instance_id, (signature, instance) in desired.items():
            try:
                await self._reconcile_one(instance_id, signature, instance)
            except Exception:  # noqa: BLE001 - one bad endpoint must not stall the others
                logger.exception("[%s:%s] endpoint reconcile failed", self.name, instance_id)

    async def _reconcile_one(self, instance_id: str, signature: tuple, instance) -> None:
        running = self._running.get(instance_id)
        if running is None:
            await self._start_instance(instance, signature)
        elif running.signature != signature:
            logger.info("[%s:%s] config changed; respooling daemons", self.name, instance_id)
            running.instance = instance
            running.signature = signature
            await self.update_source(instance)
            await self._kill_daemons(running)
            await self._spawn_daemons(running)
        elif instance.source_id not in self.server.sources:
            # The DAEMONS are healthy but the Sendspin source is gone — POST /api/mesh/source/stop
            # pops the handle and stops the feeder without telling us. The old reconcile only ever
            # looked at running.procs, so it saw a healthy set and did nothing: the daemon kept
            # writing into a FIFO with no reader and the endpoint was dead until the container
            # restarted, silently. Rebuild the source and re-point the daemons at the fresh FIFO.
            logger.warning("[%s] Sendspin source vanished; rebuilding", instance.source_id)
            await self.start_source(instance)
            for path in self.fifos(instance):
                self._ensure_fifo(path)
            await self._kill_daemons(running)
            await self._spawn_daemons(running)
        elif not running.procs or any(p.returncode is not None for p in running.procs):
            # No procs at all counts as needing a respawn: a failed launch (binary not
            # installed yet, config not written yet) leaves the set empty, and without this the
            # endpoint would sit dead forever with nothing to reap.
            await self._reap_and_respawn(running)

    async def _start_instance(self, instance, signature: tuple) -> None:
        """Bring up the Sendspin source, then the daemons that feed it (order matters: FIFO first)."""
        running = _Running(instance, signature)
        await self.start_source(instance)
        # Registered only AFTER start_source returns. Registering first meant a start_source that
        # raised — caught by _reconcile — left the endpoint in _running with procs == [], which
        # every later cycle then routed to _reap_and_respawn. That retries DAEMONS, never the
        # Sendspin source, so the endpoint stayed permanently half-up: a daemon feeding a FIFO
        # nothing reads, logged at DEBUG. Leaving it unregistered lets the next cycle retry properly.
        self._running[instance.instance_id] = running
        for path in self.fifos(instance):
            self._ensure_fifo(path)
        await self._spawn_daemons(running)
        logger.info("[%s] endpoint up (%s)", instance.source_id, getattr(instance, "device_name", ""))

    async def _stop_instance(self, instance_id: str) -> None:
        running = self._running.pop(instance_id, None)
        if running is None:
            return
        await self._kill_daemons(running)
        await self.stop_source(running.instance)
        logger.info("[%s] endpoint down", running.instance.source_id)

    # -- daemon processes ----------------------------------------------------

    @staticmethod
    def _ensure_fifo(path: str) -> None:
        try:
            os.mkfifo(path, 0o666)
        except OSError as e:
            if e.errno != errno.EEXIST:
                logger.warning("mkfifo %s failed: %s", path, e)

    async def _spawn_daemons(self, running: _Running) -> None:
        if time.monotonic() < running.next_spawn_at:
            return
        inst = running.instance
        for spec in self.daemons(inst):
            try:
                log = open(spec.log_path, "ab", buffering=0)  # noqa: SIM115 - closed in _close_logs
                proc = await asyncio.create_subprocess_exec(
                    *spec.argv,
                    stdout=log,
                    stderr=log,
                    stdin=subprocess.DEVNULL,
                    env={**os.environ, **spec.env},
                    start_new_session=True,  # own process group: kill it without touching the server
                )
            except OSError as e:
                # Binary missing (dev laptop / partial rig): the source still exists, it just has
                # nothing feeding it. Back off so we don't spin, and drop any half-started set.
                logger.warning("[%s] %s launch failed: %s", inst.source_id, spec.argv[0], e)
                await self._kill_daemons(running)
                running.next_spawn_at = time.monotonic() + RESPAWN_BACKOFF
                return
            running.procs.append(proc)
            running.logs.append(log)
            logger.info("[%s] %s pid=%d", inst.source_id, os.path.basename(spec.argv[0]), proc.pid)
            if spec.settle_s:
                await asyncio.sleep(spec.settle_s)

    async def _reap_and_respawn(self, running: _Running) -> None:
        """(Re)start an endpoint's whole daemon set — after an exit, or a launch that never took.

        All-or-nothing on purpose: the members are interdependent (a private bus and the daemon
        that registers on it), so reviving one against a stale peer is not a recovery.
        """
        if running.procs:
            codes = [p.returncode for p in running.procs]
            logger.warning("[%s] daemon exited (rc=%s); restarting set", running.instance.source_id, codes)
            await self._kill_daemons(running)
            running.next_spawn_at = time.monotonic() + RESPAWN_BACKOFF
        else:
            # Nothing ever started (missing binary/config). Retry quietly on the backoff — the
            # source is up and idle meanwhile, so this self-heals once the dependency appears.
            logger.debug("[%s] no daemons running; retrying launch", running.instance.source_id)
        await self._spawn_daemons(running)

    async def _kill_daemons(self, running: _Running) -> None:
        procs, running.procs = running.procs, []
        running.next_spawn_at = 0.0
        for proc in reversed(procs):  # reverse start order: dependents before their bus
            if proc.returncode is not None:
                continue
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=STOP_GRACE)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                with contextlib.suppress(Exception):
                    await proc.wait()
        self._close_logs(running)

    @staticmethod
    def _close_logs(running: _Running) -> None:
        for log in running.logs:
            with contextlib.suppress(Exception):
                log.close()
        running.logs = []

    def _kill_stale_daemons(self) -> None:
        """Kill this family's daemons left behind by a previous server run.

        Daemons that hold a per-config lock (or a fixed port) would block the fresh instance, so a
        survivor from a crashed run must go. The pattern is scoped to our own config paths.
        """
        if not self.stale_pattern:
            return
        patterns = [self.stale_pattern] if isinstance(self.stale_pattern, str) else self.stale_pattern
        for pattern in patterns:
            try:
                subprocess.run(["pkill", "-f", pattern], capture_output=True, timeout=10, check=False)
            except (OSError, subprocess.SubprocessError) as e:
                logger.debug("[%s] stale daemon sweep skipped (%s): %s", self.name, pattern, e)
