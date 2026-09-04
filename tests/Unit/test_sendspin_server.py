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

import sendspin_identity  # noqa: E402
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


class _FakeSecurity:
    """Stands in for aiosendspin's ConnectionSecurity: what the snapshot reads is `.psk_category.value`."""

    def __init__(self, category):
        self.psk_category = type("Cat", (), {"value": category})()


class FakeClient:
    def __init__(
        self, client_id, group=None, connected=True, roles=(), active=None, name=None, security=None, paired=False
    ):
        self.client_id = client_id
        self.group = group
        self.is_connected = connected
        # How the transport is secured. `security=None` models a CLEARTEXT client — an ESP32
        # speaker, Music Assistant, the web GUI — which is the default here because it is the
        # majority case and the one that must never be asked to pair.
        self.connection_security = None if security is None else _FakeSecurity(security)
        self.is_paired = paired
        # The handshake name. Only the snapshot path reads it, which is why it was absent until
        # that path got its first test.
        self.name = name or client_id
        self.negotiated_role_ids = list(roles)
        # NEGOTIATED and ACTIVE are different sets under 9.x, and the gap is the silent-failure
        # mode: an encrypted-but-unpaired client negotiates everything and is activated for
        # nothing. `active=None` defaults to "activated for what it negotiated", the healthy case;
        # pass an explicit list (including []) to model a client that is admitted but silent.
        self.active_role_ids = list(roles) if active is None else list(active)
        self._cleanup_handle = None  # armed by the library on every connection teardown

    def roles_by_family(self, family):
        return [r for r in self.negotiated_role_ids if str(r).startswith(family)]

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


class _FakeManagementConnection:
    """What enable_management returns: the object carrying the management commands."""

    def __init__(self, server):
        self._server = server

    async def open_pairing_window(self):
        self._server.calls.append("open_pairing_window")
        if getattr(self._server, "window_error", None) is not None:
            raise self._server.window_error
        return type("R", (), {"value": "ok"})()


class FakePairingStore:
    """Stands in for aiosendspin's ServerPairingStore. Only the three calls staging touches."""

    def __init__(self):
        self.records = {}   # client_id -> record (a real pairing)
        self.staged = {}    # client_id -> StagedPairingPsk (a pairing pre-authorised for the handshake)

    async def record_by_client_id(self, client_id):
        return self.records.get(client_id)

    async def staged_pairing_psk(self, client_id):
        return self.staged.get(client_id)

    async def stage_pairing_psk(self, client_id, staged):
        self.staged[client_id] = staged


class FakeServer:
    def __init__(self):
        self.pairing_store = FakePairingStore()
        self._by_id = {}
        self._urls = {}
        self._connection_tasks = {}
        self.dialed = []  # (url, reason) in call order
        self.disconnected = []  # urls passed to disconnect_from_client, in call order
        self.trusted = []  # client ids passed to trust_unpaired
        self.pin_seen = None            # the PIN the library asked for, once supplied
        self.pairing_error = None       # set to make initiate_pairing raise
        self.management_error = None    # set to make enable_management raise
        self.window_error = None        # set to make open_pairing_window raise
        self.calls = []  # ("trust"|"reclaim", id) in call order — ORDER is the assertion

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

    async def trust_unpaired(self, client_id):
        self.calls.append(("trust", client_id))
        self.trusted.append(client_id)

    def reclaim_client_for_playback(self, client_id, timeout_s=None):
        self.calls.append(("reclaim", client_id))
        return client_id in self._urls

    # -- pairing ---------------------------------------------------------
    async def initiate_pairing(self, client_id, attempt):
        self.calls.append(("initiate_pairing", client_id, attempt.method.value))
        if attempt.pin_provider is not None:
            self.pin_seen = await attempt.pin_provider()   # exercises the future the API resolves
        if self.pairing_error is not None:
            raise self.pairing_error

    async def end_pairing(self, client_id):
        self.calls.append(("end_pairing", client_id))

    async def unpair(self, client_id):
        self.calls.append(("unpair", client_id))

    async def untrust_unpaired(self, client_id):
        self.calls.append(("untrust", client_id))

    def enable_management(self, client_id):
        self.calls.append(("enable_management", client_id))
        if self.management_error is not None:
            raise self.management_error
        return _FakeManagementConnection(self)

    def disable_management(self, client_id):
        self.calls.append(("disable_management", client_id))


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

        # The client id, not a bool: adopt_foreign_client returns the id it learned from the
        # handshake, because this is the only moment the mDNS URL and that id are both in hand.
        assert ok == "08:B6:1F:B7:AF:5C"
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

        assert ok == "08:B6:1F:B7:AF:5C"
        assert unit.server.dialed == []
        assert handle.group.calls == []  # no remove/add, and above all no start_stream

    run_scenario(scenario)


