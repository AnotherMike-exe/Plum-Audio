"""Unit tests for the Bluetooth AVRCP progress/anchor state machine.

Every test here guards a bug that actually shipped to hardware on 2026-07-28 and was INVISIBLE in
logs — the source looked healthy while the GUI timeline was wrong. They are cheap; the hardware
round-trip that found them was not.

The headline one is `test_progress_anchor_requests_a_fresh_timestamp`. `role.update()` does
replace(current_metadata, **kwargs), which copies `timestamp_us`, and `set_metadata` only stamps
anew when it is None — so publishing progress via update() pairs a CURRENT position with an
ANCIENT timestamp and the client double-counts the elapsed time. Symptom: correct for a second,
then a jump forward, compounding after every pause. See airplay_metadata._emit_progress, which
documents the same library quirk.

Needs aiosendspin + dbus-next (real runtime deps); skipped on a bare checkout.

Run: `pytest tests/Unit`.
"""

import asyncio
import contextlib
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "scripts"))

avrcp_mod = pytest.importorskip(
    "sources.bluetooth_avrcp", reason="needs aiosendspin + dbus-next"
)
BluetoothAvrcp = avrcp_mod.BluetoothAvrcp


@dataclass
class FakeMetadata:
    """Stands in for aiosendspin's Metadata (only the fields this code touches).

    Must be a real dataclass: the production code calls dataclasses.replace() on it, which is
    precisely the mechanism that copies `timestamp_us` and caused the bug these tests guard.
    """

    track_progress: int | None = None
    track_duration: int | None = None
    playback_speed: int | None = None
    timestamp_us: int | None = 111_222_333  # a STALE stamp, as a live role would carry


class FakeRole:
    """Records how progress was published: via set_metadata (correct) or update (the bug)."""

    def __init__(self):
        self.metadata = FakeMetadata()
        self.set_metadata_calls = []
        self.update_calls = []

    def set_metadata(self, md):
        self.set_metadata_calls.append(md)
        self.metadata = md

    def update(self, **kwargs):
        self.update_calls.append(kwargs)

    def freeze_progress(self):
        pass

    def set_repeat(self, mode):
        pass

    def set_shuffle(self, shuffle):
        pass


class FakeGroup:
    def __init__(self):
        self.roles = {"metadata": FakeRole(), "controller": FakeRole()}

    def group_role(self, name):
        return self.roles.get(name)


class FakeAdapter:
    bus = None
    active_address = "AA:BB:CC:DD:EE:99"


def _avrcp():
    group = FakeGroup()
    return BluetoothAvrcp(group, "1", FakeAdapter()), group.roles["metadata"]


# -- the timestamp bug ---------------------------------------------------------------------------


def test_progress_anchor_requests_a_fresh_timestamp():
    """timestamp_us MUST be None so the server stamps 'now'; inheriting it double-counts elapsed."""
    avrcp, role = _avrcp()
    avrcp._playing = True
    avrcp._anchor_progress(45_000, 200_000)

    assert role.set_metadata_calls, "progress must be published via set_metadata"
    published = role.set_metadata_calls[-1]
    assert published.timestamp_us is None, (
        "published progress carried a stale timestamp_us; the client will add the already-elapsed "
        "time on top and the timeline will jump forward"
    )
    assert published.track_progress == 45_000
    assert published.track_duration == 200_000


def test_progress_is_never_published_through_role_update():
    """role.update() copies timestamp_us — using it for progress is the bug itself."""
    avrcp, role = _avrcp()
    avrcp._playing = True
    avrcp._anchor_progress(1_000, 200_000)

    progress_keys = {"track_progress", "track_duration", "playback_speed"}
    for call in role.update_calls:
        assert not (progress_keys & set(call)), f"progress went through role.update(): {call}"


# -- speed / pause -------------------------------------------------------------------------------


def test_pause_publishes_speed_zero():
    """Clients extrapolate locally; only an explicit speed 0 stops a paused timeline advancing."""
    avrcp, role = _avrcp()
    avrcp._playing = False
    avrcp._anchor_progress(45_000, 200_000)
    assert role.set_metadata_calls[-1].playback_speed == 0


