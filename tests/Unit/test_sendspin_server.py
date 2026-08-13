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
import contextlib
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

    async def stop(self):
        self.calls.append("stop")

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


class FakeCleanupHandle:
    """Stands in for the asyncio.TimerHandle behind SendspinClient._cleanup_handle."""

    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class FakeClient:
    def __init__(self, client_id, group=None, connected=True, roles=()):
        self.client_id = client_id
        self.group = group
        self.is_connected = connected
        self.negotiated_roles = list(roles)
        self._cleanup_handle = None  # armed by the library on every connection teardown

    def roles_by_family(self, family):
        return [r for r in self.negotiated_roles if str(r).startswith(family)]

    def arm_cleanup(self):
        self._cleanup_handle = FakeCleanupHandle()
        return self._cleanup_handle


def make_dial_task(swallows=1):
    """A real task standing in for a server-initiated dial, swallowing its first N cancels.

    That is what 6.0.5 does: `SendspinConnection._handle_client` awaits the message loop as a
    SEPARATE task and `_run_message_loop` catches CancelledError, so the cancel is consumed and the
    dialer simply reconnects. Only a cancel landing outside the message loop kills it. Must be
    created inside a running loop.
    """

    async def dial():
        swallowed = 0
        while True:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                swallowed += 1
                if swallowed > swallows:
                    raise

    return asyncio.ensure_future(dial())


def run_scenario(scenario):
    """Run an async test body, then hard-cancel any dial tasks it left behind.

    asyncio.run's own shutdown cancels leftovers exactly ONCE, which a swallowing dial task
    survives — so without this the test hangs rather than fails.
    """

    async def wrapper():
        try:
            await scenario()
        finally:
            for task in [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]:
                while not task.done():
                    task.cancel()
                    with contextlib.suppress(BaseException):
                        await asyncio.wait({task}, timeout=0.05)

    asyncio.run(wrapper())


class FakeServer:
    def __init__(self):
        self._by_id = {}
        self._urls = {}
        self._connection_tasks = {}
        self.dialed = []  # (url, reason) in call order
        self.disconnected = []  # urls passed to disconnect_from_client, in call order

    @property
    def clients(self):
        return list(self._by_id.values())

    def add(self, client, url=None):
        self._by_id[client.client_id] = client
        if url is not None:
            self._urls[client.client_id] = url
            self._connection_tasks[url] = make_dial_task()
        return client

    def get_client(self, client_id):
        return self._by_id.get(client_id)

    def get_or_create_client(self, client_id):
        if client_id not in self._by_id:
            self._by_id[client_id] = FakeClient(client_id)
        return self._by_id[client_id]

    def register_client_url(self, client_id, url):
        self._urls[client_id] = url

    def get_client_url(self, client_id):
        return self._urls.get(client_id)

    def disconnect_from_client(self, url):
        self.disconnected.append(url)
        self._connection_tasks.pop(url, None)

    def connect_to_client(self, url, *, connection_reason=None, retry_initial_connection=False):
        self.dialed.append((url, connection_reason))
        self._connection_tasks.setdefault(url, make_dial_task())


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


# --- SourceFeeder._go_idle: true none (ROUTING-MODEL.md rule 1) ------------


def test_go_idle_detaches_player_role_clients_but_not_controllers_or_the_anchor():
    group = FakeGroup()
    player1 = FakeClient("player-1", group=group, roles=["player@v1"])
    player2 = FakeClient("player-2", group=group, roles=["player@v1"])
    controller = FakeClient("ctrl:src1:n1", group=group, roles=["controller@v1"])
    anchor = FakeClient(ss.ANCHOR_PREFIX + "src1", group=group)  # transport-less: no roles
    group.members = [player1, player2, controller, anchor]
    feeder = make_feeder(group=group, ps=FakePushStream())
    feeder._last_data_at = 1.0  # was active, so _go_idle won't early-return

    asyncio.run(feeder._go_idle("test"))

    assert ("remove", "player-1") in group.calls
    assert ("remove", "player-2") in group.calls
    assert [c.client_id for c in group.members] == ["ctrl:src1:n1", ss.ANCHOR_PREFIX + "src1"]


def test_go_idle_is_a_noop_when_already_idle():
    group = FakeGroup()
    player = FakeClient("player-1", group=group, roles=["player@v1"])
    group.members = [player]
    feeder = make_feeder(group=group, ps=FakePushStream())
    feeder._last_data_at = 1.0

    asyncio.run(feeder._go_idle("first"))
    calls_after_first = list(group.calls)
    asyncio.run(feeder._go_idle("second"))

    assert group.calls == calls_after_first


