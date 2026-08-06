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
  POST /api/mesh/adopt             dial a FOREIGN Sendspin speaker (mDNS URL) onto one of our sources
  POST /api/mesh/release           hand it back to whatever server it came from
  POST /api/mesh/route             {player_id, source_id}          route a player onto a source
  POST /api/mesh/unroute           {player_id, source_id}          remove a player from a source
  POST /api/mesh/volume            {player_id, volume, muted}      per-player (endpoint) volume
  POST /api/mesh/source-volume     {source_id, volume?, muted?}    the SENDING DEVICE's own volume
  POST /api/mesh/source            {source_id, fifo?}              start a local source (a group)
  POST /api/mesh/source/stop       {source_id}                     stop a local source

Sources are local to the unit that ingests them ("servers stay") — /source acts on THIS unit;
there is no delegation. Multiple sources may run concurrently, each anchoring its own group.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable

import cors_policy
from aiohttp import web
from speaker_names import SpeakerNames
from sync_engine.base import SyncEngine

from mesh.aggregator import DataAggregator
from mesh.router import RouteError, Router

logger = logging.getLogger("plum.mesh.api")

DEFAULT_API_PORT = 5001

# The aiohttp middleware signature, spelled out rather than imported from aiohttp.typedefs: the
# requirement is `aiohttp>=3.9` and annotations are evaluated at import time on 3.13, so an alias
# that moved between releases would be an import-time crash in the audio process.
_Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]
_Middleware = Callable[[web.Request, _Handler], Awaitable[web.StreamResponse]]


def make_cors_middleware(hosts_provider) -> _Middleware:
    """CORS restricted to origins a Plum GUI is actually served from.

    `hosts_provider()` returns the currently-known host set — read per request, not captured once,
    because peers come and go and a unit discovered after start-up must still be able to drive this
    one from its own page.

    The GUI POSTs cross-origin to :5001 for EVERY unit including the one serving the page (its GETs
    go through nginx same-origin), so "self" has to be in the set too, or the very page you are
    looking at loses route/volume/adopt.

    Requests with NO Origin — peer snapshot polls, delegated routes, the loopback player-state POST —
    pass through untouched. They are not browser requests and CORS never applied to them; refusing
    them would break mesh aggregation from the inside with nothing visible in any browser.
    """

    @web.middleware
    async def _cors(request: web.Request, handler):
        origin = request.headers.get("Origin")
        allow = cors_policy.origin_header(origin, hosts_provider())

        if request.method == "OPTIONS":
            resp = web.Response()
        else:
            resp = await handler(request)

        if allow is not None:
            resp.headers["Access-Control-Allow-Origin"] = allow
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
            # Preflights are otherwise re-sent for every POST; the allowlist changes on the scale of
            # units joining the mesh, not seconds.
            resp.headers["Access-Control-Max-Age"] = "600"
            resp.headers["Vary"] = "Origin"
        elif origin:
            # Loud on purpose. This is the failure mode that takes the mesh view down on every unit
            # at once, and the fix (PLUM_ALLOWED_ORIGINS) needs to know exactly what was rejected.
            logger.warning(
                "CORS: refused origin %r (known hosts: %s) — set PLUM_ALLOWED_ORIGINS to allow it",
                origin,
                ", ".join(sorted(hosts_provider())) or "none",
            )
        return resp

    return _cors


