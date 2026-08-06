"""Unit tests for FollowReconciler — auto-route-on-connect + auto-follow ("slave" mode).

Pure logic: a fake aggregator hands back a hand-built MeshView; a fake router and a fake delegate
each record calls SEPARATELY, so a test can assert not just "a route happened" but "it went to the
right place." That distinction matters here specifically: a source_id is only unique within a
unit, so calling the local Router in-process for a REMOTE (leader's) source would silently resolve
against our own same-named source instead — a real bug this file caught on hardware once, and
these fakes are built to catch it again if it regresses.

No event loop plumbing beyond `asyncio.run(reconciler.tick())`, matching test_source_manager.py's
style. Run: `pytest tests/Unit`.
"""

import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "scripts"))

from mesh.follow import FollowReconciler  # noqa: E402
from mesh.model import MeshView, SourceState, UnitSnapshot  # noqa: E402
from mesh.router import RouteError  # noqa: E402


class FakeAggregator:
    def __init__(self, view: MeshView):
        self._view = view

    def view(self) -> MeshView:
        return self._view


class FakeRouter:
    """Stands in for OUR OWN router — only ever correct for a LOCAL target."""

    def __init__(self, fail: bool = False):
        self.calls: list[tuple[str, str]] = []
        self.fail = fail

    async def route_player(self, player_id: str, source_id: str) -> bool:
        self.calls.append((player_id, source_id))
        if self.fail:
            raise RouteError("boom")
        return True


class FakePeer:
    def __init__(self, unit_id: str):
        self.unit_id = unit_id


class FakeDelegate:
    """Stands in for MeshClient.delegate_route — the only correct path to a REMOTE unit's source."""

    def __init__(self, fail: bool = False):
        self.calls: list[tuple[str, str, str]] = []  # (unit_id, source_id, player_id)
        self.fail = fail

    async def __call__(self, peer, source_id: str, player_id: str) -> bool:
        self.calls.append((peer.unit_id, source_id, player_id))
        return not self.fail


def _unit(unit_id, *, sources=None, local_player=None) -> UnitSnapshot:
    return UnitSnapshot(unit_id=unit_id, name=unit_id, host="10.0.0.1", sources=sources or [], local_player=local_player)


def _source(source_id, group_id, *, active=True, player_ids=None) -> SourceState:
    return SourceState(source_id=source_id, group_id=group_id, group_name="", streaming=active,
                        player_ids=player_ids or [], active=active)


def _reconciler(view, settings, *, router=None, delegate=None, unroute_delegate=None, peers=None):
    router = router or FakeRouter()
    delegate = delegate or FakeDelegate()
    unroute_delegate = unroute_delegate or FakeDelegate()
    peers = peers or {}
    r = FollowReconciler(
        FakeAggregator(view), router,
        local_unit_id="unit-A", local_player_id="player-A",
        peer_provider=lambda uid: peers.get(uid),
        delegate=delegate,
        unroute_delegate=unroute_delegate,
        settings_file="/does/not/exist",
    )
    r._read_settings = lambda: settings  # noqa: SLF001 - test seam, same pattern as test_source_manager.py
    return r, router, delegate, unroute_delegate


def _run(r: FollowReconciler) -> None:
    asyncio.run(r.tick())


# -- localActivity (always local — goes through the router) --------------------------------------

def test_local_activity_routes_on_new_connection_edge():
    view = MeshView(units=[_unit("unit-A", sources=[_source("airplay-1", "gA", active=True)])])
    settings = {"autoSwitch": {"localActivity": True, "slave": {"enabled": False, "masterUnitId": None}}}
    r, router, delegate, unroute = _reconciler(view, settings)
    r._prev_active = set()  # airplay-1 was inactive last tick -> this is a NEW connection (rising edge)
    _run(r)
    assert router.calls == [("player-A", "airplay-1")]
    assert delegate.calls == []
    assert r._last_auto_target == ("unit-A", "airplay-1")


def test_local_activity_ignores_already_active_source():
    # The reported bug: a source that was ALREADY active (not a new connection) must not be grabbed,
    # or selecting None while AirPlay still streams instantly re-routes back to it.
    view = MeshView(units=[_unit("unit-A", sources=[_source("airplay-1", "gA", active=True)])])
    settings = {"autoSwitch": {"localActivity": True, "slave": {"enabled": False, "masterUnitId": None}}}
    r, router, delegate, unroute = _reconciler(view, settings)
    r._prev_active = {"airplay-1"}  # already active last tick -> existing, not a new connection
    _run(r)
    assert router.calls == []


