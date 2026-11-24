"""Unit tests for the request guard both APIs share (`cors_policy.guard`).

CORS alone only stops a page from READING a reply. The handler still ran, so a `text/plain` POST
from any web page re-routed audio blind. And a websocket upgrade is not subject to CORS at all, so
any page could open `/api/mesh/consume` and read the cached pairing PIN. The guard refuses before
the handler.

Most of these tests guard the ALLOW side, for the same reason test_cors_policy.py does: the
failure mode of this change is a GUI that is dead on every unit at once.

`test_every_legitimate_caller_passes` lists each real caller and the headers it sends:
the GUI through nginx, the GUI direct to a peer's :5001, a peer's aiohttp client, the loopback
player and the Home Assistant integration.

Run: `pytest tests/Unit/test_api_guard.py`.
"""

import asyncio
import json
import sys
from pathlib import Path

import flask
import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "scripts"))
sys.path.insert(0, str(REPO / "backend" / "scripts" / "apis"))

import cors_policy  # noqa: E402
import server as flask_server  # noqa: E402
from cors_policy import guard, known_hosts  # noqa: E402
from mesh.api import MeshApi, make_cors_middleware  # noqa: E402
from mesh.model import UnitSnapshot  # noqa: E402

HOSTS = known_hosts(
    [
        UnitSnapshot("unit-10021", "Plum Amp100", host="198.51.100.21", hostname="plum-amp100"),
        UnitSnapshot("unit-10020", "Plum Interop", host="198.51.100.20", hostname="plum-interop"),
    ]
)
JSON = "application/json"


@pytest.fixture(autouse=True)
def _guard_on(monkeypatch):
    monkeypatch.delenv("PLUM_API_GUARD", raising=False)
    monkeypatch.delenv("PLUM_ALLOWED_ORIGINS", raising=False)


def check(
    method="GET", origin=None, host="198.51.100.21:5001", content_type=None, has_body=False, extras=None, hosts=None
):
    return guard(
        method=method,
        origin=origin,
        host=host,
        content_type=content_type,
        has_body=has_body,
        hosts=HOSTS if hosts is None else hosts,
        extras=[] if extras is None else extras,
    )


# -- what must pass ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("who", "kwargs"),
    [
        ("GUI GET through nginx, by name", {"host": "plum-amp100.local"}),
        ("GUI GET through nginx, by IP", {"host": "198.51.100.21"}),
        (
            "GUI POST through nginx (same-origin POST carries Origin)",
            {
                "method": "POST",
                "origin": "http://plum-amp100.local",
                "host": "plum-amp100.local",
                "content_type": JSON,
                "has_body": True,
            },
        ),
        (
            "GUI POST direct to a PEER's :5001 from this unit's page",
            {
                "method": "POST",
                "origin": "http://plum-amp100.local",
                "host": "198.51.100.20:5001",
                "content_type": JSON,
                "has_body": True,
            },
        ),
        (
            "the preflight for that POST",
            {"method": "OPTIONS", "origin": "http://plum-amp100.local", "host": "198.51.100.20:5001"},
        ),
        ("peer snapshot poll (aiohttp client, no Origin)", {"host": "198.51.100.20:5001"}),
        (
            "peer delegated route (aiohttp json=)",
            {"method": "POST", "host": "198.51.100.20:5001", "content_type": JSON, "has_body": True},
        ),
        (
            "loopback player-state POST",
            {"method": "POST", "host": "127.0.0.1:5001", "content_type": JSON, "has_body": True},
        ),
        ("loopback player relay websocket", {"host": "127.0.0.1:5001"}),
        (
            "Home Assistant (aiohttp json=, by IP)",
            {"method": "POST", "host": "198.51.100.21:5001", "content_type": JSON, "has_body": True},
        ),
        ("GUI websocket through nginx", {"origin": "http://198.51.100.21", "host": "198.51.100.21"}),
        ("a POST with no body and no content type (tone/stop)", {"method": "POST", "origin": "http://198.51.100.21"}),
        (
            "a POST with no body but a JSON content type (integrations enable)",
            {"method": "POST", "origin": "http://198.51.100.21", "content_type": JSON},
        ),
        ("a DELETE with no body", {"method": "DELETE", "origin": "http://198.51.100.21"}),
        (
            "a JSON content type with a charset",
            {"method": "PUT", "content_type": "Application/JSON; charset=utf-8", "has_body": True},
        ),
        ("an IPv6 literal host", {"host": "[fe80::1]:5001"}),
        ("a client that sends no Host (HTTP/1.0)", {"host": None}),
        ("the dev server on loopback", {"origin": "http://localhost:5173", "host": "localhost:5173"}),
    ],
)
def test_every_legitimate_caller_passes(who, kwargs):
    assert check(**kwargs) is None, who