def test_go_idle_announces_stopped():
    group = FakeGroup()
    feeder = make_feeder(group=group, ps=FakePushStream())
    feeder._last_data_at = 1.0

    asyncio.run(feeder._go_idle("test"))

    assert "stop" in group.calls


# --- attach_player: the caller that must not lose the refresh ---------------


def test_attach_player_stops_the_stream_around_the_add_then_re_acquires_it():
    """The order is the whole point: stop, THEN add, THEN start.

    add_client alone leaves an already-connected player in the group but out of the stream, so the
    re-acquire is what makes it audible. Doing it the other way round — add first, refresh second —
    hands the joining client the outgoing stream and puts `stream/start`, `stream/end`,
    `stream/start` on the wire inside ~110 ms, which permanently wedges an ESP32 client. See
    SourceFeeder.membership_change.
    """
    unit = make_unit("airplay-1")
    handle = unit.sources["airplay-1"]
    handle.feeder.ps = FakePushStream()  # a stream is live
    unit.server.add(FakeClient("player-1", group=None))

    asyncio.run(unit.attach_player("airplay-1", "player-1"))

    assert handle.group.calls == ["stop_stream", ("add", "player-1"), "start_stream"]


def test_attach_player_removes_from_the_old_group_before_adding():
    """A bare add_client would stop the player's CURRENT source group — remove first."""
    unit = make_unit("airplay-1", "spotify-1")
    old = unit.sources["spotify-1"].group
    dest = unit.sources["airplay-1"]
    dest.feeder.ps = FakePushStream()
    player = unit.server.add(FakeClient("player-1", group=old))

    asyncio.run(unit.attach_player("airplay-1", "player-1"))

    assert old.calls == [("remove", "player-1")]
    assert dest.group.calls == ["stop_stream", ("add", "player-1"), "start_stream"]
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


# --- adopting a foreign speaker --------------------------------------------
#
# The 2026-08-10 failure: routing a source at a third-party speaker (an Esparagus HiFi board, a
# FutureProof Homes Satellite1) played for a few seconds and then dropped back to idle, every time.
# adopt_foreign_client redialled unconditionally, and in 6.0.5 a cancelled dial task does not die —
# it backs off ~1s and reconnects, while connect_to_client has already installed a second one. Six
# adopts measured six concurrent websockets to one speaker fighting over the single connection a
# Sendspin client allows. These pin both halves of the fix.

SPEAKER_URL = "ws://192.168.7.201:8928/sendspin"


def test_adopting_a_speaker_we_already_hold_does_not_redial_it():
    """The headline guard: a repeat adopt of a working speaker must not touch the connection."""

    async def scenario():
        unit = make_unit("airplay-1")
        handle = unit.sources["airplay-1"]
        handle.feeder.ps = FakePushStream()
        unit.server.add(FakeClient("08:B6:1F:B7:AF:5C", roles=("player@v1",)), url=SPEAKER_URL)

        ok = await unit.adopt_foreign_client("airplay-1", SPEAKER_URL, player_id="esparagus-hifi-1")

        assert ok is True
        assert unit.server.dialed == []  # no redial
        assert unit.server.disconnected == []  # and nothing torn down
        assert handle.group.calls == ["stop_stream", ("add", "08:B6:1F:B7:AF:5C"), "start_stream"]

    run_scenario(scenario)


def test_re_adopting_a_speaker_already_on_that_source_leaves_the_whole_group_alone():
    """refresh_stream is a WHOLE-GROUP event, so a redundant adopt must not reach it."""

    async def scenario():
        unit = make_unit("airplay-1")
        handle = unit.sources["airplay-1"]
        handle.feeder.ps = FakePushStream()
        speaker = unit.server.add(FakeClient("08:B6:1F:B7:AF:5C", roles=("player@v1",)), url=SPEAKER_URL)
        speaker.group = handle.group
        handle.group.members.append(speaker)

        ok = await unit.adopt_foreign_client("airplay-1", SPEAKER_URL)

        assert ok is True
        assert unit.server.dialed == []
        assert handle.group.calls == []  # no remove/add, and above all no start_stream

    run_scenario(scenario)


