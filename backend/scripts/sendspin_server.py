#!/usr/bin/env python3
"""
Plum-Audio — in-process Sendspin server + per-source PushStream feeders.

SKELETON (Phase 1). Grounded in the verified aiosendspin 6.0.5 API (see
_resources/spike/mesh_smoke.py, which exercises this exact path on hardware).

Responsibilities (this process, one per unit):
  - Run a SendspinServer on this unit (mDNS advertising OFF — we drive by URL).
  - For each active local source FIFO (/tmp/<source>-fifo), run a feeder that reads PCM and
    pushes it into the source's group via PushStream.prepare_audio + commit_audio.
  - Expose the server object to the mesh orchestrator (group add/remove, reclaim, dial).

TODO(Phase 1):
  - Wire source FIFO discovery to the integration lifecycle (AirPlay first).
  - Metadata/artwork/visualizer role emission from the control scripts (out-of-band).
  - supervisord integration + graceful shutdown.
"""
from __future__ import annotations

import asyncio
import logging
import os

from aiosendspin.server.server import SendspinServer
from aiosendspin.server.audio import AudioFormat

logger = logging.getLogger("plum.sendspin_server")

SERVER_PORT = 8927
# AirPlay writes 44100:16:2; most sources negotiated per-integration in Phase 1.
DEFAULT_FORMAT = AudioFormat(44100, 16, 2)
COMMIT_CHUNK_MS = 20  # feeder cadence


class SourceFeeder:
    """Reads PCM from one source FIFO and pushes it into that source's group.

    A group is obtained from a SendspinClient (server.get_or_create_client(...).group);
    group.start_stream() returns the PushStream. Feed loop:
        ps.set_live_source(True)
        ps.prepare_audio(pcm, fmt); await ps.commit_audio()
    See mesh_smoke.py step 3 for the validated call sequence.
    """

    def __init__(self, server: SendspinServer, source_id: str, fifo_path: str,
                 fmt: AudioFormat = DEFAULT_FORMAT) -> None:
        self.server = server
        self.source_id = source_id
        self.fifo_path = fifo_path
        self.fmt = fmt
        self._task: asyncio.Task | None = None

    async def run(self) -> None:  # pragma: no cover - Phase 1
        raise NotImplementedError(
            "Phase 1: open FIFO non-blocking, chunk to COMMIT_CHUNK_MS, "
            "prepare_audio + commit_audio; handle idle/underrun."
        )


class PlumSendspinServer:
    """Owns the unit's SendspinServer and its source feeders."""

    def __init__(self, unit_id: str, unit_name: str, port: int = SERVER_PORT) -> None:
        self.unit_id = unit_id
        self.unit_name = unit_name
        self.port = port
        self.server: SendspinServer | None = None
        self.feeders: dict[str, SourceFeeder] = {}

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.server = SendspinServer(loop=loop, server_id=self.unit_id, server_name=self.unit_name)
        # mDNS OFF: SendspinServer always constructs AsyncZeroconf; keep it from advertising
        # (5353 collides with our Avahi). We connect players by explicit URL via the orchestrator.
        await self.server.start_server(port=self.port, advertise_addresses=[], discover_clients=False)
        logger.info("Sendspin server up: %s (%s) :%d", self.unit_name, self.unit_id, self.port)

    async def stop(self) -> None:
        if self.server:
            await self.server.stop_server()
            await self.server.close()


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    unit_id = os.environ.get("PLUM_UNIT_ID", "unit-local")
    unit_name = os.environ.get("PLUM_UNIT_NAME", "Plum Audio")
    srv = PlumSendspinServer(unit_id, unit_name)
    await srv.start()
    try:
        await asyncio.Event().wait()  # run forever (Phase 1: supervise feeders + mesh)
    finally:
        await srv.stop()


if __name__ == "__main__":
    asyncio.run(main())