def test_a_configured_extra_origin_passes_and_its_host_is_trusted():
    extras = ["https://audio.example.net"]
    assert check(origin="https://audio.example.net", host="audio.example.net", extras=extras) is None
    assert check(host="audio.example.net", extras=extras) is None  # the same-origin GET: no Origin
    assert check(host="other.example.net", extras=extras) is not None


def test_the_wildcard_allows_every_origin_and_host():
    assert check(origin="http://anything", host="anything", extras=["*"]) is None
    assert check(host="anything", extras=["*"]) is None


def test_the_switch_turns_the_whole_guard_off(monkeypatch):
    monkeypatch.setenv("PLUM_API_GUARD", "0")
    assert check(method="POST", origin="http://evil.example", content_type="text/plain", has_body=True) is None
    assert check(host="evil.example") is None


# -- what must be refused ----------------------------------------------------------------------


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE", "OPTIONS"])
def test_an_unknown_origin_is_refused_for_every_method(method):
    status, message = check(method=method, origin="http://198.51.100.40", content_type=JSON, has_body=True)
    assert status == 403
    assert "PLUM_ALLOWED_ORIGINS" in message


def test_a_null_origin_is_refused():
    """A sandboxed iframe and a file:// page both send `Origin: null`."""
    assert check(method="POST", origin="null", content_type=JSON, has_body=True)[0] == 403


def test_dns_rebinding_is_refused():
    """The page is at evil.example, which now resolves to the unit. A GET carries no Origin at all."""
    assert check(host="evil.example")[0] == 403
    assert check(host="evil.example:5001")[0] == 403
    # The POST form carries an Origin that looks same-origin. It is still not a host we know.
    assert check(method="POST", origin="http://evil.example", host="evil.example")[0] == 403
    assert check(host="plum-amp100.evil.example")[0] == 403


@pytest.mark.parametrize("junk", ["", "a b", "[::1", "http://x/", "exa mple:80"])
def test_a_malformed_host_is_refused_without_raising(junk):
    assert check(host=junk)[0] == 403


@pytest.mark.parametrize("content_type", [None, "", "text/plain", "application/x-www-form-urlencoded", "text/json"])
@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
def test_a_body_that_is_not_json_is_refused(method, content_type):
    """The blind cross-site POST: `text/plain` needs no preflight, and `_json` parsed it anyway."""
    assert check(method=method, content_type=content_type, has_body=True)[0] == 415
    assert check(method=method, origin="http://198.51.100.21", content_type=content_type, has_body=True)[0] == 415


# -- own hosts: the unit's mDNS names ----------------------------------------------------------


