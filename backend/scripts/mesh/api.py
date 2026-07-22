#!/usr/bin/env python3
"""
Plum-Audio mesh — the REST surface (aiohttp, in the audio event loop).

Why aiohttp and not Flask: the mesh API must call the async router/aggregator inside the same
event loop that runs the SendspinServer and feeders. A WSGI Flask app would need a second
process and a thread bridge; aiohttp serves straight from the loop. The settings/integrations/
audio Flask APIs (config CRUD, no loop affinity) stay separate — this endpoint is mesh-only.

Endpoints (parity with the old /api/federation/* surface, so the GUI ports with little change):
  GET  /api/mesh/snapshot          this unit's local state (peers poll this to aggregate)
  GET  /api/mesh/view              the aggregated mesh (what the GUI renders)
  GET  /api/mesh/neighbourhood     Sendspin servers/players on this segment, via mDNS (interop)
  POST /api/mesh/player-state      our own speaker's self-report (where it is attached, what plays)
  POST /api/mesh/route             {player_id, source_id}          route a player onto a source
  POST /api/mesh/unroute           {player_id, source_id}          remove a player from a source
  POST /api/mesh/volume            {player_id, volume, muted}      per-player volume
  POST /api/mesh/source            {source_id, fifo?}              start a local source (a group)
  POST /api/mesh/source/stop       {source_id}                     stop a local source

Sources are local to the unit that ingests them ("servers stay") — /source acts on THIS unit;
there is no delegation. Multiple sources may run concurrently, each anchoring its own group.
"""

from __future__ import annotations

import logging

from aiohttp import web

from mesh.aggregator import DataAggregator
from mesh.router import Router, RouteError
from sync_engine.base import SyncEngine

logger = logging.getLogger("plum.mesh.api")

DEFAULT_API_PORT = 5001


@web.middleware
async def _cors(request: web.Request, handler):
    if request.method == "OPTIONS":
        resp = web.Response()
    else:
        resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


class MeshApi:
    """Serves the mesh REST endpoints backed by the engine, aggregator, and router."""

    def __init__(
        self, engine: SyncEngine, aggregator: DataAggregator, router: Router, *,
        port: int = DEFAULT_API_PORT, neighbourhood=None,
    ) -> None:
        self._engine = engine
        self._agg = aggregator
        self._router = router
        self._neighbourhood = neighbourhood
        self.port = port
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application(middlewares=[_cors])
        app.add_routes(
            [
                web.get("/api/mesh/snapshot", self._snapshot),
                web.get("/api/mesh/view", self._view),
                web.get("/api/mesh/neighbourhood", self._neighbours),
                web.post("/api/mesh/player-state", self._player_state),
                web.post("/api/mesh/route", self._route),
                web.post("/api/mesh/unroute", self._unroute),
                web.post("/api/mesh/volume", self._volume),
                web.post("/api/mesh/source", self._source_start),
                web.post("/api/mesh/source/stop", self._source_stop),
                web.route("OPTIONS", "/api/mesh/{tail:.*}", self._options),
            ]
        )
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host="0.0.0.0", port=self.port)
        await site.start()
        logger.info("mesh API up on :%d", self.port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # -- handlers ------------------------------------------------------------

    async def _snapshot(self, _request: web.Request) -> web.Response:
        # The aggregator's local snapshot, not the raw engine one: it stamps our host and carries
        # our speaker's self-report, both of which peers need (a claimed speaker is invisible to
        # the server it left, so its own report is the only way its unit can still describe it).
        return web.json_response(self._agg.local_snapshot().to_dict())

    async def _view(self, _request: web.Request) -> web.Response:
        """The aggregated mesh, plus WHICH unit answered.

        A unit serves its own GUI, and that page must feature *itself* — its own player and the
        source that player is on — not whichever unit happens to sort first. The view is otherwise
        identical from every unit, so identity has to come from the responder.
        """
        payload = self._agg.view().to_dict()
        payload["local_unit_id"] = self._agg.local_unit_id
        return web.json_response(payload)

    async def _player_state(self, request: web.Request) -> web.Response:
        """Our own player process reporting where it is attached and what it is playing.

        A speaker claimed by another server is, by definition, not attached to us — so the local
        server can no longer see it and the GUI would just lose it. The speaker tells us instead.
        """
        body = await self._json(request)
        self._agg.set_local_player_state(body)
        return web.json_response({"ok": True})

    async def _neighbours(self, _request: web.Request) -> web.Response:
        """Every Sendspin server and player mDNS can see on this segment, ours flagged.

        The mesh view covers PLUM units (they answer /api/mesh/snapshot); this covers the wider
        Sendspin network — a Music Assistant server, a third-party speaker — which has no mesh API
        and is reachable only by the protocol itself.
        """
        if self._neighbourhood is None:
            return web.json_response({"players": [], "servers": []})
        return web.json_response(self._neighbourhood.to_dict())

    async def _route(self, request: web.Request) -> web.Response:
        body = await self._json(request)
        player_id, source_id = body.get("player_id"), body.get("source_id")
        if not player_id or not source_id:
            return web.json_response({"error": "player_id and source_id required"}, status=400)
        try:
            ok = await self._router.route_player(player_id, source_id)
        except RouteError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response({"ok": ok})

    async def _unroute(self, request: web.Request) -> web.Response:
        body = await self._json(request)
        player_id, source_id = body.get("player_id"), body.get("source_id")
        if not player_id or not source_id:
            return web.json_response({"error": "player_id and source_id required"}, status=400)
        await self._router.unroute_player(player_id, source_id)
        return web.json_response({"ok": True})

    async def _volume(self, request: web.Request) -> web.Response:
        body = await self._json(request)
        player_id = body.get("player_id")
        if not player_id or "volume" not in body:
            return web.json_response({"error": "player_id and volume required"}, status=400)
        try:
            await self._router.set_volume(player_id, int(body["volume"]), bool(body.get("muted", False)))
        except (KeyError, RuntimeError) as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response({"ok": True})

    async def _source_start(self, request: web.Request) -> web.Response:
        body = await self._json(request)
        source_id = body.get("source_id")
        if not source_id:
            return web.json_response({"error": "source_id required"}, status=400)
        fifo = body.get("fifo") or f"/tmp/{source_id}-fifo"
        self._engine.start_source(source_id, fifo)
        return web.json_response({"ok": True, "source_id": source_id, "fifo": fifo})

    async def _source_stop(self, request: web.Request) -> web.Response:
        body = await self._json(request)
        source_id = body.get("source_id")
        if not source_id:
            return web.json_response({"error": "source_id required"}, status=400)
        await self._engine.stop_source(source_id)
        return web.json_response({"ok": True, "source_id": source_id})

    async def _options(self, _request: web.Request) -> web.Response:
        return web.Response()

    @staticmethod
    async def _json(request: web.Request) -> dict:
        try:
            return await request.json()
        except Exception:  # noqa: BLE001 - tolerate empty/malformed bodies as {}
            return {}
