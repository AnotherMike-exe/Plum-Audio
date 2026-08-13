#!/usr/bin/env python3
"""
Plum-Audio mesh smoke test — validates the ingest + group + stream + mesh-control
API path of aiosendspin executes on the pinned version/hardware. Headless (no player,
no speaker): proves the *control plane* the product depends on actually runs.

Steps (each prints PASS/FAIL):
  1. Construct SendspinServer, start HTTP listener (mDNS advertise/discover off).
  2. get_or_create_client -> auto solo group; start_stream -> PushStream.
  3. Feed one 200ms sine tone via prepare_audio + commit_audio (ingest path).
  4. Second server; move a client between two groups (intra-server live re-route).
  5. Exercise mesh cross-server primitives: register_client_url + reclaim_client_for_playback
     (returns True), connect_to_client scheduled (no throw).
  6. ADMISSION (a-c): real SendspinClient handshakes that must be admitted and negotiated
     but NOT activated — the three silent-dud combinations.
  7. ADMISSION: client unpaired_access_enabled AND server trust_unpaired -> role ACTIVATED.
     This is the mechanism our own mesh depends on.

Steps 6-7 exist because the pre-9.x version of this file never constructed a client, so it
could not have caught the failure that matters most. Measured truth table (9.1.0):

    client unpaired_access | server trust_unpaired | negotiated  | ACTIVATED
    -----------------------+-----------------------+-------------+-----------
             no            |         no            | player@v1   |    -
             yes           |         no            | player@v1   |    -
             no            |         yes           | player@v1   |    -
             yes           |         yes           | player@v1   | player@v1

The role is ALWAYS negotiated, so the client always *looks* attached: it is in the group,
in the GUI, at the right volume, and renders nothing, with no error at either end. The
signature is negotiated_role_ids diverging from active_role_ids — assert on the latter.
See docs/AIOSENDSPIN-BUMP-SCOPE.md break #3.

TIER 0 — real protocol, no hardware. Unlike its tier 2-4 neighbours this is Python, takes no
host argument, and touches no rig: it stands two servers up on localhost. It does need an
interpreter with the aiosendspin version under test, which is NOT the repo's pinned one:

    python3.13 -m venv /tmp/venv91 && /tmp/venv91/bin/pip install 'aiosendspin[server]==9.1.0'
    /tmp/venv91/bin/python tests/Integration/t0_sendspin_protocol.py

Formerly _resources/spike/mesh_smoke.py. Promoted out of the gitignored spike area on
2026-08-12 because it is the mandated pre-bump gate and was therefore being lost between
sessions.
"""
import asyncio, math, struct, sys, time, logging

logging.basicConfig(level=logging.WARNING)

from aiosendspin.server.server import SendspinServer
from aiosendspin.server.audio import AudioFormat
from aiosendspin.client.client import SendspinClient
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.types import AudioCodec, PlayerCommand, Roles
from aiosendspin.noise import Identity, InMemoryClientPairingStore, InMemoryServerPairingStore
from aiosendspin.noise.trust_store import ClientPairingConfig

RATE, BITS, CH = 48000, 16, 2

# 9.1.0 requires an X25519 identity and a pairing store per peer, and rejects cleartext
# clients unless allow_unencrypted is set. We pass it explicitly here for the same reason
# production does: sendspin-cpp has no encryption, so it is a standing requirement, not a
# transitional one. See docs/AIOSENDSPIN-BUMP-SCOPE.md.
def make_server(loop, name, *, allow_unencrypted=True):
    return SendspinServer(
        loop=loop,
        identity=Identity.generate(),
        server_name=name,
        pairing_store=InMemoryServerPairingStore(),
        allow_unencrypted=allow_unencrypted,
    )


def tone(ms, freq=440):
    n = int(RATE * ms / 1000)
    buf = bytearray()
    for i in range(n):
        s = int(0.2 * 32767 * math.sin(2 * math.pi * freq * i / RATE))
        buf += struct.pack("<hh", s, s)
    return bytes(buf)