def test_local_activity_first_tick_treats_active_as_existing():
    # On the first tick (_prev_active is None) an already-active source is "existing", not new.
    view = MeshView(units=[_unit("unit-A", sources=[_source("airplay-1", "gA", active=True)])])
    settings = {"autoSwitch": {"localActivity": True, "slave": {"enabled": False, "masterUnitId": None}}}
    r, router, delegate, unroute = _reconciler(view, settings)
    assert r._prev_active is None
    _run(r)
    assert router.calls == []


def test_local_activity_noop_when_source_inactive():
    view = MeshView(units=[_unit("unit-A", sources=[_source("airplay-1", "gA", active=False)])])
    settings = {"autoSwitch": {"localActivity": True, "slave": {"enabled": False, "masterUnitId": None}}}
    r, router, delegate, unroute = _reconciler(view, settings)
    _run(r)
    assert router.calls == []


def test_local_activity_noop_when_already_attached():
    view = MeshView(units=[_unit("unit-A", sources=[_source("airplay-1", "gA", active=True, player_ids=["player-A"])])])
    settings = {"autoSwitch": {"localActivity": True, "slave": {"enabled": False, "masterUnitId": None}}}
    r, router, delegate, unroute = _reconciler(view, settings)
    _run(r)
    assert router.calls == []


def test_local_activity_disabled_does_nothing():
    view = MeshView(units=[_unit("unit-A", sources=[_source("airplay-1", "gA", active=True)])])
    settings = {"autoSwitch": {"localActivity": False, "slave": {"enabled": False, "masterUnitId": None}}}
    r, router, delegate, unroute = _reconciler(view, settings)
    _run(r)
    assert router.calls == []


def test_local_activity_route_error_does_not_raise():
    view = MeshView(units=[_unit("unit-A", sources=[_source("airplay-1", "gA", active=True)])])
    settings = {"autoSwitch": {"localActivity": True, "slave": {"enabled": False, "masterUnitId": None}}}
    r, router, delegate, unroute = _reconciler(view, settings, router=FakeRouter(fail=True))
    r._prev_active = set()  # rising edge -> attempt the route (which then fails)
    _run(r)  # must not raise
    assert router.calls == [("player-A", "airplay-1")]
    assert r._last_auto_target is None


# -- follow / slave (always a peer unit — must go through the delegate, never the local router) ---

def _follow_settings(master="unit-B"):
    return {"autoSwitch": {"localActivity": False, "slave": {"enabled": True, "masterUnitId": master}}}


def test_follow_delegates_to_leader_unit_even_when_source_ids_collide():
    # The regression this file exists for: follower has its OWN "airplay-1" too. Routing must go
    # to unit-B specifically, via the delegate — never resolved (and silently mis-attached) against
    # our own same-named local source through the in-process router.
    view = MeshView(units=[
        _unit("unit-A", sources=[_source("airplay-1", "gA", active=False)]),  # idle, same source_id
        _unit("unit-B", sources=[_source("airplay-1", "gB", active=True)],
              local_player={"attached": True, "group_id": "gB", "server_id": "unit-B"}),
    ])
    r, router, delegate, unroute = _reconciler(view, _follow_settings(), peers={"unit-B": FakePeer("unit-B")})
    _run(r)
    assert router.calls == []
    assert delegate.calls == [("unit-B", "airplay-1", "player-A")]
    assert r._last_auto_target == ("unit-B", "airplay-1")


def test_follow_routes_when_idle_and_leader_playing():
    view = MeshView(units=[
        _unit("unit-A"),  # idle: no local_player at all
        _unit("unit-B", sources=[_source("spotify-1", "gB", active=True)],
              local_player={"attached": True, "group_id": "gB", "server_id": "unit-B"}),
    ])
    r, router, delegate, unroute = _reconciler(view, _follow_settings(), peers={"unit-B": FakePeer("unit-B")})
    _run(r)
    assert delegate.calls == [("unit-B", "spotify-1", "player-A")]
    assert router.calls == []
    assert r._last_auto_target == ("unit-B", "spotify-1")