class MeshApi:
    """Serves the mesh REST endpoints backed by the engine, aggregator, and router."""

    def __init__(
        self,
        engine: SyncEngine,
        aggregator: DataAggregator,
        router: Router,
        *,
        port: int = DEFAULT_API_PORT,
        neighbourhood=None,
    ) -> None:
        self._engine = engine
        self._agg = aggregator
        self._router = router
        self._neighbourhood = neighbourhood
        # Read-only here: the audio process learns these while a speaker is attached (see
        # sendspin_server.snapshot). Its own instance, so a reload picks up whatever is on disk.
        self._speaker_names = SpeakerNames()
        self.port = port
        self._runner: web.AppRunner | None = None
        # Consume relay: the local player (producer) forwards what it observes as a group MEMBER of
        # whatever server it plays — including a FOREIGN one (Music Assistant) — namely the group's
        # controller state and visualizer frames, plus commands back. This is purely internal
        # plumbing between our own player and our own GUI; the spec-native part is the player being a
        # conformant group member. See sendspin_player.py / MeshApi._consume.
        self._consumers: set[web.WebSocketResponse] = set()
        self._producer: web.WebSocketResponse | None = None
        self._last_ctrl: dict | None = None  # cache so a GUI that connects mid-session gets it
        self._last_art: dict | None = None  # ditto for album art (per-track, low rate)

    def _known_hosts(self) -> set[str]:
        """Every host a Plum GUI on this mesh can legitimately be served from, right now.

        Derived from the aggregator's view rather than the discovery table: the view already carries
        this unit's OWN host (which the peer table never does — a unit ignores its own beacon), and
        that entry is exactly the one whose absence would break the page being looked at.
        """
        with contextlib.suppress(Exception):
            return cors_policy.known_hosts(self._agg.view().units)
        return set(cors_policy.LOOPBACK_HOSTS)

    async def start(self) -> None:
        app = web.Application(middlewares=[make_cors_middleware(self._known_hosts)])
        app.add_routes(
            [
                web.get("/api/mesh/snapshot", self._snapshot),
                web.get("/api/mesh/view", self._view),
                web.get("/api/mesh/neighbourhood", self._neighbours),
                web.get("/api/mesh/consume", self._consume),
                web.post("/api/mesh/player-state", self._player_state),
                web.post("/api/mesh/adopt", self._adopt),
                web.post("/api/mesh/release", self._release),
                web.post("/api/mesh/route", self._route),
                web.post("/api/mesh/unroute", self._unroute),
                web.post("/api/mesh/volume", self._volume),
                web.post("/api/mesh/source-volume", self._source_volume),
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

    async def _adopt(self, request: web.Request) -> web.Response:
        """Pull a FOREIGN Sendspin speaker onto one of our sources.

        A speaker discovered by mDNS is just a player whose URL came from the neighbourhood rather
        than from a peer's snapshot, so this is the same primitive pair the mesh already uses for a
        peer's player: dial it for PLAYBACK, then add it to the source's group. The speaker leaves
        whatever server had it the spec's way (client/goodbye another_server) — that is how the
        protocol is meant to work, and it is what makes a third-party speaker usable as one of ours.
        """
        body = await self._json(request)
        url, source_id = body.get("url"), body.get("source_id")
        player_id = body.get("player_id")
        if not url or not source_id:
            return web.json_response({"error": "url and source_id required"}, status=400)
        try:
            ok = await self._engine.adopt_client(source_id, url, player_id=player_id)
        except Exception as e:  # noqa: BLE001 - report the failure rather than 500-ing
            logger.exception("adopt failed")
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response({"ok": bool(ok)})

    async def _release(self, request: web.Request) -> web.Response:
        """Let a foreign speaker go: drop it from the group and hang up, so its own server can
        take it back (Music Assistant re-dials on its next discovery/playback)."""
        body = await self._json(request)
        url, source_id = body.get("url"), body.get("source_id")
        player_id = body.get("player_id")
        if not player_id or not source_id:
            return web.json_response({"error": "player_id and source_id required"}, status=400)
        await self._engine.release_client(source_id, player_id, url=url)
        return web.json_response({"ok": True})

    async def _consume(self, request: web.Request) -> web.WebSocketResponse:
        """WS bridge for foreign-server consumption. `?role=player` = the producer (our local
        player, one at a time); anything else = a GUI consumer.

        Producer → consumers: `{"t":"ctrl",...}` (supported_commands/volume, cached) and
        `{"t":"viz","s":[...],"l":N}` (spectrum 0-255 + loudness, ~30/s, not cached).
        Consumer → producer: `{"t":"cmd","command":"pause"}` (transport to the foreign server).
        """
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        is_player = request.query.get("role") == "player"
        if is_player:
            self._producer = ws
            logger.info("consume relay: player producer connected")
        else:
            self._consumers.add(ws)
            for cached in (self._last_ctrl, self._last_art):  # bring a late GUI up to speed
                if cached is not None:
                    with contextlib.suppress(Exception):
                        await ws.send_json(cached)
        try:
            async for msg in ws:
                if msg.type is not web.WSMsgType.TEXT:
                    continue
                try:
                    data = msg.json()
                except Exception:  # noqa: BLE001
                    continue
                if is_player:
                    if data.get("t") == "ctrl":
                        self._last_ctrl = data
                    elif data.get("t") == "art":
                        self._last_art = data
                    await self._broadcast(data)  # ctrl + viz + art → every GUI
                elif data.get("t") == "cmd" and self._producer is not None:
                    with contextlib.suppress(Exception):
                        await self._producer.send_json(data)  # command → the player
        finally:
            if is_player and self._producer is ws:
                self._producer = None
                self._last_ctrl = None
                self._last_art = None
            else:
                self._consumers.discard(ws)
        return ws

    async def _broadcast(self, data: dict) -> None:
        dead = []
        for c in self._consumers:
            try:
                await c.send_json(data)
            except Exception:  # noqa: BLE001 - drop a dead consumer, keep the rest
                dead.append(c)
        for c in dead:
            self._consumers.discard(c)

    async def _neighbours(self, _request: web.Request) -> web.Response:
        """Every Sendspin server and player mDNS can see on this segment, ours flagged.

        The mesh view covers PLUM units (they answer /api/mesh/snapshot); this covers the wider
        Sendspin network — a Music Assistant server, a third-party speaker — which has no mesh API
        and is reachable only by the protocol itself.
        """
        if self._neighbourhood is None:
            return web.json_response({"players": [], "servers": []})
        payload = self._neighbourhood.to_dict()

        # Announce an idle speaker under the name it actually calls ITSELF, not its mDNS instance
        # name. A handshake name is only observable while the speaker is attached, and a third-party
        # device typically publishes no `name` TXT key — so mDNS alone gives
        # "home-assistant-voice-a1b2c3" for something that calls itself "Home Assistant Voice PE - 01".
        #
        # The GUI used to memoise this itself, in localStorage: per-browser, per-origin, and blank
        # until that particular tab had watched that particular speaker attach. Doing it here makes
        # one answer for every client, and needs no GUI change at all — it already prefers
        # `friendly_name`. See speaker_names.py.
        names = self._speaker_names.all()
        if names:
            for entry in payload.get("players", []):
                learned = names.get(entry.get("url"))
                if learned:
                    entry["friendly_name"] = learned
        return web.json_response(payload)

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

    async def _source_volume(self, request: web.Request) -> web.Response:
        """The volume ON THE SENDING DEVICE — the phone's AirPlay/BT slider, Spotify's device volume.

        Separate from /volume (our own render endpoints) because the two are genuinely different
        quantities and stack: the sender attenuates what it transmits, each endpoint then applies its
        own gain. Sendspin models only the latter, so this surface is ours.
        """
        body = await self._json(request)
        source_id = body.get("source_id")
        if not source_id or ("volume" not in body and "muted" not in body):
            return web.json_response({"error": "source_id and volume or muted required"}, status=400)
        volume = None if body.get("volume") is None else int(body["volume"])
        muted = None if body.get("muted") is None else bool(body["muted"])
        try:
            await self._router.set_source_volume(source_id, volume, muted)
        except (KeyError, RuntimeError, NotImplementedError) as e:
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
