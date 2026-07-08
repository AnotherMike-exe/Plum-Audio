#!/usr/bin/env python3
"""
Plum-Audio mesh orchestrator — SKELETON (Phase 2).

Maps the app's routing model onto verified aiosendspin primitives:

  Intra-server (player already on the ingesting unit's server):
      await group.add_client(player)           # live re-group, no reconnect
      await group.remove_client(player)         # back to a solo group

  Cross-server roam (pull a player from another unit):
      target_server.connect_to_client(player_url, connection_reason=ConnectionReason.PLAYBACK)
      target_server.reclaim_client_for_playback(player_id, timeout_s=...)
      # player leaves its old server with GoodbyeReason.ANOTHER_SERVER

  Idle pre-connect (fast-switch): hold players in ConnectionReason.DISCOVERY so a later
  route to PLAYBACK is cheap — replaces Plum-Snapcast's auto-switch-service.py.

Successor to federation/: Discovery (peer units), DataAggregator (unified servers/streams/
players view), Router (execute routes). Port the intent of federation/api.py dedup + remote
display logic here.

TODO(Phase 2):
  - Peer discovery (our own; server mDNS is off).
  - Aggregate multi-unit state into the app Server/Stream/Client/Group types.
  - Router.route_player / set_volume / control_stream over the SyncEngine seam.
  - Flask blueprint with parity to the old /api/federation/* routes.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("plum.mesh")


class Discovery:
    """Discover peer Plum-Audio units on the LAN (server mDNS is off — use our own beacon)."""


class DataAggregator:
    """Unified view of all units' servers, groups, and players for the frontend."""


class Router:
    """Execute routing actions across the mesh via the SyncEngine seam."""


class MeshOrchestrator:
    def __init__(self) -> None:
        self.discovery = Discovery()
        self.aggregator = DataAggregator()
        self.router = Router()
