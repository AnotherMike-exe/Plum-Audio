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
  POST /api/mesh/route             {player_id, source_id}          route a player onto a source
  POST /api/mesh/unroute           {player_id, source_id}          remove a player from a source
  POST /api/mesh/preconnect        {player_id}                     DISCOVERY pre-connect (fast switch)
  POST /api/mesh/volume            {player_id, volume, muted}      per-player volume
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

    def __init__(self, engine: SyncEngine, aggregator: DataAggregator, router: Router,
                 *, port: int = DEFAULT_API_PORT) -> None:
        self._engine = engine
        self._agg = aggregator
        self._router = router
        self.port = port
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application(middlewares=[_cors])
        app.add_routes([
            web.get("/api/mesh/snapshot", self._snapshot),
            web.get("/api/mesh/view", self._view),
            web.post("/api/mesh/route", self._route),
            web.post("/api/mesh/unroute", self._unroute),
            web.post("/api/mesh/preconnect", self._preconnect),
            web.post("/api/mesh/volume", self._volume),
            web.route("OPTIONS", "/api/mesh/{tail:.*}", self._options),
        ])
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
        return web.json_response(self._engine.snapshot().to_dict())

    async def _view(self, _request: web.Request) -> web.Response:
        return web.json_response(self._agg.view().to_dict())

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

    async def _preconnect(self, request: web.Request) -> web.Response:
        body = await self._json(request)
        player_id = body.get("player_id")
        if not player_id:
            return web.json_response({"error": "player_id required"}, status=400)
        try:
            self._router.preconnect(player_id)
        except RouteError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response({"ok": True})

    async def _volume(self, request: web.Request) -> web.Response:
        body = await self._json(request)
        player_id = body.get("player_id")
        if not player_id or "volume" not in body:
            return web.json_response({"error": "player_id and volume required"}, status=400)
        await self._router.set_volume(player_id, int(body["volume"]), bool(body.get("muted", False)))
        return web.json_response({"ok": True})

    async def _options(self, _request: web.Request) -> web.Response:
        return web.Response()

    @staticmethod
    async def _json(request: web.Request) -> dict:
        try:
            return await request.json()
        except Exception:  # noqa: BLE001 - tolerate empty/malformed bodies as {}
            return {}