def test_adopting_a_speaker_we_do_not_hold_still_dials_it():
    """The stale/never-seen case the redial exists for: a disconnected client must be re-dialled."""

    async def scenario():
        unit = make_unit("airplay-1")
        unit.server.add(FakeClient("08:B6:1F:B7:AF:5C", connected=False, roles=("player@v1",)), url=SPEAKER_URL)

        ok = await unit.adopt_foreign_client("airplay-1", SPEAKER_URL, timeout_s=0.3)

        assert ok is None  # nothing ever connected within the timeout
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


# -- the snapshot publishes ACTIVE roles, not just negotiated ones ---------------------------------


def _snapshot_player(active):
    """One connected player-role client on a unit, with the given ACTIVE role set."""
    unit = make_unit("airplay-1")
    player = FakeClient("player-1", group=unit.sources["airplay-1"].group, roles=["player@v1"], active=active)
    unit.server.add(player)  # no url= — that spawns a dial task, which needs a running loop
    unit.server.register_client_url("player-1", "ws://10.0.0.5:8928/sendspin")
    return unit.snapshot().players[0]


def test_the_snapshot_publishes_a_players_active_roles():
    """The signal that separates a working endpoint from a silent one.

    Under 9.x an encrypted-but-unpaired client negotiates its full role set and is activated for
    none of it — it is connected, grouped, at the right volume, and renders nothing, with no error
    at either end. `connected` cannot express that and neither can the negotiated set, so the
    active set is published for the mesh API, the GUI and deploy.sh's verify to read.
    """
    assert _snapshot_player(["player@v1"]).active_roles == ["player@v1"]


def test_an_admitted_but_unactivated_player_is_visible_as_such():
    """The failure this field exists for: everything else about this row looks healthy."""
    row = _snapshot_player([])
    assert row.active_roles == []
    assert row.connected is True, "the trap is precisely that it IS connected"


def test_active_roles_survives_the_wire_round_trip():
    """It crosses the mesh REST API, so a peer must see it too."""
    from mesh.model import PlayerState

    row = _snapshot_player(["player@v1"])
    assert PlayerState.from_dict(row.to_dict()).active_roles == ["player@v1"]


def test_a_peer_on_an_older_image_reads_as_unknown_not_as_silent():
    """None, not [] — the same reasoning as has_player defaulting True. A peer that never sends
    this field must not be read as reporting a client activated for nothing."""
    from mesh.model import PlayerState

    legacy = {"player_id": "p", "name": "p", "connected": True, "group_id": None}
    assert PlayerState.from_dict(legacy).active_roles is None


# -- a PEER's player must be trusted before we take it ---------------------------------------------


def test_reclaiming_a_peers_player_trusts_it_first():
    """The cross-server roam guard.

    Trust is per-server AND per-peer. Trusting our own player at startup says nothing about unit
    B's, and a peer's player arriving here is an encrypted-but-unpaired client — so without this
    the roam SUCCEEDS in every visible way (the player detaches from its old server, reconnects
    here, joins the group at the right volume) and is activated for no roles. The room goes silent
    with nothing in either log.

    Order matters: trust must precede the dial, so the client is already approved when it lands.
    Late trust does recover (trust_unpaired re-activates a live connection) but only after an
    audible gap, and only if something calls it again.
    """

    async def scenario():
        unit = make_unit("airplay-1")
        peer_player = "peer-player-pubkey"
        unit.server.add(FakeClient(peer_player, roles=["player@v1"]))
        await unit.reclaim_remote_player("airplay-1", peer_player, "ws://10.0.0.9:8928/sendspin", timeout_s=0.5)

        assert ("trust", peer_player) in unit.server.calls, "a peer's player was never trusted"
        trust_at = unit.server.calls.index(("trust", peer_player))
        reclaim_at = unit.server.calls.index(("reclaim", peer_player))
        assert trust_at < reclaim_at, "trusted only AFTER dialing — the player lands untrusted and silent"

    run_scenario(scenario)


