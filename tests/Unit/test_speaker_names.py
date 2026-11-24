"""Unit tests for the server-side speaker-name memo.

A speaker names itself twice: over the protocol while ATTACHED (the handshake name, "Home Assistant
Voice PE - 01") and over mDNS while IDLE (the bare instance name, because a third-party device
usually publishes no `name` TXT key). The listener URL is the only identifier both views share.

The GUI memoised this in localStorage, which made it per-browser and per-origin — so a tab that had
never watched that speaker attach had nothing to fall back to and showed the technical name. Observed
on the rig 2026-08-05 and initially mistaken for a regression; the memo was working, it simply had
never seen the name. The name belongs to the speaker, not to whoever is looking, so it lives here now
and is served from the neighbourhood endpoint.

Run: `pytest tests/Unit/test_speaker_names.py`.
"""

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "scripts"))

from speaker_names import MAX_ENTRIES, SpeakerNames  # noqa: E402

URL = "ws://198.51.100.31:8928/sendspin"
FRIENDLY = "Home Assistant Voice PE - 01"


@pytest.fixture()
def names(tmp_path):
    return SpeakerNames(str(tmp_path / "speaker_names.json"))


def test_a_learned_name_comes_back(names):
    assert names.learn(URL, FRIENDLY) is True
    assert names.get(URL) == FRIENDLY


def test_an_unknown_url_is_none(names):
    assert names.get("ws://198.51.100.40:8928/sendspin") is None
    assert names.get(None) is None


def test_learning_the_same_name_twice_is_a_no_op(names):
    names.learn(URL, FRIENDLY)
    assert names.learn(URL, FRIENDLY) is False, "snapshot() calls this constantly; it must not churn the disk"


def test_a_rename_wins(names):
    names.learn(URL, FRIENDLY)
    assert names.learn(URL, "Kitchen") is True
    assert names.get(URL) == "Kitchen"


def test_nothing_is_learned_from_a_missing_half(names):
    assert names.learn(URL, None) is False
    assert names.learn(None, FRIENDLY) is False
    assert names.learn(URL, "") is False
    assert names.all() == {}


def test_it_survives_a_restart(tmp_path):
    """The whole point: the next process, and every browser, gets the name."""
    path = str(tmp_path / "speaker_names.json")
    SpeakerNames(path).learn(URL, FRIENDLY)
    assert SpeakerNames(path).get(URL) == FRIENDLY


def test_the_file_is_json_a_human_can_read(tmp_path):
    path = tmp_path / "speaker_names.json"
    SpeakerNames(str(path)).learn(URL, FRIENDLY)
    assert json.loads(path.read_text()) == {URL: FRIENDLY}


def test_a_damaged_file_starts_empty_rather_than_failing_to_boot(tmp_path):
    path = tmp_path / "speaker_names.json"
    path.write_text("{ this is not json")
    n = SpeakerNames(str(path))
    assert n.all() == {}
    assert n.learn(URL, FRIENDLY) is True  # and it recovers on the next write


def test_a_non_object_file_is_ignored(tmp_path):
    path = tmp_path / "speaker_names.json"
    path.write_text('["not", "an", "object"]')
    assert SpeakerNames(str(path)).all() == {}


def test_an_unwritable_path_never_raises(tmp_path):
    """Persistence is a convenience — a speaker whose name we cannot store still works."""
    blocker = tmp_path / "blocked"
    blocker.write_text("i am a file, not a directory")
    n = SpeakerNames(str(blocker / "speaker_names.json"))
    n.learn(URL, FRIENDLY)
    assert n.get(URL) == FRIENDLY, "still correct in memory for this process"


def test_the_table_is_bounded(names):
    for i in range(MAX_ENTRIES + 25):
        names.learn(f"ws://10.0.0.{i}:8928/sendspin", f"Speaker {i}")
    assert len(names.all()) <= MAX_ENTRIES


def test_a_known_speaker_can_still_be_renamed_when_full(names):
    for i in range(MAX_ENTRIES):
        names.learn(f"ws://10.0.0.{i}:8928/sendspin", f"Speaker {i}")
    first = "ws://10.0.0.0:8928/sendspin"
    assert names.learn(first, "Renamed") is True, "a full table must not freeze existing entries"
    assert names.get(first) == "Renamed"


