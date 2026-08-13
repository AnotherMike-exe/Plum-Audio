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
  8. LEGACY CLEARTEXT (a-e): a raw websocket sending a 6.0.5-shaped client/hello — our GUI
     controller's exact payload, and an ESP32-shaped player — must be admitted, KEEP ITS OWN
     client id, and get ACTIVE roles with no pairing and no trust. Plus the inverse: with
     allow_unencrypted=False the same hello must be refused.

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

Step 8 is the other half, and it is the PREMISE of the whole 9.x bump for this product:
a CLEARTEXT client skips that trust gate entirely. The server activates its negotiated
roles straight from the legacy client/hello branch, so third-party devices need neither
pairing nor trust_unpaired — which is what makes allow_unencrypted=True sufficient for
sendspin-cpp speakers, Music Assistant, and our own hand-rolled GUI controller.

What step 8 does NOT prove: that real sendspin-cpp firmware or Music Assistant send a
hello we accept. It proves the shape OUR client sends is accepted, and that the flag is
load-bearing in both directions. The firmware half is still a rig test.

TIER 0 — real protocol, no hardware. Unlike its tier 2-4 neighbours this is Python, takes no
host argument, and touches no rig: it stands two servers up on localhost. It does need an
interpreter with the aiosendspin version under test, which is NOT the repo's pinned one:

    python3.13 -m venv /tmp/venv91 && /tmp/venv91/bin/pip install 'aiosendspin[server]==9.1.0'
    /tmp/venv91/bin/python tests/Integration/t0_sendspin_protocol.py