def test_a_failed_trust_does_not_abort_the_route():
    """Trust bookkeeping must never be able to fail a route — it is a store write on the audio path."""

    async def scenario():
        unit = make_unit("airplay-1")

        async def boom(_client_id):
            raise RuntimeError("trust store unavailable")

        unit.server.trust_unpaired = boom
        unit.server.add(FakeClient("p", roles=["player@v1"]))
        # Must not raise. It returns False only because the fake never "connects" the player.
        await unit.reclaim_remote_player("airplay-1", "p", "ws://10.0.0.9:8928/sendspin", timeout_s=0.2)

    run_scenario(scenario)


# -- the snapshot must say whether pairing is even a question ---------------------------------------


def _row(**kw):
    unit = make_unit("airplay-1")
    unit.server.add(FakeClient("p", group=unit.sources["airplay-1"].group, roles=["player@v1"], **kw))
    return unit.snapshot().players[0]


def test_a_cleartext_client_reports_no_security_at_all():
    """The load-bearing case. Every ESP32 speaker, Music Assistant and our own web GUI connect over
    the legacy cleartext path, which never resolves a PSK — so `security` is None, and that is how
    the GUI knows never to offer them a Pair button. Getting this wrong would put a Pair button on
    devices that cannot pair, which is worse than not shipping pairing at all."""
    row = _row(security=None)
    assert row.security is None
    assert row.paired is False
    assert row.active_roles == ["player@v1"], "cleartext clients are activated without pairing"


def test_an_encrypted_unpaired_client_is_distinguishable_from_a_cleartext_one():
    """Both are `paired=False`; only `security` separates them. This is the pair the Pair button
    keys on: sentinel + no active roles means pairing is required and possible."""
    row = _row(security="sentinel", active=[])
    assert row.security == "sentinel"
    assert row.paired is False
    assert row.active_roles == []


def test_a_paired_client_says_so():
    row = _row(security="long_term", paired=True)
    assert (row.security, row.paired) == ("long_term", True)


def test_security_and_paired_survive_the_wire_round_trip():
    """They cross the mesh REST API, so a peer's GUI must see them too."""
    from mesh.model import PlayerState

    back = PlayerState.from_dict(_row(security="sentinel", active=[]).to_dict())
    assert (back.security, back.paired, back.active_roles) == ("sentinel", False, [])


def test_a_peer_on_an_older_image_reads_as_unknown_not_as_cleartext():
    """`security` absent from the wire is indistinguishable from cleartext at the type level, so the
    GUI must gate on active_roles being present too — this pins the wire shape it relies on."""
    from mesh.model import PlayerState

    legacy = {"player_id": "p", "name": "p", "connected": True, "group_id": None}
    row = PlayerState.from_dict(legacy)
    assert row.security is None and row.paired is False
    assert row.active_roles is None, "None here is what marks the whole row unknown"


_TOKEN = None  # a real pairing token, built once at import (see below)


def _make_token():
    from aiosendspin.noise import Identity, PSKPairingToken, encode_token, generate_psk

    return encode_token(PSKPairingToken(client_id=Identity.generate().peer_id, pairing_psk=generate_psk()))


_TOKEN = _make_token()


# -- operator-driven pairing ------------------------------------------------------------------------


def test_a_pin_attempt_waits_for_the_operator_and_then_completes():
    """The whole shape of a PIN pairing: the library asks for a PIN by awaiting a provider, the API
    resolves it from a separate request, and the attempt finishes. It runs as a background task
    because that await can last as long as someone walking to a speaker to read its display —
    holding an HTTP request open for that is what this design exists to avoid."""

    async def scenario():
        unit = make_unit("airplay-1")
        unit.server.add(FakeClient("spk", roles=["player@v1"]))
        await unit.pair_client("spk", "dynamic_pin")

        for _ in range(50):  # let the task reach the provider
            await asyncio.sleep(0.01)
            if unit.pairing_state("spk").get("state") == "awaiting_pin":
                break
        assert unit.pairing_state("spk")["state"] == "awaiting_pin"

        assert unit.submit_pin("spk", "123456") is True
        for _ in range(50):
            await asyncio.sleep(0.01)
            if unit.pairing_state("spk").get("state") == "paired":
                break
        assert unit.pairing_state("spk") == {"state": "paired"}
        assert unit.server.pin_seen == "123456", "the PIN must reach the library, not just the future"

    run_scenario(scenario)