def test_follow_noop_when_leader_peer_not_discoverable():
    # Leader is playing, but its peer entry isn't known right now (mDNS/beacon gap) — no-op, not a crash.
    view = MeshView(units=[
        _unit("unit-A"),
        _unit("unit-B", sources=[_source("spotify-1", "gB", active=True)],
              local_player={"attached": True, "group_id": "gB", "server_id": "unit-B"}),
    ])
    r, router, delegate, unroute = _reconciler(view, _follow_settings(), peers={})
    _run(r)
    assert delegate.calls == []
    assert router.calls == []


def test_follow_noop_when_leader_idle():
    view = MeshView(units=[_unit("unit-A"), _unit("unit-B")])  # neither has a local_player report
    r, router, delegate, unroute = _reconciler(view, _follow_settings(), peers={"unit-B": FakePeer("unit-B")})
    _run(r)
    assert delegate.calls == []


def test_follow_noop_when_leader_attached_to_foreign_server():
    view = MeshView(units=[
        _unit("unit-A"),
        _unit("unit-B", local_player={"attached": True, "group_id": "gX", "server_id": "music-assistant"}),
    ])
    r, router, delegate, unroute = _reconciler(view, _follow_settings(), peers={"unit-B": FakePeer("unit-B")})
    _run(r)
    assert delegate.calls == []


def test_follow_holds_on_manual_override():
    # Follower is actively on its own airplay-1 (manual route), leader is on a different source.
    view = MeshView(units=[
        _unit("unit-A", sources=[_source("airplay-1", "gA", active=True, player_ids=["player-A"])],
              local_player={"attached": True, "group_id": "gA", "server_id": "unit-A"}),
        _unit("unit-B", sources=[_source("spotify-1", "gB", active=True)],
              local_player={"attached": True, "group_id": "gB", "server_id": "unit-B"}),
    ])
    r, router, delegate, unroute = _reconciler(view, _follow_settings(), peers={"unit-B": FakePeer("unit-B")})
    _run(r)
    assert delegate.calls == []  # override holds — never auto-routed anywhere, so nothing to resume


def test_local_override_holds_and_clears_only_on_manual_rejoin():
    # A local override ALWAYS wins over auto-follow (the user's rule). Follower is manually on its own
    # airplay-1 while the leader plays spotify-1 -> override holds; it does NOT get re-followed, even
    # after the follower goes idle. Only manually re-joining the master's stream lifts the override.
    view = MeshView(units=[
        _unit("unit-A", sources=[_source("airplay-1", "gA", active=True, player_ids=["player-A"])],
              local_player={"attached": True, "group_id": "gA", "server_id": "unit-A"}),
        _unit("unit-B", sources=[_source("spotify-1", "gB", active=True)],
              local_player={"attached": True, "group_id": "gB", "server_id": "unit-B"}),
    ])
    r, router, delegate, unroute = _reconciler(view, _follow_settings(), peers={"unit-B": FakePeer("unit-B")})
    _run(r)
    assert delegate.calls == []
    assert r._overridden is True

    # The follower's own source goes idle -> it reads as idle, but the override must STILL hold
    # (previously this is where it wrongly re-followed). No route.
    view.units[0].sources[0].active = False
    view.units[0].sources[0].streaming = False
    view.units[0].local_player["group_id"] = None  # user set it to None
    _run(r)
    assert delegate.calls == []
    assert r._overridden is True

    # User manually re-joins the master's stream -> override clears, following resumes.
    view.units[0].local_player = {"attached": True, "group_id": "gB", "server_id": "unit-B"}
    _run(r)
    assert r._overridden is False
    assert r._last_auto_target == ("unit-B", "spotify-1")


def test_follow_inherits_already_matching_state_without_a_redundant_route():
    # Follower has ALREADY cross-server roamed onto the leader's own group (e.g. right after a
    # restart, _last_auto_target is unset but the roam itself survived) — same unit AND same
    # group_id as the leader, not merely a same-named source of its own. Must not re-route, but
    # must remember it so a later manual move away is still detected as an override.
    view = MeshView(units=[
        # No sources of its own here — the follower's player lives on unit-B, roamed there already.
        _unit("unit-A", local_player={"attached": True, "group_id": "gB", "server_id": "unit-B"}),
        _unit("unit-B", sources=[_source("spotify-1", "gB", active=True, player_ids=["player-A"])],
              local_player={"attached": True, "group_id": "gB", "server_id": "unit-B"}),
    ])
    r, router, delegate, unroute = _reconciler(view, _follow_settings(), peers={"unit-B": FakePeer("unit-B")})
    assert r._last_auto_target is None
    _run(r)
    assert delegate.calls == []
    assert router.calls == []
    assert r._last_auto_target == ("unit-B", "spotify-1")


