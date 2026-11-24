#!/usr/bin/env python3
"""
Plum-Audio mesh — DataAggregator: one unified MeshView across all reachable units.

Successor to Plum-Snapcast's federation state merge. The local half comes straight from the
engine's snapshot; the remote half is each discovered peer's snapshot fetched over the mesh REST
API. Peers come from `discovery` (a beacon carries identity + the reachable IP); the actual
state is pulled on a poll so the view reflects live grouping/streaming, not just liveness.

The HTTP fetch is injected (`fetch_snapshot`) so the aggregator doesn't hard-depend on a
particular client and stays unit-testable. A peer that's discovered but momentarily unreachable
is simply omitted from this cycle's view — discovery's TTL remains the authority on liveness.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import time
from collections.abc import Awaitable, Callable

from sync_engine.base import SyncEngine

from mesh.discovery import MeshDiscovery, Peer
from mesh.model import MeshView, UnitSnapshot

logger = logging.getLogger("plum.mesh.aggregator")


def _detect_local_host() -> str | None:
    """This unit's own IP on the default-route interface.

    A unit ignores its own beacon, so it never learns the address peers reach it on — yet the
    local half of the view must still carry a host, or a GUI bootstrapped against this unit can't
    open its controller WS / address its REST. The connect-to-TEST-NET trick sends no packets; it
    just asks the kernel which source IP the default route would use (the wlan0 address peers see).
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))  # RFC 5737 TEST-NET-1 — unroutable, never actually contacted
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


# fetch_snapshot(peer) -> the peer's UnitSnapshot.to_dict(), or None if unreachable this cycle.
FetchSnapshot = Callable[[Peer], Awaitable["dict | None"]]

DEFAULT_INTERVAL_S = 2.0
# Comfortably above any real rig, so normal operation is still one round trip for everyone at once.
MAX_CONCURRENT_FETCHES = 16
# A refresh that keeps failing is logged once, then at most this often. The poll runs every 2 s, so
# an unlimited log is 43 000 tracebacks a day onto an SD card for one persistent bug.
REFRESH_ERROR_LOG_EVERY_S = 60.0
# The player process posts its self-report every 3 s, with a 3 s request timeout
# (sendspin_player._report_loop). So a healthy player is never more than about 6 s late. Five missed
# reports is well past that and still short enough that a dead player stops reading as attached
# within one GUI glance.
LOCAL_PLAYER_REPORT_INTERVAL_S = 3.0
LOCAL_PLAYER_STALE_S = 5 * LOCAL_PLAYER_REPORT_INTERVAL_S
# Written by the container healthcheck. Optional: no file means "unknown", never "unhealthy".
HEALTH_FILE = os.environ.get("PLUM_HEALTH_FILE", "/data/health.json")
HEALTH_MAX_BYTES = 16 * 1024  # it rides every snapshot to every peer, so it stays small
HEALTH_REREAD_S = 5.0  # local_snapshot() runs per poll AND per API request; do not stat each time