Formerly _resources/spike/mesh_smoke.py. Promoted out of the gitignored spike area on
2026-08-12 because it is the mandated pre-bump gate and was therefore being lost between
sessions.
"""
import asyncio, json, math, struct, sys, time, logging

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

    # 8. LEGACY CLEARTEXT — the premise of the whole 9.x bump for this product.
    passed &= await legacy_checks(loop)

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



# -- step 8: the legacy cleartext path ---------------------------------------------------------


# The GUI's controller hello, copied from frontend/services/sendspinControllerClient.ts (~:329).
# Kept verbatim rather than minimised: the point is to exercise the bytes our shipping client
# actually sends, so a divergence here is a real signal and not a fixture drifting.
GUI_HELLO_ROLES = ["controller@v1", "metadata@v1", "artwork@v1", "visualizer@v1"]
GUI_HELLO_SUPPORT = {
    "artwork@v1_support": {
        "channels": [{"source": "album", "format": "jpeg", "media_width": 512, "media_height": 512}]
    },
    "visualizer@v1_support": {
        "buffer_capacity": 65536,
        "rate_max": 30,
        "types": ["spectrum", "loudness"],
        "spectrum": {"n_disp_bins": 256, "scale": "log", "f_min": 40, "f_max": 16000},
    },
}


def legacy_hello(client_id, roles, extra=None):
    payload = {
        "client_id": client_id,
        "name": "Plum Web GUI",
        "version": 1,
        "device_info": {"product_name": "Plum Web GUI", "manufacturer": "Plum Solutions"},
        "supported_roles": list(roles),
    }
    payload.update(extra or {})
    return json.dumps({"type": "client/hello", "payload": payload})


async def legacy_probe(port, client_id, roles, extra=None, inspect=None):
    """Connect as a 6.0.5-era CLEARTEXT client and report what came back.

    Returns (hello_payload | None, inspect_result). No aiosendspin client anywhere in this path —
    a raw websocket, because that is what our hand-rolled GUI controller and every sendspin-cpp
    ESP32 speaker are. If this stops working, allow_unencrypted has stopped meaning anything and
    the bump loses every third-party device plus Music Assistant.

    `inspect` runs while the socket is still OPEN, which is the only time the server-side client
    object exists to look at — a disconnected client is cleaned out of the registry.
    """
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"ws://127.0.0.1:{port}/sendspin") as ws:
            await ws.send_str(legacy_hello(client_id, roles, extra))
            hello = None
            deadline = asyncio.get_running_loop().time() + 5
            while asyncio.get_running_loop().time() < deadline:
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=2)
                except asyncio.TimeoutError:
                    break
                if msg.type is not aiohttp.WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                if data.get("type") == "server/hello":
                    hello = data.get("payload")
                    break
            await asyncio.sleep(0.4)  # let the server finish registering us
            return hello, (inspect() if inspect else None)


async def legacy_checks(loop):
    """A cleartext client must be admitted, keep its OWN client id, and get ACTIVE roles.

    Three separate claims, and the middle one is easy to lose sight of. Under 9.x an encrypted
    client's id is its X25519 public key, but a legacy hello carries its own — which is what keeps
    the GUI's `ctrl:<source_id>:<nonce>` convention working, since that hint is the only way it can
    name the source it wants to control.

    Roles here are activated straight from the negotiated set (server/connection.py, the
    `if not self.is_encrypted` branch), bypassing the trust gate that steps 6-7 exercise. So a
    cleartext client needs NO pairing and NO trust_unpaired — the exact opposite of our own player,
    and the reason `allow_unencrypted=True` is sufficient for third-party devices.
    """
    port = 8971
    srv = make_server(loop, "Legacy Probe", allow_unencrypted=True)
    await srv.start_server(port=port, advertise_addresses=[], discover_clients=False)
    passed = True

    def active_of(server, client_id):
        """The server's view of a client, read while it is still connected."""
        client = server.get_client(client_id)
        return None if client is None else sorted(client.active_role_ids)

    # 8a. The shipping GUI controller, id and all.
    ctrl_id = "ctrl:airplay-1:abc123"
    hello, active = await legacy_probe(
        port, ctrl_id, GUI_HELLO_ROLES, extra=GUI_HELLO_SUPPORT, inspect=lambda: active_of(srv, ctrl_id)
    )
    passed &= ok("8a legacy controller admitted", hello is not None,
                 f"server/hello={'received' if hello else 'NONE'}")
    passed &= ok("8b legacy client keeps its OWN id", active is not None,
                 f"server knows {ctrl_id!r}: {active is not None}")
    passed &= ok("8c legacy roles are ACTIVE without any trust", bool(active), f"active={active}")
    await asyncio.sleep(0.3)

    # 8d. An ESP32-shaped speaker: player role only, cleartext, never trusted. This is the case
    # the whole allow_unencrypted decision rests on.
    spk_id = "AA:BB:CC:DD:EE:FF"
    _hello, spk_active = await legacy_probe(
        port, spk_id, ["player@v1"],
        extra={"player@v1_support": {
            "supported_formats": [{"codec": "pcm", "sample_rate": RATE, "channels": CH, "bit_depth": BITS}],
            "buffer_capacity": 1 << 20,
            "supported_commands": ["volume", "mute"],
        }},
        inspect=lambda: active_of(srv, spk_id),
    )
    passed &= ok("8d legacy SPEAKER gets an active player role",
                 bool(spk_active and any(r.startswith("player") for r in spk_active)),
                 f"active={spk_active}")

    # 8e. With the flag OFF, a cleartext client must be refused rather than silently ignored.
    strict_port = 8972
    strict = make_server(loop, "Strict Probe", allow_unencrypted=False)
    await strict.start_server(port=strict_port, advertise_addresses=[], discover_clients=False)
    refused_hello, _ = await legacy_probe(strict_port, ctrl_id, GUI_HELLO_ROLES, extra=GUI_HELLO_SUPPORT)
    passed &= ok("8e allow_unencrypted=False refuses cleartext", refused_hello is None,
                 "no server/hello (expected)" if refused_hello is None else "ADMITTED - flag is not doing anything")

    for s in (srv, strict):
        try:
            await s.stop_server(); await s.close()
        except Exception:
            pass
    return passed


if __name__ == "__main__":
    try:
        asyncio.run(asyncio.wait_for(main(), timeout=45))
    except asyncio.TimeoutError:
        print("[FAIL] timeout"); sys.exit(2)