def test_follow_targets_leader_unit_not_own_same_named_source():
    # source_id is unique only within a unit: the follower has its OWN "airplay-1" too. When it does
    # follow, the route must be delegated to the LEADER's unit-B, never resolved against our own
    # same-named local source. Here the follower is idle and has never overridden, so it follows.
    view = MeshView(units=[
        _unit("unit-A", sources=[_source("airplay-1", "gA", active=False)]),  # idle, same source_id, no local_player
        _unit("unit-B", sources=[_source("airplay-1", "gB", active=True)],
              local_player={"attached": True, "group_id": "gB", "server_id": "unit-B"}),
    ])
    r, router, delegate, unroute = _reconciler(view, _follow_settings(), peers={"unit-B": FakePeer("unit-B")})
    _run(r)
    assert router.calls == []  # never through the in-process router (would hit our own airplay-1)
    assert delegate.calls == [("unit-B", "airplay-1", "player-A")]
    assert r._last_auto_target == ("unit-B", "airplay-1")


def test_follow_delegate_failure_does_not_raise_or_update_target():
    view = MeshView(units=[
        _unit("unit-A"),
        _unit("unit-B", sources=[_source("spotify-1", "gB", active=True)],
              local_player={"attached": True, "group_id": "gB", "server_id": "unit-B"}),
    ])
    r, router, delegate, unroute = _reconciler(
        view, _follow_settings(), peers={"unit-B": FakePeer("unit-B")}, delegate=FakeDelegate(fail=True),
    )
    _run(r)  # must not raise
    assert delegate.calls == [("unit-B", "spotify-1", "player-A")]
    assert r._last_auto_target is None


# -- follow the leader INTO idle -----------------------------------------------------------------

def test_follow_unroutes_when_leader_goes_idle():
    # The reported scenario: leader's own PLAYER leaves its source (switched to None) while that
    # source is still active — the follower is still actively grouped on it. The follower must
    # unroute (via the unroute delegate, keyed on the leader's unit) to follow the leader into idle.
    view = MeshView(units=[
        _unit("unit-A", local_player={"attached": True, "group_id": "gB", "server_id": "unit-B"}),  # follower on gB
        _unit("unit-B", sources=[_source("spotify-1", "gB", active=True)],  # source STILL active
              local_player={"attached": True, "group_id": None, "server_id": "unit-B"}),  # leader -> None
    ])
    r, router, delegate, unroute = _reconciler(view, _follow_settings(), peers={"unit-B": FakePeer("unit-B")})
    r._last_auto_target = ("unit-B", "spotify-1")  # we had been following it
    _run(r)
    assert unroute.calls == [("unit-B", "spotify-1", "player-A")]
    assert delegate.calls == []
    assert r._last_auto_target is None


def test_master_idle_lifts_override_for_idle_follower_and_refollows():
    # Follower opted out to None while the master plays; the master then goes idle. Because the
    # follower is itself idle/none, the master-idle reset lifts the override, and the master's next
    # stream is followed again — no manual re-join needed.
    view = MeshView(units=[
        _unit("unit-A", local_player={"attached": True, "group_id": None, "server_id": "unit-A"}),  # None
        _unit("unit-B", sources=[_source("spotify-1", "gB", active=True)],
              local_player={"attached": True, "group_id": "gB", "server_id": "unit-B"}),
    ])
    r, router, delegate, unroute = _reconciler(view, _follow_settings(), peers={"unit-B": FakePeer("unit-B")})
    r._last_auto_target = ("unit-B", "spotify-1")  # had followed before the user set None

    _run(r)  # master playing, follower None -> override held
    assert delegate.calls == [] and r._overridden is True

    view.units[1].local_player["group_id"] = None  # master -> idle/none
    view.units[1].sources[0].active = False
    _run(r)
    assert r._overridden is False  # reset lifted the override (follower was idle)

    view.units[1].local_player["group_id"] = "gB"  # master starts a new stream
    view.units[1].sources[0].active = True
    _run(r)
    assert delegate.calls == [("unit-B", "spotify-1", "player-A")]  # follows again