def test_no_temp_files_are_left_behind(tmp_path):
    path = tmp_path / "speaker_names.json"
    n = SpeakerNames(str(path))
    for i in range(5):
        n.learn(f"ws://10.0.0.{i}:8928/sendspin", f"S{i}")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["speaker_names.json"]


# -- two instances on one file -----------------------------------------------------------------
#
# The audio server LEARNS names and the mesh API SERVES them, each through its own instance. The
# file was read once, in __init__, so a name learned after startup was on disk and never served
# until a restart.


def test_a_second_instance_serves_a_name_learned_after_it_was_built(tmp_path):
    path = str(tmp_path / "speaker_names.json")
    writer, reader = SpeakerNames(path), SpeakerNames(path)
    assert reader.all() == {}

    writer.learn(URL, FRIENDLY)

    assert reader.get(URL) == FRIENDLY
    assert reader.all() == {URL: FRIENDLY}


def test_a_second_instance_follows_a_rename_and_a_second_speaker(tmp_path):
    path = str(tmp_path / "speaker_names.json")
    writer, reader = SpeakerNames(path), SpeakerNames(path)
    writer.learn(URL, FRIENDLY)
    assert reader.get(URL) == FRIENDLY
    writer.learn(URL, "Kitchen Speaker")
    writer.learn("ws://198.51.100.32:8928/sendspin", "Office")
    assert reader.get(URL) == "Kitchen Speaker"
    assert reader.get("ws://198.51.100.32:8928/sendspin") == "Office"


def test_the_file_is_not_read_again_while_it_is_unchanged(tmp_path, monkeypatch):
    """The neighbourhood is polled by every open GUI. An unchanged file costs one stat, not a parse."""
    path = str(tmp_path / "speaker_names.json")
    SpeakerNames(path).learn(URL, FRIENDLY)
    reader = SpeakerNames(path)
    loads = []
    real_load = reader._load
    monkeypatch.setattr(reader, "_load", lambda: loads.append(1) or real_load())
    for _ in range(50):
        assert reader.get(URL) == FRIENDLY
        reader.all()
    assert loads == []


def test_a_writer_does_not_drop_what_another_instance_learned(tmp_path):
    path = str(tmp_path / "speaker_names.json")
    a, b = SpeakerNames(path), SpeakerNames(path)
    a.learn(URL, FRIENDLY)
    b.learn("ws://198.51.100.32:8928/sendspin", "Office")
    assert SpeakerNames(path).all() == {URL: FRIENDLY, "ws://198.51.100.32:8928/sendspin": "Office"}


def test_a_file_that_goes_away_or_goes_bad_never_raises(tmp_path):
    path = tmp_path / "speaker_names.json"
    writer, reader = SpeakerNames(str(path)), SpeakerNames(str(path))
    writer.learn(URL, FRIENDLY)
    assert reader.get(URL) == FRIENDLY
    path.write_text("{ torn")
    assert reader.all() == {}
    path.unlink()
    assert reader.get(URL) is None


# -- the URL match -----------------------------------------------------------------------------
#
# mDNS advertises one spelling of a listener URL and the handshake registers another. The path and
# the scheme vary; the host and the port are the comparable part, which is the rule
# `neighbourhood._hostport` already applies to this unit's own player.


def test_a_name_is_found_by_host_and_port_when_the_url_text_differs(names):
    names.learn("ws://198.51.100.31:8928/sendspin", FRIENDLY)
    for same in (
        "ws://198.51.100.31:8928",
        "ws://198.51.100.31:8928/",
        "ws://198.51.100.31:8928/sendspin/",
        "wss://198.51.100.31:8928/sendspin",
    ):
        assert names.get(same) == FRIENDLY, same


def test_a_different_host_or_port_does_not_match(names):
    names.learn("ws://198.51.100.31:8928/sendspin", FRIENDLY)
    assert names.get("ws://198.51.100.31:8929/sendspin") is None
    assert names.get("ws://198.51.100.32:8928/sendspin") is None
    assert names.get("not a url") is None
    assert names.get("") is None


def test_the_exact_url_wins_over_a_host_and_port_match(names):
    names.learn("ws://198.51.100.31:8928/a", "A")
    names.learn("ws://198.51.100.31:8928/b", "B")
    assert names.get("ws://198.51.100.31:8928/a") == "A"
    assert names.get("ws://198.51.100.31:8928/b") == "B"
