#!/usr/bin/env python3
"""
Plum-Audio mesh orchestrator — composes the Phase 2 mesh over a running unit.

Successor to Plum-Snapcast's federation/ package. Wires the five mesh pieces around one
already-started PlumSendspinServer and owns their lifecycle:

    SendspinEngine   facade over the local server (keeps the mesh aiosendspin-free)
    MeshDiscovery    UDP-beacon peer discovery of PLUM units (their mesh APIs)
    Neighbourhood    Sendspin mDNS via Avahi: advertise our server, watch every player/server
                     on the segment (Music Assistant included) — the interop surface
    MeshClient       HTTP to peers' mesh APIs (aggregator poll + router delegate)
    DataAggregator   local snapshot + peers' snapshots -> one MeshView
    Router           intra / cross-server-reclaim / delegate route selection
    MeshApi          the REST surface (snapshot for peers, view + route for the GUI)

The wiring is the point: the aggregator fetches peers via the client; the router reads the
aggregator's view, resolves peer URLs via discovery, and delegates remote-source routes via the
client; the API drives the router and serves the aggregator. "Servers stay, players roam."
"""

from __future__ import annotations

import logging

from mesh.aggregator import DataAggregator
from mesh.api import MeshApi
from mesh.client import MeshClient
from mesh.discovery import MeshDiscovery
from mesh.neighbourhood import Neighbourhood
from mesh.router import Router
from sendspin_server import PlumSendspinServer
from sync_engine.sendspin_engine import SendspinEngine

logger = logging.getLogger("plum.mesh")


class MeshOrchestrator:
    def __init__(
        self, server: PlumSendspinServer, *, beacon_port: int = 8929, api_port: int = 5001,
        local_player_id: str | None = None,
    ) -> None:
        self.unit_id = server.unit_id
        self.engine = SendspinEngine(server)
        self.discovery = MeshDiscovery(
            server.unit_id,
            server.unit_name,
            server_port=server.port,
            player_port=server.port + 1,
            beacon_port=beacon_port,
        )
        self.client = MeshClient(api_port=api_port)
        self.aggregator = DataAggregator(server.unit_id, self.engine, self.discovery, self.client.fetch_snapshot)
        self.router = Router(
            server.unit_id,
            self.engine,
            view_provider=self.aggregator.view,
            peer_provider=self.discovery.get_peer,
            delegate=self.client.delegate_route,
        )
        # Which advertised player is OURS. Taken from configuration, not from a snapshot: at
        # construction the player has not connected yet, so a snapshot would report nobody and the
        # GUI would offer to send this unit's speaker to itself.
        self.neighbourhood = Neighbourhood(
            server.unit_id, server.unit_name, server_port=server.port,
            own_client_ids={local_player_id} if local_player_id else set(),
        )
        self.api = MeshApi(self.engine, self.aggregator, self.router, port=api_port,
                           neighbourhood=self.neighbourhood)

    async def start(self) -> None:
        await self.neighbourhood.start()
        await self.discovery.start()
        await self.client.start()
        await self.aggregator.start()
        await self.api.start()
        logger.info("mesh orchestrator up: unit=%s", self.unit_id)

    async def stop(self) -> None:
        await self.api.stop()
        await self.aggregator.stop()
        await self.client.stop()
        await self.discovery.stop()
        await self.neighbourhood.stop()
