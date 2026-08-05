"""Unit tests for the client/state wire format.

The Sendspin spec makes `state` a REQUIRED field of the `client/state` payload, at the top level,
one of synchronized/error/external_source.

aiosendspin 6.0.5 does not send it. `send_player_state()` populates only `player.state` — the field
the library's own models annotate "DEPRECATED(before-spec-pr-50): Remove once all clients send state
at client level" — and leaves `ClientStatePayload.state` at its None default, which `omit_none = True`
then strips from the JSON entirely. The library's own SERVER reads `payload.state` at the top level
(server/connection.py), so it reads None and never transitions the client. Plum-to-Plum that is
benign only because both ends default to SYNCHRONIZED; a spec-strict third-party server sees a
required field missing on every state message, including the mandatory one at connect.

`test_the_library_still_omits_it` is deliberately asserting a BUG. When it starts failing, upstream
has fixed this and build_client_state_message can be deleted in favour of send_player_state().

Run: `pytest tests/Unit/test_client_state_conformance.py`.
"""

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "scripts"))

pytest.importorskip("aiosendspin", reason="aiosendspin is a real runtime dep; skipped on a bare checkout")
pytest.importorskip("numpy", reason="sendspin_player imports numpy")

from aiosendspin.models.core import ClientStatePayload  # noqa: E402
from aiosendspin.models.types import ClientStateType  # noqa: E402

from sendspin_player import build_client_state_message  # noqa: E402

VALID_STATES = {"synchronized", "error", "external_source"}


def emit(state=ClientStateType.SYNCHRONIZED, **over):
    kwargs = dict(volume=42, muted=False, static_delay_ms=0, required_lead_time_ms=250, min_buffer_ms=250)
    kwargs.update(over)
    return json.loads(build_client_state_message(state, **kwargs).to_json())


def test_state_is_present_at_the_top_level():
    payload = emit()["payload"]
    assert "state" in payload, "the spec makes payload.state REQUIRED"
    assert payload["state"] in VALID_STATES


@pytest.mark.parametrize("state", list(ClientStateType))
def test_every_state_serialises_to_its_spec_string(state):
    assert emit(state)["payload"]["state"] == state.value
    assert state.value in VALID_STATES


def test_the_message_type_is_client_state():
    assert emit()["type"] == "client/state"


def test_the_deprecated_nested_state_is_still_sent_and_agrees():
    """Kept for peers mid-migration; it must never disagree with the top-level value."""
    payload = emit(ClientStateType.ERROR)["payload"]
    assert payload["player"]["state"] == payload["state"] == "error"


def test_the_player_payload_still_carries_volume_and_mute():
    """The volume echo is load-bearing — the server's view of a player's level moves only on this."""
    payload = emit(volume=17, muted=True)["payload"]["player"]
    assert payload["volume"] == 17
    assert payload["muted"] is True


def test_supported_commands_is_omitted_when_absent_rather_than_null():
    assert "supported_commands" not in emit()["payload"]["player"]


def test_the_library_still_omits_it():
    """A canary on the pin, asserting the UPSTREAM BUG this workaround exists for.

    When this fails, aiosendspin has started sending client-level state: drop
    build_client_state_message and call send_player_state() again.
    """
    from aiosendspin.models.player import PlayerStatePayload

    library_shape = ClientStatePayload(
        player=PlayerStatePayload(state=ClientStateType.SYNCHRONIZED, volume=42, muted=False)
    )
    assert "state" not in json.loads(library_shape.to_json()), (
        "aiosendspin now emits client-level state — remove the local workaround"
    )
