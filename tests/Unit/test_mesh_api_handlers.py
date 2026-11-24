"""Unit tests for mesh API handlers, called directly with fake collaborators.

No socket and no event loop of their own: each test builds a `MeshApi` over fakes, calls one
handler coroutine and reads the reply. The request guard has its own file (test_api_guard.py).

Run: `pytest tests/Unit/test_mesh_api_handlers.py`.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "scripts"))

from mesh.api import MeshApi  # noqa: E402
from mesh.model import MeshView, PlayerState, UnitSnapshot  # noqa: E402
from speaker_names import SpeakerNames  # noqa: E402

URL = "ws://198.51.100.31:8928/sendspin"
FRIENDLY = "Home Assistant Voice PE - 01"


class Request:
    """The two things a handler reads: the JSON body and the query."""

    def __init__(self, body=None):
        self._body = {} if body is None else body
        self.query = {}
        self.headers = {}

    async def json(self):
        return self._body


class Neighbourhood:
    def __init__(self, players):
        self._players = players

    def to_dict(self):
        return {"players": [dict(p) for p in self._players], "servers": []}


class Engine:
    def __init__(self):
        self.released = []

    async def release_client(self, source_id, player_id, url=None):
        self.released.append((source_id, player_id, url))


class Router:
    def __init__(self):
        self.volumes = []

    async def set_volume(self, player_id, volume, muted):
        self.volumes.append((player_id, volume, muted))


class Aggregator:
    local_unit_id = "unit-1"

    def __init__(self, units=None):
        self.units = units or [UnitSnapshot("unit-1", "Unit 1", host="198.51.100.21")]

    def view(self):
        return MeshView(self.units)

    def local_snapshot(self):
        return self.units[0]

    async def refresh(self):
        return self.view()


@pytest.fixture()
def names_file(tmp_path, monkeypatch):
    path = tmp_path / "speaker_names.json"
    monkeypatch.setenv("PLUM_SPEAKER_NAMES_FILE", str(path))
    return str(path)


def make_api(*, units=None, neighbourhood=None):
    engine, router = Engine(), Router()
    api = MeshApi(engine, Aggregator(units), router, neighbourhood=neighbourhood)
    return api, engine, router


def call(handler, body=None):
    response = asyncio.run(handler(Request(body)))
    return response.status, json.loads(response.text)


def player(player_id, *, volume=100, muted=False, url=None):
    return PlayerState(
        player_id=player_id, name=player_id, connected=True, group_id=None, url=url, volume=volume, muted=muted
    )


# -- GET /api/mesh/neighbourhood: friendly names -------------------------------------------------


def test_neighbourhood_serves_a_name_the_server_learned_after_startup(names_file):
    """The bug: the API's own SpeakerNames read the file once, so this needed a restart."""
    idle = {"name": "home-assistant-voice-a1b2c3", "friendly_name": "home-assistant-voice-a1b2c3", "url": URL}
    api, _, _ = make_api(neighbourhood=Neighbourhood([idle]))
    _, before = call(api._neighbours)
    assert before["players"][0]["friendly_name"] == "home-assistant-voice-a1b2c3"

    SpeakerNames(names_file).learn(URL, FRIENDLY)  # what sendspin_server does when it attaches

    _, after = call(api._neighbours)
    assert after["players"][0]["friendly_name"] == FRIENDLY


def test_neighbourhood_matches_a_name_by_host_and_port(names_file):
    SpeakerNames(names_file).learn(URL, FRIENDLY)
    advertised = {"name": "x", "friendly_name": "x", "url": "ws://198.51.100.31:8928"}  # no path
    other = {"name": "y", "friendly_name": "y", "url": "ws://198.51.100.99:8928/sendspin"}
    no_url = {"name": "z", "friendly_name": "z"}
    api, _, _ = make_api(neighbourhood=Neighbourhood([advertised, other, no_url]))
    _, payload = call(api._neighbours)
    assert [p["friendly_name"] for p in payload["players"]] == [FRIENDLY, "y", "z"]


def test_neighbourhood_without_mdns_is_empty(names_file):
    api, _, _ = make_api()
    assert call(api._neighbours) == (200, {"players": [], "servers": []})


# -- POST /api/mesh/volume ---------------------------------------------------------------------
#
# The Home Assistant integration sends `volume` alone from a slider and `muted` alone from a mute
# button. Before: `muted` alone was a 400, and `volume` alone CLEARED mute (it defaulted to False),
# so moving the slider on a muted speaker made it play.


def _unit_with(*players, local_player=None):
    unit = UnitSnapshot("unit-1", "Unit 1", host="198.51.100.21", players=list(players))
    unit.local_player = local_player
    return [unit]


def test_volume_alone_keeps_the_current_mute(names_file):
    api, _, router = make_api(units=_unit_with(player("p1", volume=40, muted=True)))
    assert call(api._volume, {"player_id": "p1", "volume": 55}) == (200, {"ok": True})
    assert router.volumes == [("p1", 55, True)]


def test_volume_alone_on_a_player_that_is_not_muted_stays_not_muted(names_file):
    api, _, router = make_api(units=_unit_with(player("p1", volume=40, muted=False)))
    call(api._volume, {"player_id": "p1", "volume": 55})
    assert router.volumes == [("p1", 55, False)]


