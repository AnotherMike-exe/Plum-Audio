"""Unit tests for the in-process Sendspin server: stream membership, routing and grouping.

The headline case is `SourceFeeder.refresh_stream` and its caller `attach_player`. Adding an
already-connected player to a group does NOT put it in a stream that is already running — membership
is fixed at `start_stream()` — so the player sits in the group, in the GUI, at the right volume, and
silent, with nothing in any log. That is the highest-profile bug in this repo and it had no
regression guard. A roam masks it (a reconnect gets the stream for free), so a passing roam test is
not evidence; these tests pin the intra-server path specifically.

Pure-logic: fakes stand in for SendspinGroup/SendspinServer/PushStream, so no sockets, no audio and
no aiosendspin server is started. Run: `pytest tests/Unit`.
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend" / "scripts"))

import sendspin_server as ss  # noqa: E402


# --- fakes ------------------------------------------------------------------


class FakePushStream:
    def __init__(self):
        self.is_stopped = False
        self.live_source = None

    def set_live_source(self, live):
        self.live_source = live


class FakeGroup:
    """Records membership churn and stream acquisition in call order."""

    def __init__(self, group_id="g" * 8):
        self.group_id = group_id
        self.group_name = group_id
        self.calls = []
        self.streams = []
        self.members = []
        self.has_active_stream = False
        self._roles = {}

    @property
    def clients(self):
        return list(self.members)

    def start_stream(self, *_a, **_kw):
        self.calls.append("start_stream")
        ps = FakePushStream()
        self.streams.append(ps)
        return ps

    def stop_stream(self):
        self.calls.append("stop_stream")

    async def add_client(self, client):
        self.calls.append(("add", client.client_id))
        self.members.append(client)
        client.group = self

    async def remove_client(self, client):
        self.calls.append(("remove", client.client_id))
        if client in self.members:
            self.members.remove(client)
        client.group = None

    def group_role(self, family):
        return self._roles.get(family)


class FakeClient:
    def __init__(self, client_id, group=None, connected=True, roles=()):
        self.client_id = client_id
        self.group = group
        self.is_connected = connected
        self.negotiated_roles = list(roles)

    def roles_by_family(self, family):
        return [r for r in self.negotiated_roles if str(r).startswith(family)]


class FakeServer:
    def __init__(self):
        self.clients = {}

    def add(self, client):
        self.clients[client.client_id] = client
        return client

    def get_client(self, client_id):
        return self.clients.get(client_id)

    def get_or_create_client(self, client_id):
        if client_id not in self.clients:
            self.clients[client_id] = FakeClient(client_id)
        return self.clients[client_id]


def make_feeder(group=None, ps=None):
    feeder = ss.SourceFeeder("src1", "/tmp/does-not-exist-fifo", group or FakeGroup())
    feeder.ps = ps
    return feeder


def make_unit(*source_ids):
    """A PlumSendspinServer with a FakeServer and one already-started source per id."""
    unit = ss.PlumSendspinServer("unitA", "A")
    unit.server = FakeServer()
    for source_id in source_ids:
        group = FakeGroup(group_id=f"grp-{source_id}")
        feeder = make_feeder(group=group)
        unit.sources[source_id] = ss.SourceHandle(source_id, group, feeder, name=source_id)
        if unit._primary_source is None:
            unit._primary_source = source_id
    return unit


# --- SourceFeeder.refresh_stream -------------------------------------------


def test_refresh_stream_reacquires_when_a_stream_is_live():
    """The guard for the silent-player bug: a live stream must be replaced, not left alone."""
    group = FakeGroup()
    feeder = make_feeder(group=group, ps=FakePushStream())
    feeder.refresh_stream()
    assert group.calls == ["start_stream"]
    assert feeder.ps is group.streams[-1]


def test_refresh_stream_marks_the_new_stream_as_a_live_source():
    """Re-acquisition must go through _acquire_stream; a raw start_stream() drops set_live_source."""
    group = FakeGroup()
    feeder = make_feeder(group=group, ps=FakePushStream())
    feeder.refresh_stream()
    assert feeder.ps.live_source is True


def test_refresh_stream_is_a_noop_when_nothing_is_playing():
    """No stream yet: the next chunk starts one that includes everyone, so don't churn."""
    group = FakeGroup()
    feeder = make_feeder(group=group, ps=None)
    feeder.refresh_stream()
    assert group.calls == []


def test_refresh_stream_is_a_noop_on_a_stopped_stream():
    group = FakeGroup()
    stopped = FakePushStream()
    stopped.is_stopped = True
    feeder = make_feeder(group=group, ps=stopped)
    feeder.refresh_stream()
    assert group.calls == []
    assert feeder.ps is stopped


def test_is_active_mirrors_what_is_announced_on_the_wire():
    feeder = make_feeder()
    assert feeder.is_active is False  # idle == announced stopped
    feeder._last_data_at = 1.0
    assert feeder.is_active is True


# --- attach_player: the caller that must not lose the refresh ---------------