def test_play_publishes_full_rate():
    avrcp, role = _avrcp()
    avrcp._playing = True
    avrcp._anchor_progress(45_000, 200_000)
    assert role.set_metadata_calls[-1].playback_speed == 1000


def test_anchor_is_skipped_without_a_duration():
    """The role emits progress only as a complete trio; a duration-less anchor would be dropped."""
    avrcp, role = _avrcp()
    avrcp._playing = True
    avrcp._anchor_progress(45_000, 0)
    assert not role.set_metadata_calls


# -- polled-position sanity check ----------------------------------------------------------------


def test_expected_position_advances_only_while_playing():
    avrcp, _role = _avrcp()
    avrcp._playing = True
    avrcp._anchor_progress(10_000, 200_000)
    assert avrcp._expected_position_ms() == pytest.approx(10_000, abs=200)

    # Paused: the anchor is the answer, no elapsed time added.
    avrcp._playing = False
    avrcp._anchor_progress(10_000, 200_000)
    assert avrcp._expected_position_ms() == 10_000


def test_expected_position_is_none_before_any_anchor():
    """No anchor yet means the ticker has nothing to validate against and must not reject blindly."""
    avrcp, _role = _avrcp()
    assert avrcp._expected_position_ms() is None


# -- the two position lies measured on hardware --------------------------------------------------
#
# Both of these were got wrong in BOTH directions before they were understood: first over-guarded
# (rejecting real remote scrubs), then under-guarded (publishing a position past the end of the
# track). The rule that survives is "check against the track length", because a legitimate scrub is
# always inside the track and a wall-clock-inflated position never is.


def test_position_past_end_of_track_is_rejected():
    """BlueZ advances Position by wall clock across a pause, so it can exceed track length.

    Observed on hardware: 548733 ms reported for a 190066 ms track — the previous anchor plus the
    idle time. The role clamps at duration, so publishing it parks every client at 100%.
    """
    avrcp, role = _avrcp()
    avrcp._playing = True
    avrcp._anchor_progress(150_000, 190_066)          # a good anchor first
    published_before = len(role.set_metadata_calls)

    avrcp._anchor_progress(548_733, 190_066)          # the inflated reading
    latest = role.set_metadata_calls[-1]
    assert latest.track_progress <= 190_066, "a position past the end of the track was published"
    # It either kept the sane anchor or published nothing new — never the impossible value.
    assert latest.track_progress != 548_733
    assert len(role.set_metadata_calls) >= published_before


def test_a_scrub_within_the_track_is_always_honoured():
    """The guard must never reject a real remote seek — that was the previous bug in reverse."""
    avrcp, role = _avrcp()
    avrcp._playing = True
    avrcp._anchor_progress(150_000, 190_066)
    avrcp._anchor_progress(300, 190_066)              # user scrubbed back to the start
    assert role.set_metadata_calls[-1].track_progress == 300

    avrcp._anchor_progress(189_000, 190_066)          # and forward to near the end
    assert role.set_metadata_calls[-1].track_progress == 189_000


# -- the ticker must be unkillable ---------------------------------------------------------------


def test_a_failing_tick_does_not_kill_the_ticker():
    """A raise inside the loop body must be swallowed, logged, and the loop must carry on.

    This is the highest-value guard in the file. asyncio only reports an unretrieved task exception
    when the task is garbage collected, and BluetoothAvrcp holds a strong reference to its ticker
    task for the life of the process — so before this, one bad tick killed periodic progress
    SILENTLY, with nothing in the log. Because clients extrapolate locally between anchors, the GUI
    went on counting up smoothly and only lost its corrections, which is why it was reported as
    "scrubs on the phone never reach the GUI" rather than as a stopped timeline.
    """
    avrcp, role = _avrcp()
    calls = []

    def boom():
        calls.append(len(calls))
        if len(calls) <= 2:
            raise RuntimeError("metadata role went away mid-tick")

    avrcp._tick_once = boom

    async def drive():
        task = asyncio.ensure_future(avrcp._progress_ticker())
        # Four ticks' worth of wall clock; the first two raise, the rest must still be attempted.
        await asyncio.sleep(avrcp_mod.PROGRESS_TICK_S * 4.5)
        assert not task.done(), "the ticker exited on a failing tick"
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(drive())
    assert len(calls) >= 4, f"ticker stopped calling _tick_once after a raise (got {len(calls)})"


