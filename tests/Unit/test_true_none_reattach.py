"""Integration-style test: the handoff between SourceFeeder._go_idle (true none,
docs/ROUTING-MODEL.md rule 1) and FollowReconciler's localActivity, exercising the REAL _go_idle
and the REAL FollowReconciler.tick() together — not just each in isolation, which is all
test_sendspin_server.py and test_follow_reconciler.py cover on their own.

Confirmed live on hardware (unit-7204, 2026-08-12, see docs/PHASE-HISTORY.md): a player detached by
_go_idle is picked back up by localActivity the same second its source reactivates, while a
cross-routed player on a different unit stays down (already covered separately by
test_follow_reconciler.py's localActivity-only-touches-my_unit.sources scoping — nothing here
duplicates that). This file exists so a future change to either module can't silently break that
handoff without a red test; it was proven once by hand and had no automated guard before this.

local_player is deliberately hand-built here, not derived from PlumSendspinServer.snapshot(): in the
real system it is a separate self-report the PLAYER process posts to mesh/api.py, never something
sendspin_server.py itself produces — so reconstructing it by hand from the real group's post-_go_idle
state is the accurate way to bridge the two subsystems, not a shortcut.

Run: `pytest tests/Unit`.
"""

import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "scripts"))

import sendspin_server as ss  # noqa: E402
from mesh.follow import FollowReconciler  # noqa: E402
from mesh.model import MeshView, SourceState, UnitSnapshot  # noqa: E402

UNIT_ID = "unit-7204"
PLAYER_ID = "player-7204"
SOURCE_ID = "airplay-1"


# -- sendspin_server side: same shape as test_sendspin_server.py's fakes --------------------------


class FakeGroup:
    def __init__(self, group_id):
        self.group_id = group_id
        self.group_name = group_id
        self.calls = []
        self.members = []

    @property
    def clients(self):
        return list(self.members)

    async def stop(self):
        self.calls.append("stop")

    async def remove_client(self, client):
        self.calls.append(("remove", client.client_id))
        if client in self.members:
            self.members.remove(client)
        client.group = None


class FakeClient:
    def __init__(self, client_id, group=None, roles=()):
        self.client_id = client_id
        self.group = group
        self.is_connected = True
        self.negotiated_roles = list(roles)


# -- mesh.follow side: same shape as test_follow_reconciler.py's fakes ----------------------------


class FakeAggregator:
    def __init__(self, view):
        self._view = view

    def view(self):
        return self._view


class FakeRouter:
    def __init__(self):
        self.calls = []

    async def route_player(self, player_id, source_id):
        self.calls.append((player_id, source_id))
        return True


def _reconciler(view, settings):
    router = FakeRouter()
    r = FollowReconciler(
        FakeAggregator(view), router,
        local_unit_id=UNIT_ID, local_player_id=PLAYER_ID,
        peer_provider=lambda uid: None,
        delegate=lambda *a: None,  # localActivity for this unit's own player never reaches it
        settings_file="/does/not/exist",
    )
    r._read_settings = lambda: settings  # noqa: SLF001 - test seam, matches test_follow_reconciler.py
    return r, router


def _went_idle_via_go_idle():
    """Build a real SourceHandle whose player was actually attached, then really detached via the
    real _go_idle -- not a hand-constructed 'idle' fixture."""
    group = FakeGroup(group_id="g-" + SOURCE_ID)
    feeder = ss.SourceFeeder(SOURCE_ID, "/tmp/does-not-exist-fifo", group)
    handle = ss.SourceHandle(SOURCE_ID, group, feeder)
    player = FakeClient(PLAYER_ID, group=group, roles=["player@v1"])
    group.members = [player]
    feeder._last_data_at = 1.0  # was active

    asyncio.run(feeder._go_idle("session end"))
    assert group.calls == ["stop", ("remove", PLAYER_ID)]  # sanity: the detach really happened
    return handle


def _source_state(handle):
    """The same shape PlumSendspinServer.snapshot() derives from a group's real, current membership."""
    player_ids = [c.client_id for c in handle.group.clients if not c.client_id.startswith(ss.ANCHOR_PREFIX)]
    return SourceState(source_id=handle.source_id, group_id=handle.group.group_id, group_name="",
                        streaming=handle.feeder.is_active, player_ids=player_ids, active=handle.feeder.is_active)


def _snapshot(handle):
    # Post-detach, aiosendspin hands the player a fresh solo group_id -- never the source's. See
    # sendspin_server.py's _go_idle docstring for why that's the real library behavior, not a guess.
    local_player = {"attached": True, "group_id": "solo-after-detach", "server_id": UNIT_ID}
    return UnitSnapshot(unit_id=UNIT_ID, name=UNIT_ID, host="10.0.0.1",
                        sources=[_source_state(handle)], local_player=local_player)


def test_a_player_detached_by_go_idle_reads_as_idle_to_follow():
    """The state-interpretation half: _go_idle's real output must be what _player_status calls idle."""
    handle = _went_idle_via_go_idle()
    view = MeshView(units=[_snapshot(handle)])

    idle, target = FollowReconciler._player_status(view, UNIT_ID)

    assert idle is True
    assert target is None


def test_localactivity_reattaches_after_go_idle_when_the_source_comes_back():
    """The full handoff, mirroring the hardware log timeline captured on unit-7204 (2026-08-12):
    a source going active -> _go_idle detaching on idle -> the source going active again ->
    localActivity re-attaching, without anything manually re-routing it.
    """
    handle = _went_idle_via_go_idle()
    settings = {"autoSwitch": {"localActivity": True, "slave": {"enabled": False, "masterUnitId": None}}}
    view = MeshView(units=[_snapshot(handle)])
    r, router = _reconciler(view, settings)

    asyncio.run(r.tick())
    assert router.calls == []  # still idle: nothing to do yet

    # The source comes back -- the same transition _go_idle's caller (SourceFeeder._pump) makes when
    # a sender reconnects.
    view.units[0].sources[0].active = True
    view.units[0].sources[0].streaming = True

    asyncio.run(r.tick())
    assert router.calls == [(PLAYER_ID, SOURCE_ID)]  # localActivity fired, same as on the rig