def test_master_idle_keeps_override_when_follower_on_another_source():
    # Follower manually on its OWN active airplay-1 (override), master then goes idle. Because the
    # follower is NOT idle, the override is kept — it is not yanked onto the master's next stream.
    view = MeshView(units=[
        _unit("unit-A", sources=[_source("airplay-1", "gA", active=True, player_ids=["player-A"])],
              local_player={"attached": True, "group_id": "gA", "server_id": "unit-A"}),
        _unit("unit-B", local_player={"attached": True, "group_id": None, "server_id": "unit-B"}),  # idle
    ])
    r, router, delegate, unroute = _reconciler(view, _follow_settings(), peers={"unit-B": FakePeer("unit-B")})
    r._overridden = True
    r._last_auto_target = ("unit-B", "spotify-1")  # we followed spotify before; user moved us to airplay
    _run(r)
    assert unroute.calls == []  # left on airplay
    assert r._overridden is True  # override kept across the master reset


def test_follow_does_not_unroute_a_manual_override_when_leader_idle():
    # Follower is on its OWN active source (manual override, not what we last auto-followed). Leader
    # idle. We must NOT unroute the user's manual choice.
    view = MeshView(units=[
        _unit("unit-A", sources=[_source("airplay-1", "gA", active=True, player_ids=["player-A"])],
              local_player={"attached": True, "group_id": "gA", "server_id": "unit-A"}),
        _unit("unit-B", local_player={"attached": True, "group_id": "gX", "server_id": "unit-B"}),  # no active source
    ])
    r, router, delegate, unroute = _reconciler(view, _follow_settings(), peers={"unit-B": FakePeer("unit-B")})
    r._last_auto_target = ("unit-B", "spotify-1")  # we followed spotify before; user has since moved us
    _run(r)
    assert unroute.calls == []  # current target (unit-A/airplay-1) != last auto target -> leave it be


# -- following a leader that has no speaker of its own ---------------------------------------------
#
# An ingest-only unit exists to BE followed: it encodes AirPlay/Spotify and other rooms render it.
# But it can never self-report a player, and `_player_status` reads a missing self-report as "idle",
# which the master-idle branch treats as "the session ended" — so before `_leader_status` existed,
# such a leader unrouted its own followers within one tick of them joining.


def _headless(unit_id, *sources) -> UnitSnapshot:
    """A unit that ingests but has no speaker: has_player False, local_player permanently None."""
    return UnitSnapshot(unit_id=unit_id, name=unit_id, host="10.0.0.9",
                        sources=list(sources), local_player=None, has_player=False)


def _slave_settings(master="unit-B"):
    return {"autoSwitch": {"localActivity": False, "slave": {"enabled": True, "masterUnitId": master}}}


def test_a_playerless_leader_is_followed_rather_than_unrouting_us():
    """The regression. Before the fix this produced an UNROUTE on the very first tick."""
    view = MeshView(units=[
        _unit("unit-A", local_player=None),
        _headless("unit-B", _source("airplay-1", "gB", active=True)),
    ])
    r, router, delegate, unroute = _reconciler(
        view, _slave_settings(), peers={"unit-B": FakePeer("unit-B")}
    )
    _run(r)

    # Delegated, never routed locally: a source_id is only unique within a unit, and unit-A could
    # easily have its own "airplay-1".
    assert delegate.calls == [("unit-B", "airplay-1", "player-A")]
    assert router.calls == []
    assert unroute.calls == []
    assert r._last_auto_target == ("unit-B", "airplay-1")


def test_a_playerless_leader_going_quiet_resets_the_relationship():
    """The other half: leading must end cleanly, not just start.

    No unroute happens here, and that is correct rather than a gap. The follower is attached to the
    leader's group, so when that source goes quiet BOTH read as idle off the same SourceState — the
    audio has already stopped, and the existing reset branch simply expires the opt-out so the
    leader's next stream is followed again without a manual re-join. The unroute path is for a
    follower still actively playing something else.
    """
    view = MeshView(units=[
        _unit("unit-A", local_player={"attached": True, "group_id": "gB", "server_id": "unit-B"}),
        _headless("unit-B", _source("airplay-1", "gB", active=False)),
    ])
    r, router, delegate, unroute = _reconciler(
        view, _slave_settings(), peers={"unit-B": FakePeer("unit-B")}
    )
    r._last_auto_target = ("unit-B", "airplay-1")
    _run(r)

    assert (unroute.calls, delegate.calls, router.calls) == ([], [], [])
    assert r._last_auto_target is None
    assert r._overridden is False