def test_ensure_ticker_revives_a_dead_task():
    """Even a ticker lost to outside cancellation comes back on the next player bind."""
    avrcp, _ = _avrcp()

    async def drive():
        avrcp._ensure_ticker()
        first = avrcp._ticker_task
        first.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await first
        assert first.done()

        avrcp._ensure_ticker()
        assert avrcp._ticker_task is not first, "_ensure_ticker did not replace the dead task"
        assert not avrcp._ticker_task.done()
        avrcp._ticker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await avrcp._ticker_task

    asyncio.run(drive())


def test_ensure_ticker_does_not_double_start_a_live_ticker():
    avrcp, _ = _avrcp()

    async def drive():
        avrcp._ensure_ticker()
        live = avrcp._ticker_task
        avrcp._ensure_ticker()
        assert avrcp._ticker_task is live, "a second ticker was started alongside a live one"
        live.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await live

    asyncio.run(drive())


def test_tick_holds_instead_of_publishing_past_the_end_of_the_track():
    avrcp, role = _avrcp()
    avrcp._playing = True
    avrcp._duration_ms = 10_000
    avrcp._anchor_pos_ms = 9_500
    avrcp._anchor_at = time.monotonic() - 5.0        # extrapolates to 14_500
    before = len(role.set_metadata_calls)
    avrcp._tick_once()
    assert len(role.set_metadata_calls) == before, "published a position past the end of the track"


# -- session state must not outlive the AVRCP player ---------------------------------------------


def test_player_teardown_clears_the_anchor():
    """Observed on hardware: a reconnect hours later resurrected a four-hour-old position.

    The device honestly reported 0, and the paused-device-lie guard preferred the ancient anchor.
    """
    avrcp, _ = _avrcp()
    avrcp._playing = True
    avrcp._anchor_progress(301_297, 400_346)
    avrcp._last_title = "Never More"
    assert avrcp._expected_position_ms() is not None

    # Set the path directly rather than via present=True: binding also schedules the D-Bus seed,
    # which needs a running loop and is not what this test is about.
    avrcp._player_path = "/org/bluez/hci0/dev_X/player0"
    avrcp._on_object("/org/bluez/hci0/dev_X/player0", [avrcp_mod.PLAYER_IFACE], False)

    assert avrcp._expected_position_ms() is None, "the anchor survived the player object"
    assert avrcp._duration_ms == 0
    assert avrcp._last_title is None
    assert avrcp._playing is False


# -- a repeated Position is not a seek -----------------------------------------------------------


def test_repeated_position_matching_our_anchor_is_not_a_seek():
    """BlueZ re-emits every property when it recreates the player; that is not a jump."""
    avrcp, role = _avrcp()
    avrcp._playing = False
    avrcp._anchor_progress(301_297, 400_346)
    before = len(role.set_metadata_calls)

    avrcp._apply_position_signal(301_297)
    assert len(role.set_metadata_calls) == before, "a redundant Position was republished as a seek"


def test_a_real_seek_is_still_honoured_after_the_dedupe():
    avrcp, role = _avrcp()
    avrcp._playing = False
    avrcp._duration_ms = 400_346
    avrcp._anchor_progress(301_297, 400_346)

    avrcp._apply_position_signal(120_000)             # user scrubbed back
    assert role.set_metadata_calls[-1].track_progress == 120_000