def test_a_pin_for_nobody_is_refused_rather_than_swallowed():
    """False here means "nothing was waiting" — a timed-out or cancelled attempt — NOT a wrong PIN.
    The API turns it into a 409 so the operator is told to start again rather than left retyping
    into a dead dialog, which otherwise looks identical to getting the digits wrong."""
    unit = make_unit("airplay-1")
    assert unit.submit_pin("spk", "123456") is False


def test_pairing_a_disconnected_client_is_a_bad_request_not_a_crash():
    """Pairing runs over a live connection; there is nothing to attempt without one."""

    async def scenario():
        unit = make_unit("airplay-1")
        with pytest.raises(KeyError):
            await unit.pair_client("never-connected", "dynamic_pin")

    run_scenario(scenario)


def test_an_unknown_pairing_method_is_rejected_before_anything_starts():
    async def scenario():
        unit = make_unit("airplay-1")
        unit.server.add(FakeClient("spk", roles=["player@v1"]))
        with pytest.raises(ValueError):
            await unit.pair_client("spk", "telepathy")
        assert not any(c[0] == "initiate_pairing" for c in unit.server.calls if isinstance(c, tuple))

    run_scenario(scenario)


def test_a_failed_attempt_records_a_reason_the_operator_can_act_on():
    """A wrong PIN, a hung-up speaker and a timeout are different problems needing different
    actions, so the failure is kept as a message rather than collapsing to False."""

    async def scenario():
        unit = make_unit("airplay-1")
        unit.server.add(FakeClient("spk", roles=["player@v1"]))
        unit.server.pairing_error = RuntimeError("pairing aborted by the client")
        await unit.pair_client("spk", "pairing_psk", token=_TOKEN)
        for _ in range(50):
            await asyncio.sleep(0.01)
            if unit.pairing_state("spk").get("state") == "failed":
                break
        state = unit.pairing_state("spk")
        assert state["state"] == "failed"
        assert "aborted" in state["error"]

    run_scenario(scenario)


def test_the_psk_method_does_not_ask_for_a_pin():
    """Pairing PSK is the no-interaction method — offering a PIN dialog for it would be a bug the
    operator experiences as a prompt they cannot answer."""

    async def scenario():
        unit = make_unit("airplay-1")
        unit.server.add(FakeClient("spk", roles=["player@v1"]))
        await unit.pair_client("spk", "pairing_psk", token=_TOKEN)
        for _ in range(50):
            await asyncio.sleep(0.01)
            if unit.pairing_state("spk").get("state") == "paired":
                break
        assert unit.server.pin_seen is None

    run_scenario(scenario)


def test_opening_a_pairing_window_goes_through_the_management_role():
    """The protocol's answer to multi-server pairing: a server already paired with a device may
    stand in for the physical gesture. This is why a unit pairs with its own player at startup."""

    async def scenario():
        unit = make_unit("airplay-1")
        unit.server.add(FakeClient("own-player", roles=["player@v1"], paired=True))
        assert await unit.open_pairing_window("own-player") is True
        assert ("enable_management", "own-player") in unit.server.calls
        assert "open_pairing_window" in unit.server.calls

    run_scenario(scenario)


def test_a_refused_management_session_is_not_fatal():
    """Management needs a long-term record, so this fails for anything we have not paired with —
    a normal answer, not an error worth taking the API down for."""

    async def scenario():
        unit = make_unit("airplay-1")
        unit.server.add(FakeClient("stranger", roles=["player@v1"]))
        unit.server.management_error = RuntimeError("not paired")
        assert await unit.open_pairing_window("stranger") is False

    run_scenario(scenario)


def test_turning_unpaired_access_off_revokes_every_existing_approval():
    """The client half rides in client/hello and is fixed for a connection's life, but the SERVER
    half is live. Without this, turning the setting off would leave every already-trusted peer
    playing until its next reconnect — a policy change that appears to have applied and has not."""

    async def scenario():
        unit = make_unit("airplay-1")
        unit.server.add(FakeClient("a", roles=["player@v1"]))
        unit.server.add(FakeClient("b", roles=["player@v1"]))
        await unit.set_unpaired_access(False)
        assert {c[1] for c in unit.server.calls if isinstance(c, tuple) and c[0] == "untrust"} == {"a", "b"}

    run_scenario(scenario)


