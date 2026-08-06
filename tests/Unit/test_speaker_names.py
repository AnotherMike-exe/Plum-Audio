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