class DataAggregator:
    def __init__(
        self,
        local_unit_id: str,
        engine: SyncEngine,
        discovery: MeshDiscovery,
        fetch_snapshot: FetchSnapshot,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
        health_file: str = HEALTH_FILE,
    ) -> None:
        self.local_unit_id = local_unit_id
        self._local_player_state: dict | None = None  # our speaker's self-report (see model)
        self._engine = engine
        self._discovery = discovery
        self._fetch = fetch_snapshot
        self.interval_s = interval_s
        self._clock = clock  # injected so tests can move time
        self._local_player_seen_at: float | None = None  # clock() of the last self-report
        self._local_player_offline = False  # last published state, so a change is logged once
        self.health_file = health_file
        self._health: tuple[dict | None, float | None] = (None, None)
        self._health_read_at: float | None = None
        self._local_host = _detect_local_host()
        self._view = MeshView(units=[self.local_snapshot()])
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._task = asyncio.ensure_future(self._poll_loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def view(self) -> MeshView:
        """The most recently built mesh view (updated each poll cycle)."""
        return self._view

    def set_local_player_state(self, state: dict) -> None:
        """Record our own speaker's self-report (pushed by the player process), and when it came."""
        if not isinstance(state, dict):
            return  # a malformed body must not replace a good report, nor refresh its timestamp
        self._local_player_state = state
        self._local_player_seen_at = self._clock()

    def _local_player_report(self) -> tuple[dict | None, float | None, bool]:
        """(report, age in seconds, offline) for our own speaker.

        The report used to be stored with no timestamp, so the last word of a player that crashed
        was published for ever — `attached: true`, to a server that had long since lost it.

        Once it is older than LOCAL_PLAYER_STALE_S the report is published with `attached` and
        `playing` forced False and `offline: True`. It is NOT dropped: the speaker's id, name and URL
        are what the GUI shows it by and what the router dials it back with, and an idle read is
        the correct one for every consumer (`follow._player_status` treats it as free). The original
        dict is left alone, so a player that resumes reporting is whole again on its next post.
        """
        state = self._local_player_state
        if state is None or self._local_player_seen_at is None:
            return None, None, False  # never reported: unknown, not offline
        age = max(0.0, self._clock() - self._local_player_seen_at)
        offline = age > LOCAL_PLAYER_STALE_S
        if offline != self._local_player_offline:
            self._local_player_offline = offline
            if offline:
                logger.warning("local player stopped reporting %.0fs ago; publishing it as offline", age)
            else:
                logger.info("local player is reporting again")
        if offline:
            state = {**state, "attached": False, "playing": False, "offline": True}
        return state, round(age, 1), offline

    def _read_health(self) -> tuple[dict | None, float | None]:
        """(contents, age in seconds) of the healthcheck file, or (None, None) when unknown.

        Defensive on every axis, because another component owns the file and its keys are not
        settled: a missing file, an unreadable one, an oversized one, invalid JSON or a non-object
        all read as unknown. Nothing here interprets a key. Cached for HEALTH_REREAD_S.
        """
        now = self._clock()
        if self._health_read_at is not None and now - self._health_read_at < HEALTH_REREAD_S:
            return self._health
        self._health_read_at = now
        result: tuple[dict | None, float | None] = (None, None)
        try:
            stat = os.stat(self.health_file)
            if stat.st_size <= HEALTH_MAX_BYTES:
                with open(self.health_file, encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict):
                    result = (data, round(max(0.0, time.time() - stat.st_mtime), 1))
        except (OSError, ValueError):
            pass  # absent or mid-write: unknown this time, read again on the next pass
        self._health = result
        return result

    def local_snapshot(self) -> UnitSnapshot:
        """The engine's snapshot, with this unit's own host stamped in (the engine can't know it)."""
        snap = self._engine.snapshot()
        if snap.host is None:
            snap.host = self._local_host
        # The engine only knows clients attached to ITS server; our speaker may be attached to
        # someone else's (a peer, Music Assistant, any Sendspin server). Its self-report travels
        # with the snapshot so every unit's GUI can still show where it went and what it's playing.
        snap.local_player, snap.local_player_age_s, snap.local_player_offline = self._local_player_report()
        snap.health, snap.health_age_s = self._read_health()
        return snap

    async def refresh(self) -> MeshView:
        """Rebuild the view now: local snapshot + every reachable peer's snapshot."""
        units: list[UnitSnapshot] = [self.local_snapshot()]
        peers = self._discovery.peers()
        # Bounded fan-out. This runs in the AUDIO event loop every interval_s, so the number of
        # sockets it opens at once has to be a property of our code, not of how many beacons
        # happen to have arrived — the beacon is unauthenticated broadcast. Discovery caps the
        # table; this caps the concurrency, and the two together bound the work per tick.
        sem = asyncio.Semaphore(MAX_CONCURRENT_FETCHES)

        async def fetch(peer: Peer) -> UnitSnapshot | None:
            async with sem:
                return await self._fetch_peer(peer)

        results = await asyncio.gather(*(fetch(p) for p in peers))
        units.extend(u for u in results if u is not None)
        self._view = MeshView(units=units)
        return self._view

    async def _fetch_peer(self, peer: Peer) -> UnitSnapshot | None:
        try:
            data = await self._fetch(peer)
        except Exception:  # noqa: BLE001 - a peer's HTTP hiccup must not drop the whole view
            logger.debug("snapshot fetch failed for %s", peer.unit_id, exc_info=True)
            return None
        if not data:
            return None
        snap = UnitSnapshot.from_dict(data)
        snap.host = peer.host  # the beacon source IP is the authority on how we reach this peer
        return snap

    async def _poll_loop(self) -> None:
        """Refresh the view every interval. A failure is logged and the loop continues.

        This used to suppress every exception with no log. One bug in a snapshot then froze the mesh
        view — routing, follow and the GUI all read it — and the only sign was that nothing changed.
        Rate-limited: the first failure is logged at once with its traceback, a fault that persists
        is logged again every REFRESH_ERROR_LOG_EVERY_S with the count in between, and the recovery
        is logged, so the log shows when the view was frozen and for how long.
        """
        failing = False
        last_logged = 0.0
        unlogged = 0
        while not self._stop.is_set():
            try:
                await self.refresh()
            except Exception:  # noqa: BLE001 - the loop must survive any fault in one refresh
                now = self._clock()
                if not failing or now - last_logged >= REFRESH_ERROR_LOG_EVERY_S:
                    logger.exception(
                        "mesh view refresh failed; the view is frozen%s",
                        f" ({unlogged} more failure(s) since the last report)" if unlogged else "",
                    )
                    last_logged = now
                    unlogged = 0
                else:
                    unlogged += 1
                failing = True
            else:
                if failing:
                    logger.info("mesh view refresh recovered")
                failing = False
                unlogged = 0
            await asyncio.sleep(self.interval_s)