# --- auto-pairing must never touch a cleartext client -----------------------


def test_a_cleartext_speaker_is_never_offered_the_shared_psk(monkeypatch):
    """The ESP32 regression, pinned.

    Pairing mixes a PSK into a Noise handshake, so there is nothing to pair over the legacy
    cleartext path and `initiate_pairing` refuses it. Every ESP32 speaker is cleartext, so with a
    fleet PSK configured each one that connected earned a doomed pairing attempt — and that broke
    `adopt_foreign_client` on hardware: the speaker connected, the pairing ran and failed, and the
    adopt's 15 s wait expired reporting "never connected" about a device whose MAC had just been
    logged. The NEXT adopt of the same speaker succeeded, because by then it was already connected.

    Measured on .7.122 against three boards (Voice PE, Esparagus, Satellite1), 2026-08-13.
    """
    unit = make_unit("airplay-1")
    monkeypatch.setattr(sendspin_identity, "fleet_psk", lambda: "fleet-secret")
    monkeypatch.setattr(sendspin_identity, "peer_id_of", lambda role: "our-own-player")
    unit.server.add(FakeClient("ESP32-MAC", roles=["player@v1"], security=None))

    asyncio.run(unit._maybe_pair_via_shared_psk("ESP32-MAC"))

    assert not [c for c in unit.server.calls if c[0] == "initiate_pairing"], (
        "a cleartext speaker must not be asked to pair — the library refuses and the adopt breaks"
    )


def test_a_peer_player_is_staged_before_the_dial_not_paired_after_it(monkeypatch):
    """The other half, and the reason the peer branch moved out of the connect handler.

    `initiate_pairing` on a client that is already connected on the sentinel PSK forces a
    mid-connection Noise re-handshake. A peer's player is contended — its own server is dialling it
    too and it holds only ONE websocket — so the re-handshake finds the socket gone:

        could not pair player G2UChhEv...: expected Noise message 2 (TEXT), got CLOSE
        [airplay-1] reclaim of remote player G2UChhEv... timed out    (then, forever)

    Staging puts the same PSK in front of the handshake instead, so the reclaim's own connection
    comes up already paired. Measured on .7.122 taking .7.204's player, 2026-08-13.
    """
    unit = make_unit("airplay-1")
    monkeypatch.setattr(sendspin_identity, "fleet_psk", lambda: b"f" * 32)
    monkeypatch.setattr(sendspin_identity, "peer_id_of", lambda role: "our-own-player")
    monkeypatch.setattr(sendspin_identity, "local_pairing_psk", lambda: b"f" * 32)
    unit.server.add(FakeClient("peer-player", roles=["player@v1"], security="sentinel"))

    # The connect handler must leave a peer alone...
    asyncio.run(unit._maybe_pair_via_shared_psk("peer-player"))
    assert not [c for c in unit.server.calls if c[0] == "initiate_pairing"]

    # ...and the roam path must stage it instead.
    assert asyncio.run(unit.stage_shared_psk("peer-player")) is True
    assert "peer-player" in unit.server.pairing_store.staged


# --- the management session must never outlive the call ---------------------


def test_the_management_session_is_closed_after_opening_a_window():
    """A declared `management` activity makes the player UNROAMABLE until the process restarts.

    Activities are part of what the client's arbitration ranks when a second server dials it, so a
    server still holding management outranks a peer asking for plain PLAYBACK. The peer's dial is
    accepted provisionally, handshakes, and is then rejected — it lands in the peer's registry as
    `(disconnected)` and the reclaim polls for a client that never comes up.

    Measured on .7.122 on 2026-08-13, and the reproduction is brutally simple: 12/12 successful
    roams, ONE /api/mesh/pairing-window call, then failure on the very next attempt and every one
    after it. A restart "fixed" it, which is what made it look like drifting state for hours.
    """
    unit = make_unit("airplay-1")
    assert asyncio.run(unit.open_pairing_window("our-own-player")) is True
    assert ("disable_management", "our-own-player") in unit.server.calls
    enabled = unit.server.calls.index(("enable_management", "our-own-player"))
    disabled = unit.server.calls.index(("disable_management", "our-own-player"))
    assert enabled < disabled, "management must be enabled first and closed after"


