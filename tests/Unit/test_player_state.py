"""Unit tests for the durability of player_state.json.

The file holds the level a room was left at. A unit that loses it comes back at the default level,
which is 100% — so a damaged file is a loud speaker, not a cosmetic fault. These tests pin three
things: the write is atomic and unique per writer, a damaged file is reported and kept for
inspection instead of being overwritten in silence, and two threads cannot drop each other's keys.

Run: `pytest tests/Unit/test_player_state.py`.
"""

import json
import logging
import os
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "scripts"))

import player_state  # noqa: E402


def _state(tmp_path) -> str:
    return str(tmp_path / "player_state.json")


def test_a_write_is_readable_and_leaves_no_temp_file(tmp_path):
    path = _state(tmp_path)
    player_state.save_render_state(path, 37, True)
    assert player_state.load_render_state(path, default_volume=100) == (37, True)
    assert sorted(os.listdir(tmp_path)) == ["player_state.json"]


def test_a_write_keeps_the_keys_it_did_not_touch(tmp_path):
    path = _state(tmp_path)
    player_state.save_render_state(path, 37, False)
    player_state.save_active_output(path, "sndrpihifiberry:0")
    player_state.save_player_health(path, "error")
    data = json.loads(Path(path).read_text())
    assert data["volume"] == 37
    assert data["output_device"] == "sndrpihifiberry:0"
    assert data["player_health"] == "error"


def test_the_data_is_synced_to_disk_before_the_rename(tmp_path, monkeypatch):
    """A rename without fsync is what leaves a zero-length file after a power loss."""
    events: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (events.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(os, "replace", lambda a, b: (events.append("replace"), real_replace(a, b))[1])
    player_state.save_render_state(_state(tmp_path), 50, False)
    assert "fsync" in events and "replace" in events
    assert events.index("fsync") < events.index("replace")


def test_the_temp_name_is_unique_per_write(tmp_path, monkeypatch):
    """A fixed `<file>.tmp` lets two writers truncate each other. Each write needs its own name."""
    sources: list[str] = []
    real_replace = os.replace
    monkeypatch.setattr(os, "replace", lambda a, b: (sources.append(str(a)), real_replace(a, b))[1])
    path = _state(tmp_path)
    player_state.save_render_state(path, 1, False)
    player_state.save_render_state(path, 2, False)
    assert len(set(sources)) == 2
    assert all(os.path.dirname(s) == str(tmp_path) for s in sources), "same directory keeps the rename atomic"
    assert f"{path}.tmp" not in sources


def test_a_failed_write_leaves_the_old_file_and_no_temp_file(tmp_path, monkeypatch):
    path = _state(tmp_path)
    player_state.save_render_state(path, 37, False)

    def boom(a, b):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    player_state.save_render_state(path, 99, False)  # must not raise
    monkeypatch.undo()
    assert player_state.load_render_state(path, default_volume=100) == (37, False)
    assert sorted(os.listdir(tmp_path)) == ["player_state.json"]


def test_a_zero_length_file_is_reported_at_load(tmp_path, caplog):
    path = _state(tmp_path)
    Path(path).write_text("")
    with caplog.at_level(logging.WARNING, logger="plum.player_state"):
        assert player_state.load_render_state(path, default_volume=80) == (80, False)
    assert any("zero-length" in r.getMessage() for r in caplog.records)
    assert any(r.levelno >= logging.ERROR for r in caplog.records), "a level reset is an error, not a note"


def test_a_corrupt_file_is_reported_at_load(tmp_path, caplog):
    path = _state(tmp_path)
    Path(path).write_text('{"volume": 4')
    with caplog.at_level(logging.WARNING, logger="plum.player_state"):
        assert player_state.load_render_state(path, default_volume=80) == (80, False)
    assert any("not valid JSON" in r.getMessage() for r in caplog.records)


def test_a_missing_file_is_normal_and_quiet(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="plum.player_state"):
        assert player_state.load_render_state(_state(tmp_path), default_volume=80) == (80, False)
    assert not caplog.records


def test_a_write_over_a_damaged_file_keeps_a_copy_and_says_so(tmp_path, caplog):
    """The silent path: a health write lands on a damaged file and replaces it with one key.

    The level is then gone with nothing in any log, and the unit comes back at the default.
    """
    path = _state(tmp_path)
    Path(path).write_text('{"volume": 4')
    with caplog.at_level(logging.WARNING, logger="plum.player_state"):
        player_state.save_player_health(path, "error")
    assert json.loads(Path(path).read_text()) == {"player_health": "error"}
    assert Path(f"{path}.corrupt").read_text() == '{"volume": 4'
    assert any(r.levelno >= logging.ERROR and ".corrupt" in r.getMessage() for r in caplog.records)


def test_concurrent_writers_do_not_drop_each_other(tmp_path):
    """The heartbeat runs in a worker thread and the level is saved from the loop thread."""
    path = _state(tmp_path)

    def writer(key: str) -> None:
        for i in range(40):
            player_state._merge_write(path, {key: i})

    threads = [threading.Thread(target=writer, args=(f"k{n}",)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert json.loads(Path(path).read_text()) == {"k0": 39, "k1": 39, "k2": 39, "k3": 39}


def test_the_health_report_round_trips(tmp_path):
    path = _state(tmp_path)
    player_state.save_render_state(path, 37, False)
    player_state.save_health_report(path, {"ts": 123.0, "output": "ok"})
    assert player_state.load_health_report(path) == {"ts": 123.0, "output": "ok"}
    assert player_state.load_render_state(path, default_volume=100) == (37, False)
    assert player_state.load_health_report(str(tmp_path / "absent.json")) is None