def test_a_seek_arriving_before_any_duration_is_held_then_applied():
    """BlueZ does not guarantee Track precedes Position in the burst it emits for a new player.

    Without a duration _anchor_progress drops the value outright, so the first seek of a session
    would be lost — a wider window now that teardown clears the duration.
    """
    avrcp, role = _avrcp()

    async def drive():
        avrcp._apply_position_signal(45_000)
        assert role.set_metadata_calls == [], "anchored a position with no known track length"
        assert avrcp._pending_position_ms == 45_000

        # _apply_track schedules a re-anchor, so it needs a running loop.
        avrcp._apply_track({"Title": "Wish I Could Forget", "Duration": 206_626})
        assert role.set_metadata_calls[-1].track_progress == 45_000
        assert avrcp._pending_position_ms is None

    asyncio.run(drive())


def test_first_position_of_a_session_is_always_anchored():
    """With no anchor of our own there is nothing to compare against — take the device's word."""
    avrcp, role = _avrcp()
    avrcp._duration_ms = 400_346
    avrcp._apply_position_signal(32_320)
    assert role.set_metadata_calls[-1].track_progress == 32_320


# -- cover-art bus liveness ----------------------------------------------------------------------


@pytest.mark.skipif(
    avrcp_mod is None, reason="needs the real modules"
)
def test_coverart_reconnects_when_its_private_bus_died():
    """A cached MessageBus whose daemon was replaced must be dropped, not reused.

    Our obex daemons are respooled as a SET (any endpoint edit does it), so the private dbus-daemon
    behind the cover-art bus gets replaced. dbus-next does not raise on that — verified on hardware:
    `connected` flips to False within 0.1 s, but every introspect on the dead bus HANGS to timeout.
    Reusing it therefore killed album art permanently while logging only "the device may not support
    AVRCP cover art". Observed 2026-07-29 after obexd lost a bus-name race and the set restarted.
    """
    import importlib
    ca_mod = importlib.import_module("sources.bluetooth_coverart")

    class DeadBus:
        connected = False
        def __init__(self): self.disconnected = False
        def disconnect(self): self.disconnected = True

    ca = ca_mod.BluetoothCoverArt.__new__(ca_mod.BluetoothCoverArt)
    ca.instance_id = "1"
    ca.bus_address = "unix:path=/nonexistent/obex.socket"
    dead = DeadBus()
    ca._bus = dead
    ca._session_path = "/org/bluez/obex/session0"
    ca._session_device = "AA:BB:CC:DD:EE:FF"
    ca._image_iface = object()
    ca._fetch_method = "call_get_thumbnail"
    ca._warned_unavailable = True

    # The reconnect attempt fails (no such socket), but what matters is that the corpse was released
    # and the session state that lived on it was forgotten.
    assert asyncio.run(ca._connect()) is False
    assert dead.disconnected, "the dead bus was not disconnected"
    assert ca._bus is None, "the dead bus was kept and would be reused"
    assert ca._session_path is None and ca._session_device is None
    # The interface proxy belonged to the dead bus; keeping it would hand a corpse to the next
    # fetch. (This asserted on `_image` — a typo'd stray attribute that was never the one the
    # fetch path reads, so the check passed while the real field was left dangling.)
    assert ca._image_iface is None and ca._fetch_method is None


def test_coverart_reuses_a_live_bus():
    """The liveness check must not cost a reconnect on every track."""
    import importlib
    ca_mod = importlib.import_module("sources.bluetooth_coverart")

    class LiveBus:
        connected = True
        def disconnect(self): raise AssertionError("disconnected a live bus")

    ca = ca_mod.BluetoothCoverArt.__new__(ca_mod.BluetoothCoverArt)
    ca.instance_id = "1"
    ca._bus = LiveBus()
    ca._session_path = "/org/bluez/obex/session0"
    assert asyncio.run(ca._connect()) is True
    assert ca._session_path == "/org/bluez/obex/session0", "a live bus lost its session"


# -- adopting a player via PropertiesChanged must still seed it -----------------------------------


