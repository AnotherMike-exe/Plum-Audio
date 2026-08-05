"""Unit tests for the PLUM_PLAYER_CODEC override.

The override deviates from the spec ON PURPOSE, so the tests exist mainly to pin how far.

The spec says a client's `supported_formats` is in priority order — first is preferred — and the
server should take the first match it implements. aiosendspin does that, so UNSET must mean we touch
nothing at all; test_unset_is_the_spec_behaviour is the guard on that, and it is the important one.
The device is the authority on what it wants unless a human has explicitly overruled it for a unit.

The rest guard the blast radius: we can only ever select a codec the client itself advertised (the
library validates, and we must not paper over a False), an unparseable value must degrade to spec
behaviour rather than crash a source, and a device that cannot do the requested codec keeps its own
choice instead of being left with nothing.

Run: `pytest tests/Unit/test_player_codec_override.py`.
"""

import importlib
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "scripts"))

pytest.importorskip("aiosendspin", reason="aiosendspin is a real runtime dep")
pytest.importorskip("numpy", reason="sendspin_server imports numpy at module scope")

from aiosendspin.models.types import AudioCodec  # noqa: E402


class FakeRole:
    """Stands in for PlayerV1Role; records what set_preferred_format was asked for."""

    def __init__(self, supported=(AudioCodec.FLAC, AudioCodec.OPUS, AudioCodec.PCM)):
        self.supported = set(supported)
        self.calls = []

    def set_preferred_format(self, audio_format, codec=None):
        self.calls.append((audio_format, codec))
        return codec in self.supported  # the real one validates against the client's own list


class FakeClient:
    def __init__(self, role, connected=True):
        self._role = role
        self.is_connected = connected

    def roles_by_family(self, family):
        return [self._role] if family == "player" else []


def _server_module(monkeypatch, codec_env):
    """Import sendspin_server with PLUM_PLAYER_CODEC set, since it is read at module scope."""
    if codec_env is None:
        monkeypatch.delenv("PLUM_PLAYER_CODEC", raising=False)
    else:
        monkeypatch.setenv("PLUM_PLAYER_CODEC", codec_env)
    import sendspin_server

    return importlib.reload(sendspin_server)


def _engine(module, client, monkeypatch=None):
    # The code guards on isinstance(role, PlayerV1Role); point that name at our fake so the guard
    # stays real in production and the test still exercises the path.
    if monkeypatch is not None:
        monkeypatch.setattr(module, "PlayerV1Role", FakeRole)
    engine = module.PlumSendspinServer.__new__(module.PlumSendspinServer)
    engine.server = type("S", (), {"get_client": staticmethod(lambda _id: client)})()
    return engine


@pytest.fixture
def role():
    return FakeRole()


# -- the default ---------------------------------------------------------------------------------


def test_unset_is_the_spec_behaviour(monkeypatch, role):
    """No env, no interference: the client's own priority order stands, untouched."""
    module = _server_module(monkeypatch, None)
    assert module.PLAYER_CODEC_OVERRIDE is None

    _engine(module, FakeClient(role))._apply_codec_override("spk")
    assert role.calls == []  # we never even asked


def test_empty_and_whitespace_are_treated_as_unset(monkeypatch, role):
    for value in ("", "   "):
        module = _server_module(monkeypatch, value)
        assert module.PLAYER_CODEC_OVERRIDE is None
        _engine(module, FakeClient(role))._apply_codec_override("spk")
    assert role.calls == []


# -- the override --------------------------------------------------------------------------------


@pytest.mark.parametrize("value, expected", [("pcm", AudioCodec.PCM), ("opus", AudioCodec.OPUS), ("flac", AudioCodec.FLAC)])
def test_override_requests_that_codec(monkeypatch, role, value, expected):
    module = _server_module(monkeypatch, value)
    _engine(module, FakeClient(role), monkeypatch)._apply_codec_override("spk")

    assert role.calls == [(None, expected)]
    # audio_format=None is deliberate: it makes the library pick that codec's first format in the
    # CLIENT's priority order, rather than us inventing a sample rate the device never offered.


def test_case_is_not_significant(monkeypatch, role):
    module = _server_module(monkeypatch, "PCM")
    _engine(module, FakeClient(role), monkeypatch)._apply_codec_override("spk")
    assert role.calls == [(None, AudioCodec.PCM)]


def test_a_codec_the_client_never_offered_leaves_it_alone(monkeypatch):
    """The library returns False; we must not pretend it worked, and must not leave it unplayable."""
    role = FakeRole(supported={AudioCodec.FLAC})  # e.g. a FLAC-only speaker
    module = _server_module(monkeypatch, "opus")
    _engine(module, FakeClient(role), monkeypatch)._apply_codec_override("spk")

    assert role.calls == [(None, AudioCodec.OPUS)]  # asked once...
    # ...and the caller carries on; the device keeps its advertised preference.


def test_a_nonsense_value_degrades_to_spec_behaviour(monkeypatch, role):
    """A typo in an env var must not take a source down."""
    module = _server_module(monkeypatch, "mp3")
    _engine(module, FakeClient(role))._apply_codec_override("spk")
    assert role.calls == []


def test_a_disconnected_client_is_skipped(monkeypatch, role):
    module = _server_module(monkeypatch, "pcm")
    _engine(module, FakeClient(role, connected=False))._apply_codec_override("spk")
    assert role.calls == []


def test_an_absent_client_is_skipped(monkeypatch):
    module = _server_module(monkeypatch, "pcm")
    engine = module.PlumSendspinServer.__new__(module.PlumSendspinServer)
    engine.server = type("S", (), {"get_client": staticmethod(lambda _id: None)})()
    engine._apply_codec_override("spk")  # must not raise