def test_muted_alone_keeps_the_current_volume(names_file):
    api, _, router = make_api(units=_unit_with(player("p1", volume=40, muted=False)))
    assert call(api._volume, {"player_id": "p1", "muted": True}) == (200, {"ok": True})
    assert call(api._volume, {"player_id": "p1", "muted": False}) == (200, {"ok": True})
    assert router.volumes == [("p1", 40, True), ("p1", 40, False)]


def test_the_old_request_shape_still_works(names_file):
    """The GUI sends all three fields. An explicit `muted: false` must still clear mute."""
    api, _, router = make_api(units=_unit_with(player("p1", volume=40, muted=True)))
    assert call(api._volume, {"player_id": "p1", "volume": 70, "muted": False}) == (200, {"ok": True})
    assert call(api._volume, {"player_id": "p1", "volume": 70, "muted": True}) == (200, {"ok": True})
    assert router.volumes == [("p1", 70, False), ("p1", 70, True)]


def test_a_null_field_reads_as_absent(names_file):
    api, _, router = make_api(units=_unit_with(player("p1", volume=40, muted=True)))
    call(api._volume, {"player_id": "p1", "volume": 30, "muted": None})
    call(api._volume, {"player_id": "p1", "volume": None, "muted": False})
    assert router.volumes == [("p1", 30, True), ("p1", 40, False)]


def test_the_level_of_a_player_on_a_peer_comes_from_the_mesh_view(names_file):
    units = [
        UnitSnapshot("unit-1", "Unit 1", host="198.51.100.21"),
        UnitSnapshot("unit-2", "Unit 2", host="198.51.100.22", players=[player("p9", volume=25, muted=True)]),
    ]
    api, _, router = make_api(units=units)
    call(api._volume, {"player_id": "p9", "volume": 60})
    assert router.volumes == [("p9", 60, True)]


def test_volume_alone_for_a_player_nobody_reports_keeps_the_old_default(names_file):
    """A released speaker is in no `players` list. Its level is held for the next connect."""
    api, _, router = make_api()
    assert call(api._volume, {"player_id": "idle", "volume": 60}) == (200, {"ok": True})
    assert router.volumes == [("idle", 60, False)]


def test_muted_alone_for_a_player_nobody_reports_is_refused(names_file):
    """There is no current volume to keep, and an invented one would move a real speaker."""
    api, _, router = make_api()
    status, payload = call(api._volume, {"player_id": "idle", "muted": True})
    assert status == 400
    assert "volume" in payload["error"]
    assert router.volumes == []


@pytest.mark.parametrize(
    "body", [{}, {"player_id": "p1"}, {"volume": 50}, {"muted": True}, {"player_id": "p1", "volume": "loud"}]
)
def test_a_volume_request_with_nothing_usable_is_refused(names_file, body):
    api, _, router = make_api(units=_unit_with(player("p1")))
    status, payload = call(api._volume, body)
    assert status == 400
    assert payload["error"]
    assert router.volumes == []


def test_the_loudness_matcher_hears_a_volume_change_but_not_a_mute(names_file):
    api, _, _ = make_api(units=_unit_with(player("p1", volume=40)))
    heard = []
    api.on_user_volume = lambda player_id, volume: heard.append((player_id, volume))
    call(api._volume, {"player_id": "p1", "muted": True})
    assert heard == []  # a mute is not a new level for the group to follow
    call(api._volume, {"player_id": "p1", "volume": 55})
    assert heard == [("p1", 55)]


# -- POST /api/mesh/release --------------------------------------------------------------------
#
# Home Assistant knows a speaker by its listener URL. It learns the handshake id only from the
# reply to an adopt, and it does not have that after a restart.


def test_release_by_url_alone_resolves_the_player(names_file):
    api, engine, _ = make_api(
        units=_unit_with(player("AA:BB", url=URL), player("other", url="ws://198.51.100.9:8928/x"))
    )
    assert call(api._release, {"source_id": "airplay-1", "url": URL}) == (200, {"ok": True, "player_id": "AA:BB"})
    assert engine.released == [("airplay-1", "AA:BB", URL)]


def test_release_by_url_matches_on_host_and_port(names_file):
    api, engine, _ = make_api(units=_unit_with(player("AA:BB", url=URL)))
    status, _ = call(api._release, {"source_id": "airplay-1", "url": "ws://198.51.100.31:8928"})
    assert status == 200
    assert engine.released == [("airplay-1", "AA:BB", "ws://198.51.100.31:8928")]


def test_the_old_release_shapes_still_work(names_file):
    api, engine, _ = make_api()
    assert call(api._release, {"source_id": "s", "player_id": "p1"})[0] == 200
    assert call(api._release, {"source_id": "s", "player_id": "p1", "url": URL})[0] == 200
    assert engine.released == [("s", "p1", None), ("s", "p1", URL)]


def test_release_by_a_url_no_attached_player_has_is_not_found(names_file):
    api, engine, _ = make_api(units=_unit_with(player("AA:BB", url=URL)))
    status, payload = call(api._release, {"source_id": "s", "url": "ws://198.51.100.77:8928/sendspin"})
    assert status == 404
    assert payload["error"]
    assert engine.released == []


@pytest.mark.parametrize("body", [{}, {"source_id": "s"}, {"url": URL}, {"player_id": "p1"}])
def test_a_release_with_nothing_to_identify_the_speaker_is_refused(names_file, body):
    api, engine, _ = make_api(units=_unit_with(player("AA:BB", url=URL)))
    assert call(api._release, body)[0] == 400
    assert engine.released == []