def test_a_playerless_leader_switching_source_behaves_like_any_other_leader():
    """Pins PARITY, not desirability.

    When a leader moves to a second source and the follower's old source goes quiet, the follower's
    current_target becomes None — which the override guard reads as "the user moved us" — so it stops
    following and does NOT move to the new source. That is pre-existing behaviour, identical for a
    leader WITH a speaker (verified directly), and this test exists so that a headless leader is not
    accidentally given different semantics while the shared question is decided. See docs/CLAUDE.md.
    """
    view = MeshView(units=[
        _unit("unit-A", local_player={"attached": True, "group_id": "gB1", "server_id": "unit-B"}),
        _headless(
            "unit-B",
            _source("airplay-1", "gB1", active=False),
            _source("spotify-1", "gB2", active=True),
        ),
    ])
    r, _router, delegate, unroute = _reconciler(
        view, _slave_settings(), peers={"unit-B": FakePeer("unit-B")}
    )
    r._last_auto_target = ("unit-B", "airplay-1")
    _run(r)

    assert (delegate.calls, unroute.calls) == ([], [])
    assert r._overridden is True


def test_a_playerless_leader_with_no_active_source_routes_nothing():
    view = MeshView(units=[
        _unit("unit-A", local_player=None),
        _headless("unit-B", _source("airplay-1", "gB", active=False)),
    ])
    r, router, delegate, unroute = _reconciler(
        view, _slave_settings(), peers={"unit-B": FakePeer("unit-B")}
    )
    _run(r)

    assert (delegate.calls, router.calls, unroute.calls) == ([], [], [])
    assert r._last_auto_target is None


def test_two_concurrent_sources_pick_the_most_attended_one():
    """Every follower must reach the same answer independently, with no coordination."""
    view = MeshView(units=[
        _unit("unit-A", local_player=None),
        _headless(
            "unit-B",
            _source("airplay-1", "gB1", active=True, player_ids=[]),
            _source("spotify-1", "gB2", active=True, player_ids=["player-C", "player-D"]),
        ),
    ])
    r, _router, delegate, _unroute = _reconciler(
        view, _slave_settings(), peers={"unit-B": FakePeer("unit-B")}
    )
    _run(r)
    assert delegate.calls == [("unit-B", "spotify-1", "player-A")]


def test_the_choice_does_not_oscillate_between_ticks():
    """A tie must break deterministically, or two followers land on different sources."""
    view = MeshView(units=[
        _unit("unit-A", local_player=None),
        _headless(
            "unit-B",
            _source("spotify-1", "gB2", active=True, player_ids=[]),
            _source("airplay-1", "gB1", active=True, player_ids=[]),
        ),
    ])
    picks = []
    for _ in range(3):
        r, _router, delegate, _unroute = _reconciler(
            view, _slave_settings(), peers={"unit-B": FakePeer("unit-B")}
        )
        _run(r)
        picks.append(delegate.calls)
    assert picks == [[("unit-B", "airplay-1", "player-A")]] * 3  # source_id order, every time


def test_has_player_wins_over_a_stale_self_report():
    """A unit that just became playerless may still carry an old local_player in a cached snapshot."""
    stale = UnitSnapshot(
        unit_id="unit-B", name="unit-B", host="10.0.0.9", has_player=False, local_player=None,
        sources=[_source("airplay-1", "gB", active=True)],
    )
    stale.local_player = {"attached": True, "group_id": "gOLD", "server_id": "unit-B"}
    view = MeshView(units=[_unit("unit-A", local_player=None), stale])
    r, _router, delegate, _unroute = _reconciler(
        view, _slave_settings(), peers={"unit-B": FakePeer("unit-B")}
    )
    _run(r)
    assert delegate.calls == [("unit-B", "airplay-1", "player-A")]  # sources, not the stale report


def test_a_leader_with_a_speaker_still_uses_its_self_report():
    """The existing path must be untouched — a leader that has roamed is still followed correctly."""
    view = MeshView(units=[
        _unit("unit-A", local_player=None),
        _unit("unit-B", local_player={"attached": True, "group_id": "gC", "server_id": "unit-C"}),
        _unit("unit-C", sources=[_source("spotify-1", "gC", active=True)]),
    ])
    r, _router, delegate, _unroute = _reconciler(
        view, _slave_settings(), peers={"unit-C": FakePeer("unit-C")}
    )
    _run(r)
    assert delegate.calls == [("unit-C", "spotify-1", "player-A")]  # where the leader ACTUALLY is