def test_own_hosts_holds_the_mdns_name_from_settings(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hostname": "Kitchen-Amp"}))
    monkeypatch.setenv("PLUM_SETTINGS_FILE", str(settings))
    hosts = cors_policy.own_hosts()
    assert {"kitchen-amp", "kitchen-amp.local"} <= hosts
    assert {"localhost", "127.0.0.1"} <= hosts

    # A rename applies live: the next request sees the new name.
    settings.write_text(json.dumps({"hostname": "hallway"}))
    import os

    os.utime(settings, ns=(1, 1))
    assert "hallway.local" in cors_policy.own_hosts()


@pytest.mark.parametrize("content", ["{ not json", "[]", json.dumps({"hostname": 7}), json.dumps({"hostname": "a b"})])
def test_own_hosts_never_raises_on_a_bad_settings_file(tmp_path, monkeypatch, content):
    settings = tmp_path / "settings.json"
    settings.write_text(content)
    monkeypatch.setenv("PLUM_SETTINGS_FILE", str(settings))
    assert "localhost" in cors_policy.own_hosts()
    monkeypatch.setenv("PLUM_SETTINGS_FILE", str(tmp_path / "absent.json"))
    assert "localhost" in cors_policy.own_hosts()


# -- a peer's custom mDNS name ------------------------------------------------------------------
#
# `hostname` on the snapshot is socket.gethostname(). Settings -> Device gives Avahi a different
# name (SetHostName) and stores it in settings.json. The page is then at http://<that-name>.local,
# and a PEER refused every call from it (403): the peer knew the unit by its kernel name only.


def _renamed_peer(**kwargs):
    return UnitSnapshot("unit-10022", "Kitchen", host="198.51.100.22", hostname="plum-pi4-02", **kwargs)


def test_a_peer_with_a_custom_mdns_name_is_allowed_as_origin():
    hosts = known_hosts([_renamed_peer(mdns_hostname="Kitchen-Amp")])
    for origin in ("http://kitchen-amp.local", "http://kitchen-amp", "http://kitchen-amp.local:5173"):
        assert check(method="POST", origin=origin, content_type=JSON, has_body=True, hosts=hosts) is None, origin
    # The same page's same-origin GET carries the name in Host and no Origin.
    assert check(host="kitchen-amp.local", hosts=hosts) is None


def test_an_unknown_name_is_still_refused_beside_a_custom_name():
    hosts = known_hosts([_renamed_peer(mdns_hostname="kitchen-amp")])
    for origin in ("http://evil.example", "http://kitchen-amp.evil.example", "http://kitchen.local"):
        verdict = check(origin=origin, hosts=hosts)
        assert verdict is not None and verdict[0] == 403, origin
    assert check(host="evil.example", hosts=hosts)[0] == 403


@pytest.mark.parametrize("junk", ["evil.example", "a b", "-lead", "x" * 64, "", 7, ["kitchen"], {"a": 1}, None])
def test_a_peers_mdns_name_that_is_not_one_label_adds_nothing(junk):
    """A snapshot is a peer's JSON. Only one DNS label is a name, so a peer cannot add a domain."""
    snap = UnitSnapshot.from_dict({"unit_id": "unit-x", "host": "198.51.100.30", "mdns_hostname": junk})
    assert known_hosts([snap]) == set(cors_policy.LOOPBACK_HOSTS) | {"198.51.100.30"}
    # The same value on an object that did not come through from_dict.
    direct = UnitSnapshot("unit-x", "X", host="198.51.100.30", mdns_hostname=junk)
    assert known_hosts([direct]) == set(cors_policy.LOOPBACK_HOSTS) | {"198.51.100.30"}


def test_an_old_snapshot_without_the_field_still_parses():
    """A peer on an older image sends no `mdns_hostname`. Its IP and kernel name stay allowed."""
    old = {"unit_id": "unit-old", "name": "Old", "host": "198.51.100.11", "hostname": "plum-old"}
    snap = UnitSnapshot.from_dict(old)
    assert snap.mdns_hostname is None
    assert {"198.51.100.11", "plum-old.local"} <= known_hosts([snap])


def test_the_mdns_name_survives_the_wire():
    snap = _renamed_peer(mdns_hostname="kitchen-amp")
    assert snap.to_dict()["mdns_hostname"] == "kitchen-amp"
    assert UnitSnapshot.from_dict(snap.to_dict()).mdns_hostname == "kitchen-amp"


def test_mdns_hostname_reads_the_settings_value(tmp_path, monkeypatch):
    import os

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hostname": "Kitchen-Amp"}))
    monkeypatch.setenv("PLUM_SETTINGS_FILE", str(settings))
    assert cors_policy.mdns_hostname() == "kitchen-amp"
    settings.write_text(json.dumps({"hostname": "a b"}))
    os.utime(settings, ns=(2, 2))
    assert cors_policy.mdns_hostname() is None
    monkeypatch.setenv("PLUM_SETTINGS_FILE", str(tmp_path / "absent.json"))
    assert cors_policy.mdns_hostname() is None


# -- the aiohttp middleware ---------------------------------------------------------------------


def _through_middleware(method, path="/api/mesh/route", headers=None):
    """Run one request through the mesh middleware. Returns (response, did the handler run)."""
    ran = []

    async def handler(_request):
        ran.append(True)
        return web.json_response({"ok": True})

    async def drive():
        middleware = make_cors_middleware(lambda: HOSTS)
        request = make_mocked_request(method, path, headers=headers or {})
        return await middleware(request, handler)

    return asyncio.run(drive()), bool(ran)


def test_the_handler_does_not_run_for_a_refused_origin():
    """The finding: the handler ran first and only the CORS headers were withheld."""
    headers = {"Origin": "http://evil.example", "Host": "198.51.100.21:5001", "Content-Type": "text/plain"}
    response, ran = _through_middleware("POST", headers={**headers, "Content-Length": "40"})
    assert response.status == 403
    assert ran is False
    assert "Access-Control-Allow-Origin" not in response.headers


def test_a_websocket_upgrade_from_an_unknown_origin_is_refused():
    """A websocket is not subject to CORS, and /consume replays the cached pairing PIN on connect."""
    headers = {
        "Origin": "http://evil.example",
        "Host": "198.51.100.21:5001",
        "Upgrade": "websocket",
        "Connection": "Upgrade",
    }
    response, ran = _through_middleware("GET", "/api/mesh/consume", headers)
    assert (response.status, ran) == (403, False)


def test_a_simple_text_plain_post_with_no_origin_is_refused():
    headers = {"Host": "198.51.100.21:5001", "Content-Type": "text/plain", "Content-Length": "40"}
    response, ran = _through_middleware("POST", headers=headers)
    assert (response.status, ran) == (415, False)


def test_a_rebound_host_is_refused_by_the_middleware():
    response, ran = _through_middleware("GET", "/api/mesh/view", {"Host": "evil.example:5001"})
    assert (response.status, ran) == (403, False)


def test_the_gui_post_still_runs_and_gets_its_cors_headers():
    headers = {
        "Origin": "http://plum-amp100.local",
        "Host": "198.51.100.20:5001",
        "Content-Type": "application/json",
        "Content-Length": "40",
    }
    response, ran = _through_middleware("POST", headers=headers)
    assert (response.status, ran) == (200, True)
    assert response.headers["Access-Control-Allow-Origin"] == "http://plum-amp100.local"


def test_a_peer_request_with_no_origin_still_runs():
    headers = {"Host": "198.51.100.20:5001", "Content-Type": "application/json", "Content-Length": "40"}
    response, ran = _through_middleware("POST", headers=headers)
    assert (response.status, ran) == (200, True)
    assert "Access-Control-Allow-Origin" not in response.headers


def test_the_middleware_passes_everything_when_the_guard_is_off(monkeypatch):
    monkeypatch.setenv("PLUM_API_GUARD", "0")
    headers = {"Origin": "http://evil.example", "Host": "evil.example", "Content-Type": "text/plain"}
    response, ran = _through_middleware("POST", headers={**headers, "Content-Length": "40"})
    assert (response.status, ran) == (200, True)
    assert "Access-Control-Allow-Origin" not in response.headers  # CORS itself is unchanged


def test_an_error_raised_by_a_handler_keeps_its_cors_headers():
    """Without them the GUI cannot read WHY a request was refused; it sees a network error."""

    async def drive():
        async def handler(_request):
            raise web.HTTPBadRequest(text="{}", content_type="application/json")

        middleware = make_cors_middleware(lambda: HOSTS)
        request = make_mocked_request("GET", "/x", headers={"Origin": "http://198.51.100.21", "Host": "198.51.100.21"})
        return await middleware(request, handler)

    response = asyncio.run(drive())
    assert response.status == 400
    assert response.headers["Access-Control-Allow-Origin"] == "http://198.51.100.21"


# -- _json -------------------------------------------------------------------------------------


class _Body:
    def __init__(self, value=None, raises=False):
        self._value, self._raises = value, raises

    async def json(self):
        if self._raises:
            raise ValueError("not json")
        return self._value


@pytest.mark.parametrize("body", [[1, 2], "text", 5, None, True])
def test_json_refuses_a_body_that_is_not_an_object(body):
    """Every handler calls `body.get(...)`. A JSON array made that a 500 with a traceback."""
    with pytest.raises(web.HTTPBadRequest) as caught:
        asyncio.run(MeshApi._json(_Body(body)))
    assert json.loads(caught.value.text)["error"]


def test_json_still_reads_an_object_and_tolerates_an_empty_body():
    assert asyncio.run(MeshApi._json(_Body({"a": 1}))) == {"a": 1}
    assert asyncio.run(MeshApi._json(_Body(raises=True))) == {}  # tone/stop posts no body


# -- the Flask side -----------------------------------------------------------------------------


@pytest.fixture()
def client():
    app = flask.Flask(__name__)
    flask_server.install_api_guard(app, lambda: HOSTS)

    @app.route("/api/settings", methods=["GET", "POST", "PUT", "DELETE"])
    def _settings():
        return flask.jsonify({"ok": True})

    return app.test_client()


def test_flask_refuses_an_unknown_origin_before_the_view(client):
    response = client.post("/api/settings", json={"a": 1}, headers={"Origin": "http://evil.example"})
    assert response.status_code == 403
    assert "error" in response.get_json()
    assert "Access-Control-Allow-Origin" not in response.headers


def test_flask_refuses_a_text_plain_body(client):
    response = client.post("/api/settings", data="{}", content_type="text/plain", headers={"Host": "198.51.100.21"})
    assert response.status_code == 415


def test_flask_refuses_a_rebound_host(client):
    assert client.get("/api/settings", headers={"Host": "evil.example"}).status_code == 403


def test_flask_passes_the_gui(client):
    headers = {"Origin": "http://plum-amp100.local", "Host": "plum-amp100.local"}
    response = client.post("/api/settings", json={"a": 1}, headers=headers)
    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == "http://plum-amp100.local"
    assert client.get("/api/settings", headers={"Host": "plum-amp100.local"}).status_code == 200
    assert client.delete("/api/settings", headers=headers).status_code == 200


def test_flask_passes_everything_when_the_guard_is_off(client, monkeypatch):
    monkeypatch.setenv("PLUM_API_GUARD", "0")
    response = client.post("/api/settings", data="{}", content_type="text/plain", headers={"Host": "evil.example"})
    assert response.status_code == 200