def test_attach_player_refreshes_the_stream_after_adding():
    """add_client alone leaves an already-connected player in the group but out of the stream."""
    unit = make_unit("airplay-1")
    handle = unit.sources["airplay-1"]
    handle.feeder.ps = FakePushStream()  # a stream is live
    unit.server.add(FakeClient("player-1", group=None))

    asyncio.run(unit.attach_player("airplay-1", "player-1"))

    assert handle.group.calls == [("add", "player-1"), "start_stream"]


def test_attach_player_removes_from_the_old_group_before_adding():
    """A bare add_client would stop the player's CURRENT source group — remove first."""
    unit = make_unit("airplay-1", "spotify-1")
    old = unit.sources["spotify-1"].group
    dest = unit.sources["airplay-1"]
    dest.feeder.ps = FakePushStream()
    player = unit.server.add(FakeClient("player-1", group=old))

    asyncio.run(unit.attach_player("airplay-1", "player-1"))

    assert old.calls == [("remove", "player-1")]
    assert dest.group.calls == [("add", "player-1"), "start_stream"]
    assert player.group is dest.group


def test_attach_player_is_idempotent_for_a_player_already_on_that_source():
    """Re-attaching must not churn the stream for everyone else already listening."""
    unit = make_unit("airplay-1")
    handle = unit.sources["airplay-1"]
    handle.feeder.ps = FakePushStream()
    player = unit.server.add(FakeClient("player-1"))
    player.group = handle.group

    asyncio.run(unit.attach_player("airplay-1", "player-1"))

    assert handle.group.calls == []


def test_attach_player_rejects_an_unknown_source():
    unit = make_unit("airplay-1")
    with pytest.raises(KeyError):
        asyncio.run(unit.attach_player("nope-9", "player-1"))


def test_detach_player_removes_without_touching_the_stream():
    """Leaving is free: the remaining members keep the stream they already have."""
    unit = make_unit("airplay-1")
    handle = unit.sources["airplay-1"]
    handle.feeder.ps = FakePushStream()
    player = unit.server.add(FakeClient("player-1", group=handle.group))
    handle.group.members.append(player)

    asyncio.run(unit.detach_player("airplay-1", "player-1"))

    assert handle.group.calls == [("remove", "player-1")]


def test_detach_player_on_an_unknown_source_is_silent():
    unit = make_unit("airplay-1")
    asyncio.run(unit.detach_player("nope-9", "player-1"))  # must not raise


# --- source lifecycle -------------------------------------------------------


def test_stop_source_hands_the_primary_on_to_a_survivor():
    """A dead _primary_source silently un-groups every controller with no ctrl: hint."""
    unit = make_unit("airplay-1", "spotify-1")
    assert unit._primary_source == "airplay-1"

    asyncio.run(unit.stop_source("airplay-1"))

    assert unit._primary_source == "spotify-1"


def test_stop_source_clears_the_primary_when_the_last_source_goes():
    unit = make_unit("airplay-1")
    asyncio.run(unit.stop_source("airplay-1"))
    assert unit._primary_source is None


def test_stop_source_leaves_the_primary_alone_when_another_source_stops():
    unit = make_unit("airplay-1", "spotify-1")
    asyncio.run(unit.stop_source("spotify-1"))
    assert unit._primary_source == "airplay-1"


def test_stop_source_stops_the_stream_and_forgets_the_handle():
    unit = make_unit("airplay-1")
    group = unit.sources["airplay-1"].group
    asyncio.run(unit.stop_source("airplay-1"))
    assert "airplay-1" not in unit.sources
    assert "stop_stream" in group.calls


def test_stop_source_is_idempotent():
    unit = make_unit("airplay-1")
    asyncio.run(unit.stop_source("airplay-1"))
    asyncio.run(unit.stop_source("airplay-1"))  # must not raise


def test_set_source_name_renames_in_place():
    unit = make_unit("airplay-1")
    unit.set_source_name("airplay-1", "Kitchen")
    assert unit.sources["airplay-1"].name == "Kitchen"


def test_set_source_name_ignores_an_empty_name_and_an_unknown_source():
    unit = make_unit("airplay-1")
    unit.set_source_name("airplay-1", "")
    assert unit.sources["airplay-1"].name == "airplay-1"
    unit.set_source_name("nope-9", "Kitchen")  # must not raise


# --- controller grouping ----------------------------------------------------


def test_requested_source_reads_the_ctrl_client_id():
    unit = make_unit("airplay-1")
    assert unit._requested_source("ctrl:airplay-1:abc123") == "airplay-1"


def test_requested_source_rejects_a_hint_for_a_source_that_is_gone():
    """Falls back to the primary rather than resolving a source that no longer exists."""
    unit = make_unit("airplay-1")
    assert unit._requested_source("ctrl:spotify-9:abc123") is None


def test_requested_source_ignores_a_non_controller_client_id():
    unit = make_unit("airplay-1")
    assert unit._requested_source("player-1") is None
    assert unit._requested_source(ss.ANCHOR_PREFIX + "airplay-1") is None