def ok(step, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {step} {extra}")
    return cond

async def main():
    loop = asyncio.get_running_loop()
    passed = True

    # 1. server up
    srvA = make_server(loop, "Unit A")
    await srvA.start_server(port=8927, advertise_addresses=[], discover_clients=False)
    passed &= ok("1 server-start", srvA._app is not None, f"server_id={srvA.id[:12]}...")

    # 2. client + auto group + start_stream
    cA = srvA.get_or_create_client("playerA")
    grpA = cA.group
    passed &= ok("2a client-auto-group", grpA is not None, f"group_id={grpA.group_id}")
    ps = grpA.start_stream()
    passed &= ok("2b start_stream->PushStream", ps is not None)

    # 3. ingest: feed a tone buffer through the real encode/commit path
    fmt = AudioFormat(RATE, BITS, CH)
    ps.set_live_source(True)
    t0 = time.perf_counter()
    ps.prepare_audio(tone(200), fmt)
    start_us = await ps.commit_audio()
    dt = (time.perf_counter() - t0) * 1000
    passed &= ok("3 ingest prepare+commit", isinstance(start_us, int), f"play_start_us={start_us} commit={dt:.1f}ms")

    # 4. intra-server live re-route: two clients, move one into the other's group
    c2 = srvA.get_or_create_client("playerA2")
    g_before = c2.group.group_id
    await grpA.add_client(c2)               # move c2 into grpA (multi-client group)
    g_after = c2.group.group_id
    passed &= ok("4a add_client re-route", g_after == grpA.group_id and g_after != g_before,
                 f"{g_before[:8]}->{g_after[:8]}")
    await grpA.remove_client(c2)            # back to a fresh solo group
    passed &= ok("4b remove_client -> new solo", c2.group.group_id != grpA.group_id)

    # 5. mesh cross-server primitives
    srvB = make_server(loop, "Unit B")
    await srvB.start_server(port=8937, advertise_addresses=[], discover_clients=False)
    # register a URL for playerA on srvB, then reclaim it for playback (True == initiated)
    cB = srvB.get_or_create_client("playerA")
    srvB.register_client_url("playerA", "ws://127.0.0.1:8928/sendspin")
    reclaimed = srvB.reclaim_client_for_playback("playerA", timeout_s=2.0)
    passed &= ok("5a reclaim_for_playback", reclaimed is True)
    # server-dials-player scheduled without throwing (target absent; retry off)
    try:
        srvB.connect_to_client("ws://127.0.0.1:8928/sendspin")
        passed &= ok("5b connect_to_client scheduled", True)
    except Exception as e:
        passed &= ok("5b connect_to_client scheduled", False, str(e))

    # 6/7. ADMISSION — the whole point of steps 6-7 is that "it connected" is NOT the test.
    # A client id is now the peer's X25519 public key, so it is only knowable after the
    # handshake; everything downstream keys on it.
    passed &= await admission_checks(loop, srvA)

    await asyncio.sleep(0.3)  # let background tasks spin
    for s in (srvA, srvB):
        try:
            await s.stop_server(); await s.close()
        except Exception:
            pass

    print(f"\n=== {'ALL PASS' if passed else 'SOME FAILED'} ===")
    sys.exit(0 if passed else 1)


async def make_client(name, *, unpaired_access):
    """A minimal player-role client. unpaired_access mirrors the ClientPairingConfig flag
    that, together with the server's trust_unpaired(), is what lets an UNPAIRED peer play.
    Both store_pairing_config and trust_unpaired are coroutines — awaiting them is the
    whole mechanism, and forgetting to is indistinguishable from the feature not working."""
    store = InMemoryClientPairingStore()
    await store.store_pairing_config(
        ClientPairingConfig(unpaired_access_enabled=unpaired_access, record_mode_psk_id="smoke")
    )
    return SendspinClient(
        identity=Identity.generate(),
        client_name=name,
        roles=[Roles.PLAYER],
        pairing_store=store,
        player_support=ClientHelloPlayerSupport(
            supported_formats=[
                SupportedAudioFormat(codec=AudioCodec.PCM, sample_rate=RATE, channels=CH, bit_depth=BITS)
            ],
            buffer_capacity=1 << 20,
            supported_commands=[PlayerCommand.VOLUME, PlayerCommand.MUTE],
        ),
    )


async def admission_case(loop, port, *, unpaired_access, trust):
    """Stand up an isolated server, connect one player-role client under the given
    opt-in combination, and report what the server NEGOTIATED vs what it ACTIVATED.

    The two are not the same, and that gap is the entire hazard: `client.activities` is
    what the SERVER declares in server/activate, not what the client was granted, so it
    is the wrong thing to assert on. Read active_role_ids off the server's client object.
    """
    srv = make_server(loop, "Admission Probe")
    await srv.start_server(port=port, advertise_addresses=[], discover_clients=False)
    client = await make_client("Probe", unpaired_access=unpaired_access)
    if trust:
        await srv.trust_unpaired(client.identity.peer_id)
    try:
        await asyncio.wait_for(client.connect(f"ws://127.0.0.1:{port}/sendspin"), timeout=8)
    except Exception as e:
        print(f"       connect raised: {type(e).__name__}: {e}")
        negotiated, active = [], []
    else:
        await asyncio.sleep(0.8)  # let role activation settle
        negotiated = sorted(r for c in srv.clients for r in c.negotiated_role_ids)
        active = sorted(r for c in srv.clients for r in c.active_role_ids)
    for shutdown in (client.disconnect(), srv.stop_server()):
        try:
            await shutdown
        except Exception:
            pass
    await srv.close()
    return negotiated, active


async def admission_checks(loop, _srv):
    """The truth table. A player role is ALWAYS negotiated, so a client always *looks*
    attached — it is only activated when the client sets unpaired_access_enabled AND the
    server calls trust_unpaired() for that peer id. Three of these four combinations are
    a silent dud: in the GUI, in the group, at the right volume, rendering nothing.

    This is the check the pre-9.x smoke test could not make, because it never built a
    client. Do not weaken it to "did it connect".
    """
    cases = [
        ("6a neither opt-in      -> negotiated, NOT active", False, False, 8961, False),
        ("6b client opt-in only  -> negotiated, NOT active", True, False, 8962, False),
        ("6c server trust only   -> negotiated, NOT active", False, True, 8963, False),
        ("7  BOTH                -> negotiated AND ACTIVE ", True, True, 8964, True),
    ]
    passed = True
    for label, ua, trust, port, want_active in cases:
        negotiated, active = await admission_case(loop, port, unpaired_access=ua, trust=trust)
        good = bool(negotiated) and (bool(active) == want_active)
        passed &= ok(label, good, f"negotiated={negotiated} active={active}")
    return passed


if __name__ == "__main__":
    try:
        asyncio.run(asyncio.wait_for(main(), timeout=45))
    except asyncio.TimeoutError:
        print("[FAIL] timeout"); sys.exit(2)