def test_props_adoption_seeds_the_player(monkeypatch):
    """A player whose first signal is PropertiesChanged (not InterfacesAdded) must still be seeded.

    Hardware 2026-07-29: reconnecting a phone with Device1.ConnectProfile(A2DP) delivered the
    player's PropertiesChanged ahead of InterfacesAdded. The adoption branch recorded the path and
    nothing else, so _seed_from_player never ran — and everything only it reads was silently lost:
    repeat/shuffle support (the GUI hid both controls) and ObexPort (album art never even ATTEMPTED
    a fetch, so the log showed no failures at all). Track/Status/Position kept flowing through these
    same signals, so the source looked perfectly healthy.
    """
    avrcp, _ = _avrcp()
    path = "/org/bluez/hci0/dev_X/player0"
    seeded = []

    async def fake_seed():
        seeded.append(avrcp._player_path)

    async def drive():
        monkeypatch.setattr(avrcp, "_seed_from_player", fake_seed)
        monkeypatch.setattr(avrcp, "_ensure_ticker", lambda: None)
        avrcp._on_props(path, avrcp_mod.PLAYER_IFACE, {})
        await asyncio.sleep(0)  # let the scheduled seed run

    asyncio.run(drive())

    assert avrcp._player_path == path
    assert seeded == [path], "adopting a player via PropertiesChanged did not seed it"


def test_props_adoption_does_not_reseed_a_bound_player():
    """Only the FIRST sighting adopts; every later signal on the same path must be a plain update."""
    avrcp, _ = _avrcp()
    path = "/org/bluez/hci0/dev_X/player0"
    avrcp._player_path = path
    calls = []
    avrcp._seed_from_player = lambda: calls.append(1)  # would raise if awaited/scheduled

    avrcp._on_props(path, avrcp_mod.PLAYER_IFACE, {})

    assert calls == [], "re-seeded a player that was already bound"


# -- a player bind must rebuild the cover-art session ---------------------------------------------


def test_player_bind_rebuilds_the_cover_art_session(monkeypatch):
    """Reset BEFORE prepare, in that order — prepare() is a no-op while a session is cached.

    Hardware 2026-07-30 (.201.113): bluetoothd was replaced under a running server. Its objects did
    NOT depart with InterfacesRemoved, so there was no player-gone event to invalidate anything; the
    player rebound cleanly the next morning and prepare() returned early on the previous day's dead
    session path. The phone withholds ImgHandle without a live BIP session, so no fetch was ever
    attempted and the artwork role served the previous day's image against every new track — with
    nothing in the log, because nothing was tried.
    """
    avrcp, _ = _avrcp()
    calls = []

    class FakeCoverArt:
        async def reset_for_new_session(self):
            calls.append("reset")

        async def prepare(self, address, obex_port):
            calls.append(("prepare", address, obex_port))
            return True

    avrcp.cover_art = FakeCoverArt()
    avrcp._obex_port = 4105
    monkeypatch.setattr(avrcp_mod, "ART_REFRESH_DELAYS_S", ())  # do not sleep in a unit test

    asyncio.run(avrcp._reopen_cover_art())

    assert calls == ["reset", ("prepare", avrcp.adapter.active_address, 4105)], (
        "cover art must be reset before prepare, or the stale session makes prepare a no-op"
    )


def test_cover_art_prepare_is_retried_until_our_obexd_is_up(monkeypatch):
    """Losing the race against our own obexd must not be permanent.

    The source manager starts obexd independently of the player bind — 10 s apart on `.201.113`,
    where prepare() failed on a bus with no org.bluez.obex yet. Nothing else calls prepare(), and
    without a session the phone withholds ImgHandle, so handle_track never runs to retry: one lost
    race meant no album art for the life of the process.
    """
    avrcp, _ = _avrcp()
    attempts = []

    class LateCoverArt:
        async def reset_for_new_session(self):
            pass

        async def prepare(self, address, obex_port):
            attempts.append(obex_port)
            return len(attempts) >= 3  # obexd finally answers on the third try

    avrcp.cover_art = LateCoverArt()
    avrcp._obex_port = 4105
    monkeypatch.setattr(avrcp_mod, "COVER_ART_PREPARE_RETRY_S", 0)  # do not sleep in a unit test
    monkeypatch.setattr(avrcp_mod, "ART_REFRESH_DELAYS_S", ())

    asyncio.run(avrcp._reopen_cover_art())

    assert len(attempts) == 3, f"gave up after {len(attempts)} attempt(s); a lost race is permanent"


