#!/usr/bin/env python3
"""Dead-peer watchdog, end to end: a real aiosendspin server dials a real client through a TCP proxy
that can stop forwarding without closing, which is what a half-open session is.

TIER 0: real protocol, no rig, loopback only (ports 18927-18929). About 45 s.

    <venv>/bin/python tests/Integration/t0_player_peer_watchdog.py
Proves (1) the watched websocket does not break the Noise handshake, (2) an idle attached session
counts time-sync replies as traffic and is NOT closed, (3) a frozen peer is detected and the close
makes attach_websocket return, (4) a new dial attaches afterwards.
"""

import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend" / "scripts"))

import sendspin_player as sp  # noqa: E402
from aiosendspin.client.client import SendspinClient  # noqa: E402
from aiosendspin.client.listener import ClientListener  # noqa: E402
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat  # noqa: E402
from aiosendspin.models.types import AudioCodec, PlayerCommand, Roles  # noqa: E402
from aiosendspin.noise import Identity, InMemoryClientPairingStore, InMemoryServerPairingStore  # noqa: E402
from aiosendspin.noise.trust_store import ClientPairingConfig  # noqa: E402
from aiosendspin.server.server import SendspinServer  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("plum.sendspin_player").setLevel(logging.INFO)

LISTEN, PROXY = 18928, 18929


class Proxy:
    """TCP forwarder that can stop forwarding without closing: a half-open peer."""

    def __init__(self):
        self.frozen = False
        self.pairs = []

    async def start(self):
        self.server = await asyncio.start_server(self._accept, "127.0.0.1", PROXY)

    async def _accept(self, r, w):
        ur, uw = await asyncio.open_connection("127.0.0.1", LISTEN)
        self.pairs.append((w, uw))
        asyncio.ensure_future(self._pump(r, uw))
        asyncio.ensure_future(self._pump(ur, w))

    async def _pump(self, r, w):
        try:
            while True:
                data = await r.read(65536)
                if not data:
                    break
                while self.frozen:
                    await asyncio.sleep(0.05)
                w.write(data)
                await w.drain()
        except Exception:
            pass


class P:
    """The player's session bookkeeping + watchdog, on a real client."""

    def __init__(self, client):
        self.client = client
        self._audio_flowing = False
        self._last_audio_mono = 0.0
        self._sessions = {}
        self.seq = 0
        self._init_recovery_state()

    _init_recovery_state = sp.SendspinPlayer._init_recovery_state
    request_exit = sp.SendspinPlayer.request_exit
    _check_peer_silence = sp.SendspinPlayer._check_peer_silence
    _close_silent_session = sp.SendspinPlayer._close_silent_session
    _peer_close_overdue = sp.SendspinPlayer._peer_close_overdue

    async def on_connection(self, ws):
        self.seq += 1
        n = self.seq
        watched = sp._WatchedWebSocket(ws)
        self._session_ws[n] = watched
        print(f"  session {n} attached")
        t0 = time.monotonic()
        try:
            await self.client.attach_websocket(watched)
        finally:
            self._session_ws.pop(n, None)
            self._silence_closing.pop(n, None)
            print(f"  session {n} detached after {time.monotonic() - t0:.1f}s rx={watched.rx_frames} tx={watched.tx_frames}")


async def main():
    sp.PEER_SILENCE_TIMEOUT_S = 4.0
    loop = asyncio.get_running_loop()
    results = {}
    srv = SendspinServer(
        loop=loop,
        identity=Identity.generate(),
        server_name="E2E",
        pairing_store=InMemoryServerPairingStore(),
        allow_unencrypted=True,
    )
    await srv.start_server(port=18927, advertise_addresses=[], discover_clients=False)
    store = InMemoryClientPairingStore()
    await store.store_pairing_config(ClientPairingConfig(unpaired_access_enabled=True, record_mode_psk_id="e2e"))
    client = SendspinClient(
        identity=Identity.generate(),
        client_name="E2E player",
        roles=[Roles.PLAYER],
        pairing_store=store,
        player_support=ClientHelloPlayerSupport(
            supported_formats=[SupportedAudioFormat(codec=AudioCodec.PCM, sample_rate=44100, channels=2, bit_depth=16)],
            buffer_capacity=1 << 20,
            supported_commands=[PlayerCommand.VOLUME, PlayerCommand.MUTE],
        ),
    )
    await srv.trust_unpaired(client.identity.peer_id)
    p = P(client)
    listener = ClientListener(client_id="e2e-player", on_connection=p.on_connection, port=LISTEN, advertise_mdns=False)
    await listener.start()
    proxy = Proxy()
    await proxy.start()

    async def poll(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            await asyncio.sleep(0.5)
            p._check_peer_silence()

    srv.connect_to_client(f"ws://127.0.0.1:{PROXY}/sendspin")
    await poll(3)
    results["1 handshake through the watched websocket"] = client.connected and 1 in p._session_ws
    active = sorted(r for c in srv.clients for r in c.active_role_ids)
    results["1b player role ACTIVE on the server"] = any(r.startswith("player") for r in active)

    # Idle but attached for 3x the timeout: time sync only, no audio. Must NOT be closed.
    w = p._session_ws.get(1)
    rx0 = w.rx_frames if w else 0
    await poll(12)
    w = p._session_ws.get(1)
    results["2 idle attached session survives 3x the timeout"] = w is not None and p._peer_silence_closes == 0
    results["2b time-sync replies arrived while idle"] = w is not None and w.rx_frames > rx0
    if w:
        print(f"  idle 12 s: rx {rx0} -> {w.rx_frames}, unanswered now {w.unanswered_tx}, synced={client.is_time_synchronized()}")

    # Freeze the peer: half-open.
    proxy.frozen = True
    t0 = time.monotonic()
    while time.monotonic() - t0 < 20 and 1 in p._session_ws:
        await asyncio.sleep(0.5)
        p._check_peer_silence()
    took = time.monotonic() - t0
    results[f"3 frozen peer closed and detached (took {took:.1f}s, timeout 4s)"] = (
        1 not in p._session_ws and p._peer_silence_closes == 1
    )
    results["3b no process exit was needed"] = p.exit_code is None
    results["3c client reports disconnected"] = not client.connected

    # A new dial must attach.
    proxy.frozen = False
    for a, b in proxy.pairs:
        a.close()
        b.close()
    t0 = time.monotonic()
    while time.monotonic() - t0 < 25 and not (client.connected and len(p._session_ws) == 1):
        await asyncio.sleep(0.5)
        p._check_peer_silence()
    results[f"4 a new dial attached afterwards ({time.monotonic() - t0:.1f}s)"] = client.connected

    for name, good in results.items():
        print(f"[{'PASS' if good else 'FAIL'}] {name}")
    for coro in (client.disconnect(), listener.stop(), srv.stop_server()):
        try:
            await asyncio.wait_for(coro, 5)
        except Exception as exc:
            print("  cleanup:", repr(exc))
    try:
        await srv.close()
    except Exception:
        pass
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