def test_adopting_a_speaker_we_do_not_hold_still_dials_it():
    """The stale/never-seen case the redial exists for: a disconnected client must be re-dialled."""

    async def scenario():
        unit = make_unit("airplay-1")
        unit.server.add(FakeClient("08:B6:1F:B7:AF:5C", connected=False, roles=("player@v1",)), url=SPEAKER_URL)

        ok = await unit.adopt_foreign_client("airplay-1", SPEAKER_URL, timeout_s=0.3)

        assert ok is False  # nothing ever connected within the timeout
        assert [url for url, _ in unit.server.dialed] == [SPEAKER_URL]

    run_scenario(scenario)


def test_a_redial_waits_for_the_old_dial_task_to_actually_die():
    """One cancel is swallowed by 6.0.5's message loop; dialing again before it dies leaks a dialer."""

    async def scenario():
        unit = make_unit("airplay-1")
        unit.server.add(FakeClient("08:B6:1F:B7:AF:5C", connected=False, roles=("player@v1",)), url=SPEAKER_URL)
        old = unit.server._connection_tasks[SPEAKER_URL]

        await unit.adopt_foreign_client("airplay-1", SPEAKER_URL, timeout_s=0.3)

        assert old.done()  # gone before the new dial went out, not left redialing beside it

    run_scenario(scenario)


def test_routing_a_player_defuses_the_eviction_timer_a_teardown_left_behind():
    """The 30s registry cleanup must not survive to evict a speaker that is routed and playing.

    Pinned on attach_player, not on adopt, so it covers every routing path — adopt (both the fast
    path and the redial), a plain route, and a cross-server reclaim.
    """

    async def scenario():
        unit = make_unit("airplay-1")
        source = unit.sources["airplay-1"]
        source.feeder.ps = FakePushStream()
        speaker = unit.server.add(FakeClient("08:B6:1F:B7:AF:5C", roles=("player@v1",)), url=SPEAKER_URL)
        timer = speaker.arm_cleanup()  # what a connection teardown leaves behind

        await unit.attach_player("airplay-1", "08:B6:1F:B7:AF:5C")

        assert timer.cancelled is True
        assert speaker._cleanup_handle is None
        assert source.group.calls == ["stop_stream", ("add", "08:B6:1F:B7:AF:5C"), "start_stream"]

    run_scenario(scenario)


def test_an_idempotent_re_attach_still_defuses_the_eviction_timer():
    """The early return must not skip it — a re-adopt of an already-routed speaker is the case
    that was dropping to idle 30 s later."""

    async def scenario():
        unit = make_unit("airplay-1")
        source = unit.sources["airplay-1"]
        source.feeder.ps = FakePushStream()
        speaker = unit.server.add(FakeClient("08:B6:1F:B7:AF:5C", roles=("player@v1",)), url=SPEAKER_URL)
        speaker.group = source.group
        timer = speaker.arm_cleanup()

        await unit.attach_player("airplay-1", "08:B6:1F:B7:AF:5C")

        assert timer.cancelled is True
        assert source.group.calls == []  # still idempotent: no re-group, no stream churn

    run_scenario(scenario)


def test_releasing_defuses_the_pending_cleanup_before_arming_the_goodbye_one():
    """Two schedules orphan the first: _schedule_cleanup overwrites the handle without cancelling."""

    async def scenario():
        unit = make_unit("airplay-1")
        handle = unit.sources["airplay-1"]
        speaker = unit.server.add(FakeClient("08:B6:1F:B7:AF:5C", roles=("player@v1",)), url=SPEAKER_URL)
        speaker.group = handle.group
        handle.group.members.append(speaker)
        pending = speaker.arm_cleanup()  # armed by the dial teardown, reason=None -> 30s

        await unit.release_foreign_client("airplay-1", "08:B6:1F:B7:AF:5C")

        assert pending.cancelled is True  # or it fires 30s later and evicts whoever holds this id

    run_scenario(scenario)


def test_cancelling_a_cleanup_is_harmless_when_there_is_none_or_no_such_client():
    """Best-effort by design: it reaches into a private attribute and must never break routing."""

    async def scenario():
        unit = make_unit("airplay-1")
        unit.server.add(FakeClient("08:B6:1F:B7:AF:5C", roles=("player@v1",)))
        unit._cancel_pending_cleanup("08:B6:1F:B7:AF:5C")  # no handle armed
        unit._cancel_pending_cleanup("nobody-by-that-name")  # not in the registry

    run_scenario(scenario)