def test_controller_grouping_honours_the_source_hint():
    unit = make_unit("airplay-1", "spotify-1")
    unit.server.add(FakeClient("ctrl:spotify-1:n1"))

    asyncio.run(unit._maybe_group_controller("ctrl:spotify-1:n1"))

    assert unit.sources["spotify-1"].group.calls == [("add", "ctrl:spotify-1:n1")]
    assert unit.sources["airplay-1"].group.calls == []


def test_controller_grouping_falls_back_to_the_primary_source():
    unit = make_unit("airplay-1", "spotify-1")
    unit.server.add(FakeClient("some-other-client"))

    asyncio.run(unit._maybe_group_controller("some-other-client"))

    assert unit.sources["airplay-1"].group.calls == [("add", "some-other-client")]


def test_controller_grouping_follows_the_primary_after_it_moves():
    """The pairing that item 9 of the backlog was about: stop the first source, keep grouping."""
    unit = make_unit("airplay-1", "spotify-1")
    asyncio.run(unit.stop_source("airplay-1"))
    unit.server.add(FakeClient("some-other-client"))

    asyncio.run(unit._maybe_group_controller("some-other-client"))

    assert unit.sources["spotify-1"].group.calls == [("add", "some-other-client")]


def test_controller_grouping_never_regroups_a_player():
    """The mesh orchestrator owns player routing; regrouping one here would fight it."""
    unit = make_unit("airplay-1")
    unit.server.add(FakeClient("player-1", roles=["player@v1"]))

    asyncio.run(unit._maybe_group_controller("player-1"))

    assert unit.sources["airplay-1"].group.calls == []


def test_controller_grouping_skips_group_anchors():
    unit = make_unit("airplay-1")
    anchor_id = ss.ANCHOR_PREFIX + "airplay-1"
    unit.server.add(FakeClient(anchor_id))

    asyncio.run(unit._maybe_group_controller(anchor_id))

    assert unit.sources["airplay-1"].group.calls == []


def test_controller_grouping_skips_a_disconnected_client():
    unit = make_unit("airplay-1")
    unit.server.add(FakeClient("ctrl:airplay-1:n1", connected=False))

    asyncio.run(unit._maybe_group_controller("ctrl:airplay-1:n1"))

    assert unit.sources["airplay-1"].group.calls == []


def test_controller_grouping_is_idempotent():
    unit = make_unit("airplay-1")
    group = unit.sources["airplay-1"].group
    unit.server.add(FakeClient("ctrl:airplay-1:n1", group=group))

    asyncio.run(unit._maybe_group_controller("ctrl:airplay-1:n1"))

    assert group.calls == []


def test_controller_grouping_is_a_noop_with_no_sources():
    unit = make_unit()
    unit.server.add(FakeClient("ctrl:airplay-1:n1"))
    asyncio.run(unit._maybe_group_controller("ctrl:airplay-1:n1"))  # must not raise


# -- headless: the unit that has no speaker ----------------------------------------------------------


def test_local_player_config_derives_the_pair_by_default():
    player_id, url = ss.local_player_config({}, "unit-210")
    assert player_id == "unit-210-player"
    assert url == "ws://127.0.0.1:8928/sendspin"


def test_local_player_config_honours_explicit_values():
    env = {"PLUM_LOCAL_PLAYER_ID": "player-210", "PLUM_LOCAL_PLAYER_URL": "ws://10.0.0.9:8928/sendspin"}
    assert ss.local_player_config(env, "unit-210") == ("player-210", "ws://10.0.0.9:8928/sendspin")


def test_the_operator_flag_removes_the_local_player():
    """deploy.sh writes this from a units.conf row whose DAC column is `none`."""
    env = {"PLUM_PLAYER_ENABLED": "0", "PLUM_LOCAL_PLAYER_ID": "player-210"}
    assert ss.local_player_config(env, "unit-210") == (None, None)


def test_an_empty_url_also_removes_the_local_player():
    """The older way of saying it, still used on the dev rig — must keep working."""
    assert ss.local_player_config({"PLUM_LOCAL_PLAYER_URL": ""}, "unit-210") == (None, None)


def test_the_flag_wins_over_an_explicit_url():
    env = {"PLUM_PLAYER_ENABLED": "0", "PLUM_LOCAL_PLAYER_URL": "ws://10.0.0.9:8928/sendspin"}
    assert ss.local_player_config(env, "unit-210") == (None, None)


def test_a_unit_reports_whether_it_has_a_speaker_at_all():
    """Not the same question as `players == []`, which is also true for a moment at every boot."""
    assert ss.PlumSendspinServer("unitA", "A").snapshot().has_player is True
    assert ss.PlumSendspinServer("unitA", "A", has_player=False).snapshot().has_player is False


def test_a_playerless_unit_still_reports_its_sources():
    """The whole point of the unit: it ingests, and peers route those sources to their own speakers."""
    unit = make_unit("airplay-1")
    unit.has_player = False
    snapshot = unit.snapshot()
    assert snapshot.has_player is False
    assert [s.source_id for s in snapshot.sources] == ["airplay-1"]
    assert snapshot.players == []