# -- a playerless unit never follows and never auto-routes ------------------------------------------


def _playerless_reconciler(view, settings):
    r = FollowReconciler(
        FakeAggregator(view), FakeRouter(),
        local_unit_id="unit-A", local_player_id=None,
        peer_provider=lambda uid: None,
        delegate=FakeDelegate(),
        unroute_delegate=FakeDelegate(),
        settings_file="/does/not/exist",
    )
    r._read_settings = lambda: settings
    return r


def test_a_playerless_unit_does_nothing_with_both_modes_on():
    """Both paths end in routing a player id that resolves to nothing.

    localActivity raised a RouteError with a stack trace per source activation; slave mode re-routed
    every tick forever, because a failed route never sets _last_auto_target and the override guard
    then compares None against None.
    """
    view = MeshView(units=[
        _headless("unit-A", _source("airplay-1", "gA", active=True)),
        _unit("unit-B", sources=[_source("spotify-1", "gB", active=True)]),
    ])
    settings = {"autoSwitch": {"localActivity": True, "slave": {"enabled": True, "masterUnitId": "unit-B"}}}
    r = _playerless_reconciler(view, settings)
    r._prev_active = set()

    for _ in range(3):
        _run(r)

    assert r._last_auto_target is None
    assert r._overridden is False


# -- localActivity vs slave: both enabled at once ----------------------------------------------------
#
# Real configuration, from .2.10 on 2026-08-06: localActivity ON and slave ON with master .11.
# Both units have a source called `spotify-1`. The reported symptom was the visualizer dropping to
# zero and recovering — which is what a listener sees when the player is re-routed, because every
# attach calls SourceFeeder.refresh_stream() and that replaces the stream for the whole group.


def _both_modes_view():
    return MeshView(units=[
        _unit("unit-A", sources=[_source("spotify-1", "gA", active=True)], local_player=None),
        _unit("unit-B", sources=[_source("spotify-1", "gB", active=True)],
              local_player={"attached": True, "group_id": "gB", "server_id": "unit-B"}),
    ])


BOTH_MODES = {"autoSwitch": {"localActivity": True, "slave": {"enabled": True, "masterUnitId": "unit-B"}}}


def test_a_local_grab_is_not_undone_by_slave_mode_on_the_next_tick():
    """The ping-pong. PlaybackTab's own copy promises "Local connections always take priority"."""
    view = _both_modes_view()
    r, router, delegate, _unroute = _reconciler(view, BOTH_MODES, peers={"unit-B": FakePeer("unit-B")})
    r._prev_active = set()  # our own spotify-1 just went active

    _run(r)
    assert router.calls == [("player-A", "spotify-1")]  # localActivity grabbed it locally

    # The view catches up: our player is now on our own source.
    view.units[0].sources[0].player_ids = ["player-A"]
    view.units[0].local_player = {"attached": True, "group_id": "gA", "server_id": "unit-A"}
    for _ in range(4):
        _run(r)

    # Nothing delegated: slave mode must NOT pull it back to the master. Before the fix this
    # produced a route on the very next tick, and every hop is a discontinuity for every listener.
    assert delegate.calls == []
    assert router.calls == [("player-A", "spotify-1")]  # and no repeat locally either


def test_slave_still_follows_when_local_activity_has_not_fired():
    """The fix must not disable slave mode — only stop it undoing a local grab."""
    view = _both_modes_view()
    view.units[0].sources[0].active = False  # nothing local going on
    r, router, delegate, _unroute = _reconciler(view, BOTH_MODES, peers={"unit-B": FakePeer("unit-B")})
    r._prev_active = set()

    _run(r)
    assert delegate.calls == [("unit-B", "spotify-1", "player-A")]
    assert router.calls == []


def test_the_local_override_still_clears_when_both_go_idle():
    """Otherwise a unit that once grabbed locally would never follow its master again."""
    view = _both_modes_view()
    r, _router, delegate, _unroute = _reconciler(view, BOTH_MODES, peers={"unit-B": FakePeer("unit-B")})
    r._prev_active = set()
    _run(r)
    assert r._overridden is True

    # Both go quiet — the documented reset point.
    view.units[0].sources[0].active = False
    view.units[0].local_player = None
    view.units[1].sources[0].active = False
    view.units[1].local_player = None
    _run(r)

    assert r._overridden is False
    assert r._last_auto_target is None
