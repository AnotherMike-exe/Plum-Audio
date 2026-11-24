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
    return PlayerState(player_id=player_id, name=player_id, url=url, volume=volume, muted=muted)


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