def test_the_management_session_is_closed_even_when_the_window_call_fails():
    """The failure path is the one that matters: an exception mid-call must not leak the session, or
    a single failed pairing attempt costs roaming until the next restart."""
    unit = make_unit("airplay-1")
    unit.server.window_error = RuntimeError("client said no")
    assert asyncio.run(unit.open_pairing_window("our-own-player")) is False
    assert ("disable_management", "our-own-player") in unit.server.calls


# --- releasing an idle player so a foreign server can claim it ---------------


def _own(monkeypatch, player_id="our-own-player"):
    monkeypatch.setattr(sendspin_identity, "peer_id_of", lambda role: player_id)
    return player_id


def test_register_player_does_not_dial():
    """The whole of third-party interop, in one assertion.

    A client holds exactly ONE websocket and the library keeps an incumbent that outranks the
    newcomer, so a resident PLAYBACK connection to our own player means a foreign server's dial is
    admitted just long enough to register the speaker and then dropped. Music Assistant listed both
    units and marked them `available=False` — not a state anyone can play out of.
    """
    unit = make_unit("airplay-1")
    unit.register_player("our-own-player", "ws://127.0.0.1:8928/sendspin")
    assert unit.server.get_client_url("our-own-player") == "ws://127.0.0.1:8928/sendspin", (
        "the URL must still be registered — routing and peer reclaim both join on it"
    )
    assert unit.server.dialed == [], "registering must not dial: holding the socket is what blocks interop"


def test_release_local_player_lets_go_of_an_idle_player(monkeypatch):
    """Detaching from a group is NOT releasing: we still hold the websocket that blocks MA."""
    own = _own(monkeypatch)
    unit = make_unit("airplay-1")
    unit.server.add(FakeClient(own, roles=["player@v1"], security="long_term"))
    unit.server.register_client_url(own, "ws://127.0.0.1:8928/sendspin")

    asyncio.run(unit.release_local_player())

    assert "ws://127.0.0.1:8928/sendspin" in unit.server.disconnected


def test_release_local_player_never_takes_a_playing_speaker(monkeypatch):
    """Called when ONE source goes idle, while another may still be feeding that same player.
    Releasing then would cut off a room that is audibly playing."""
    own = _own(monkeypatch)
    unit = make_unit("airplay-1", "spotify-1")
    client = FakeClient(own, roles=["player@v1"], security="long_term")
    unit.server.add(client)
    unit.server.register_client_url(own, "ws://127.0.0.1:8928/sendspin")
    unit.sources["spotify-1"].group.members.append(client)  # still playing on the other source

    asyncio.run(unit.release_local_player())

    assert unit.server.disconnected == [], "a player still attached to a live source must be left alone"


def test_volume_set_while_released_is_held_not_dialled(monkeypatch):
    """A slider nudge must never steal a room back mid-track from a foreign server. The level is
    remembered and sent on the next connect instead."""
    own = _own(monkeypatch)
    unit = make_unit("airplay-1")
    unit.server.register_client_url(own, "ws://127.0.0.1:8928/sendspin")

    unit.set_player_volume(own, 42, False)   # not connected: must not raise, must not dial

    assert unit.server.dialed == []
    assert unit._pending_volume[own] == (42, False)


def test_setting_the_local_player_to_none_releases_it(monkeypatch):
    """"Idle" must not depend on HOW it got there.

    Going idle (EOF/silence) released the player, but an explicit unroute did not — so a speaker set
    to "none" looked idle to us while still holding the websocket that makes it invisible to every
    other server. Caught on the rig: .204 went `available=True` in Music Assistant while .122, which
    the test teardown had unrouted, stayed False.
    """
    own = _own(monkeypatch)
    unit = make_unit("airplay-1")
    client = FakeClient(own, roles=["player@v1"], security="long_term")
    unit.server.add(client)
    unit.server.register_client_url(own, "ws://127.0.0.1:8928/sendspin")
    unit.sources["airplay-1"].group.members.append(client)

    async def unroute():
        unit.sources["airplay-1"].group.members.remove(client)   # what remove_client does
        await unit.detach_player("airplay-1", own)

    asyncio.run(unroute())

    assert "ws://127.0.0.1:8928/sendspin" in unit.server.disconnected
