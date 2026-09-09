"""Unit tests for the About/version REST surface.

The app's own version comes straight from env (baked in at image build time by docker/build.sh); the
dependency versions are queried live, so these tests fake the probes rather than the environment they
read from — a real rig has none of shairport-sync/go-librespot/dpkg-query available on a dev machine.

Run: `pytest tests/Unit/test_about_api.py`.
"""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "scripts"))
sys.path.insert(0, str(REPO / "backend" / "scripts" / "apis"))

flask = pytest.importorskip("flask", reason="Flask is a real runtime dep; skipped on a bare checkout")

import about_api  # noqa: E402
from about_api import create_about_blueprint  # noqa: E402


@pytest.fixture
def client():
    app = flask.Flask(__name__)
    app.register_blueprint(create_about_blueprint())
    return app.test_client()


def test_app_version_comes_from_env(client, monkeypatch):
    monkeypatch.setenv("PLUM_APP_VERSION", "0.4.2")
    monkeypatch.setenv("PLUM_BUILD_TYPE", "release")
    monkeypatch.setenv("PLUM_GIT_DESCRIBE", "v0.4.2")

    body = client.get("/api/about/versions").get_json()

    assert body["app"] == {"version": "0.4.2", "buildType": "release", "gitDescribe": "v0.4.2"}


def test_app_version_defaults_to_dev_when_unset(client, monkeypatch):
    monkeypatch.delenv("PLUM_APP_VERSION", raising=False)
    monkeypatch.delenv("PLUM_BUILD_TYPE", raising=False)

    body = client.get("/api/about/versions").get_json()

    assert body["app"]["version"] == "0.0.0-dev"
    assert body["app"]["buildType"] == "dev"


def test_aiosendspin_version_absent_reports_none(client, monkeypatch):
    def _raise(_name):
        raise about_api.md.PackageNotFoundError

    monkeypatch.setattr(about_api.md, "version", _raise)

    body = client.get("/api/about/versions").get_json()

    assert body["sendspin"]["aiosendspin"] is None


def test_shairport_sync_version_is_parsed_from_the_dash_flag_output(client, monkeypatch):
    monkeypatch.setattr(
        about_api,
        "_run_version",
        lambda argv: "4.3.7-OpenSSL-Avahi-ALSA-soxr-metadata-sysconfdir:/etc-mpris"
        if argv[0] == "shairport-sync"
        else None,
    )

    body = client.get("/api/about/versions").get_json()

    assert body["airplay"]["shairportSync"] == "4.3.7"


def test_go_librespot_falls_back_to_the_baked_in_env_when_the_binary_cannot_answer(client, monkeypatch):
    monkeypatch.setattr(about_api, "_run_version", lambda argv: None)
    monkeypatch.setenv("PLUM_GO_LIBRESPOT_VERSION", "0.7.4")

    body = client.get("/api/about/versions").get_json()

    assert body["spotify"]["goLibrespot"] == "0.7.4"


def test_go_librespot_error_output_is_not_mistaken_for_a_version(client, monkeypatch):
    """Measured on .7.200: go-librespot 0.7.4 has no --version flag, so the probe came back with
    `level=fatal msg="failed loading config" error="unknown flag: --version"`. That string is
    truthy, so it satisfied the fallback `or` and the whole log line rendered in the About panel
    where a version belongs."""
    monkeypatch.setattr(
        about_api,
        "_run_version",
        lambda argv: 'time="2026-09-08T18:03:35-07:00" level=fatal msg="failed loading config" '
        'error="unknown flag: --version"'
        if argv[0] == "go-librespot"
        else None,
    )
    monkeypatch.setenv("PLUM_GO_LIBRESPOT_VERSION", "0.7.4")

    body = client.get("/api/about/versions").get_json()

    assert body["spotify"]["goLibrespot"] == "0.7.4"


def test_go_librespot_reports_a_real_version_when_the_binary_gains_the_flag(client, monkeypatch):
    """And the leading "v" must not eat the major: `\b` cannot open the pattern, because there is
    no word boundary inside "v0.7.4" — it would report 7.4."""
    monkeypatch.setattr(
        about_api, "_run_version", lambda argv: "go-librespot v0.8.0 (commit abc1234)" if argv[0] == "go-librespot" else None
    )
    monkeypatch.setenv("PLUM_GO_LIBRESPOT_VERSION", "0.7.4")

    body = client.get("/api/about/versions").get_json()

    assert body["spotify"]["goLibrespot"] == "0.8.0"


def test_a_probe_that_cannot_find_its_binary_reports_none_not_an_error(client, monkeypatch):
    monkeypatch.setattr(about_api.shutil, "which", lambda _exe: None)
    monkeypatch.delenv("PLUM_GO_LIBRESPOT_VERSION", raising=False)

    response = client.get("/api/about/versions")

    assert response.status_code == 200
    body = response.get_json()
    assert body["airplay"]["shairportSync"] is None
    assert body["bluetooth"]["bluezAlsa"] is None