# -- cover art must refetch when the TRACK changes, not only when the handle does -----------------


def test_cover_art_is_keyed_on_the_track_not_just_the_handle():
    """iOS reuses one ImgHandle for whatever is playing, so the handle alone is not a track identity.

    Reported from hardware 2026-07-30: play, disconnect, skip several tracks on the phone, reconnect
    and play — metadata was correct and the artwork was still the one from before the disconnect,
    because the incoming handle matched the last one fetched and the fetch was deduped away.
    """
    avrcp, _ = _avrcp()
    seen = []

    class FakeCoverArt:
        def note_track(self, track_key):
            pass

        async def handle_track(self, address, obex_port, img_handle, track_key=""):
            seen.append((img_handle, track_key))

    avrcp.cover_art = FakeCoverArt()
    avrcp._obex_port = 4105

    async def drive():
        avrcp._apply_track({"Title": "First", "Album": "A", "TrackNumber": 1, "ImgHandle": "1000097",
                            "Duration": 200_000})
        avrcp._apply_track({"Title": "Second", "Album": "B", "TrackNumber": 2, "ImgHandle": "1000097",
                            "Duration": 200_000})
        await asyncio.sleep(0)

    asyncio.run(drive())

    assert len(seen) == 2, "both tracks must reach the cover-art fetcher"
    assert seen[0][0] == seen[1][0] == "1000097", "the phone reused the handle, as iOS does"
    assert seen[0][1] != seen[1][1], (
        "the two tracks produced the SAME cover-art key, so the second fetch will be deduped away "
        "and the previous track's artwork will stay on screen"
    )


# -- stale art is worse than none -----------------------------------------------------------------


def test_a_track_with_no_art_clears_the_previous_cover():
    """A new track we cannot fetch art for must not keep showing the last album's cover."""
    import importlib
    ca_mod = importlib.import_module("sources.bluetooth_coverart")

    class Role:
        def __init__(self): self.calls = []
        async def set_album_artwork(self, image): self.calls.append(image)

    class Group:
        def __init__(self, role): self._role = role
        def group_role(self, name): return self._role if name == "artwork" else None

    role = Role()
    # Real constructor: it only assigns fields (no I/O), so the test cannot drift out of sync with
    # __init__ the way hand-built instances do.
    ca = ca_mod.BluetoothCoverArt(Group(role), "1", "unix:path=/nonexistent/obex.socket")
    ca._shown_key = "old|Previous Track|Old Album|1"

    async def drive():
        ca_mod.ART_CLEAR_GRACE_S = 0
        ca.note_track("new|Fresh Track|New Album|2")
        await asyncio.sleep(0.05)

    asyncio.run(drive())

    assert role.calls == [None], "the previous album's cover survived a track change with no art"


def test_art_that_arrives_within_the_grace_period_cancels_the_clear():
    """The clear must not blink art off when ImgHandle simply arrived a moment after the title."""
    import importlib
    ca_mod = importlib.import_module("sources.bluetooth_coverart")

    class Role:
        def __init__(self): self.calls = []
        async def set_album_artwork(self, image): self.calls.append(image)

    class Group:
        def __init__(self, role): self._role = role
        def group_role(self, name): return self._role if name == "artwork" else None

    role = Role()
    ca = ca_mod.BluetoothCoverArt(Group(role), "1", "unix:path=/nonexistent/obex.socket")
    ca._shown_key = "old|Previous|Old|1"

    async def drive():
        ca_mod.ART_CLEAR_GRACE_S = 0.2
        ca.note_track("new|Fresh|New|2")
        ca._shown_key = "new|Fresh|New|2"      # the fetch landed first
        await asyncio.sleep(0.35)

    asyncio.run(drive())

    assert role.calls == [], "art for the new track landed, but the pending clear still fired"