def test_releasing_a_speaker_stops_the_dial_instead_of_letting_it_reconnect():
    """disconnect_from_client does not stop the dialer, so a release that used it took the speaker
    straight back about a second later — a release that silently did nothing."""

    async def scenario():
        unit = make_unit("airplay-1")
        handle = unit.sources["airplay-1"]
        speaker = unit.server.add(FakeClient("08:B6:1F:B7:AF:5C", roles=("player@v1",)), url=SPEAKER_URL)
        speaker.group = handle.group
        handle.group.members.append(speaker)
        dial = unit.server._connection_tasks[SPEAKER_URL]

        await unit.release_foreign_client("airplay-1", "08:B6:1F:B7:AF:5C")

        assert dial.done()
        assert handle.group.calls == [("remove", "08:B6:1F:B7:AF:5C")]

    run_scenario(scenario)


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


def test_controller_grouping_falls_back_to_the_primary_source_when_active():
    unit = make_unit("airplay-1", "spotify-1")
    unit.sources["airplay-1"].feeder._last_data_at = 1.0
    unit.server.add(FakeClient("some-other-client"))

    asyncio.run(unit._maybe_group_controller("some-other-client"))

    assert unit.sources["airplay-1"].group.calls == [("add", "some-other-client")]


def test_controller_grouping_follows_the_primary_after_it_moves():
    """The pairing that item 9 of the backlog was about: stop the first source, keep grouping."""
    unit = make_unit("airplay-1", "spotify-1")
    asyncio.run(unit.stop_source("airplay-1"))
    unit.sources["spotify-1"].feeder._last_data_at = 1.0
    unit.server.add(FakeClient("some-other-client"))

    asyncio.run(unit._maybe_group_controller("some-other-client"))

    assert unit.sources["spotify-1"].group.calls == [("add", "some-other-client")]


def test_controller_grouping_does_not_default_into_an_idle_primary_source():
    """A third-party controller with no ctrl: hint must not be handed a dead group."""
    unit = make_unit("airplay-1", "spotify-1")
    unit.server.add(FakeClient("some-other-client"))

    asyncio.run(unit._maybe_group_controller("some-other-client"))

    assert unit.sources["airplay-1"].group.calls == []
    assert unit.sources["spotify-1"].group.calls == []


def test_controller_grouping_falls_back_to_any_active_source_when_primary_is_idle():
    unit = make_unit("airplay-1", "spotify-1")
    unit.sources["spotify-1"].feeder._last_data_at = 1.0
    unit.server.add(FakeClient("some-other-client"))

    asyncio.run(unit._maybe_group_controller("some-other-client"))

    assert unit.sources["airplay-1"].group.calls == []
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


def test_local_player_config_defaults_to_the_loopback_listener():
    assert ss.local_player_config({}) == "ws://127.0.0.1:8928/sendspin"


def test_local_player_config_honours_an_explicit_url():
    env = {"PLUM_LOCAL_PLAYER_URL": "ws://10.0.0.9:8928/sendspin"}
    assert ss.local_player_config(env) == "ws://10.0.0.9:8928/sendspin"


def test_the_operator_flag_removes_the_local_player():
    """deploy.sh writes this from a units.conf row whose DAC column is `none`."""
    assert ss.local_player_config({"PLUM_PLAYER_ENABLED": "0"}) is None


def test_an_empty_url_also_removes_the_local_player():
    """The older way of saying it, still used on the dev rig — must keep working."""
    assert ss.local_player_config({"PLUM_LOCAL_PLAYER_URL": ""}) is None


def test_the_flag_wins_over_an_explicit_url():
    env = {"PLUM_PLAYER_ENABLED": "0", "PLUM_LOCAL_PLAYER_URL": "ws://10.0.0.9:8928/sendspin"}
    assert ss.local_player_config(env) is None


def test_the_player_id_no_longer_comes_from_the_environment():
    """PLUM_LOCAL_PLAYER_ID used to name the player. Under 9.x the id IS the X25519 public key, so
    honouring an env override would hand the server an id no handshake can ever produce — it would
    register a URL and trust a peer that does not exist, and the real player would arrive unknown
    and untrusted. The variable is deliberately ignored rather than removed, because a deployed
    units.conf still sets it."""
    env = {"PLUM_LOCAL_PLAYER_ID": "player-210"}
    assert ss.local_player_config(env) == "ws://127.0.0.1:8928/sendspin"


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
