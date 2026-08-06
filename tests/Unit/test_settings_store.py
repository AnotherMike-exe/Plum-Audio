"""Unit tests for the settings store's write-path integrity.

Every test here guards a failure that is silent by construction — the file ends up wrong, the API
returns 200, and the GUI's version poller sees a bumped version and stops looking.

`test_update_on_a_damaged_file_refuses_rather_than_resetting` is the important one. get_settings()
answers an unreadable file with DEFAULT_SETTINGS so the GUI still renders. update_settings() used to
build on that same reply, so ONE torn read inside a write replaced the whole unit configuration —
every AirPlay/Spotify/Bluetooth endpoint, the device name, the chosen output — with defaults plus
whatever the caller happened to be patching, and persisted it with a bumped version.

`test_migration_does_not_rewrite_on_every_read` guards a loop, not a corruption: the placeholder set
contains "", and `(None or "").strip()` is "", so the shipped default (device=None) matched on every
call and get_settings() wrote the file on every read — with the GUI polling it every 10s per tab.

`test_concurrent_updates_do_not_lose_each_other` is why the lock exists; it fails reliably without
one, because Flask serves this API with threaded=True.

Run: `pytest tests/Unit/test_settings_store.py`.
"""

import importlib
import json
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "scripts"))
sys.path.insert(0, str(REPO / "backend" / "scripts" / "apis"))

import settings_api  # noqa: E402  — the module itself, for DEFAULT_SETTINGS and reload
from settings_api import SettingsManager, sanitize_device_name  # noqa: E402


@pytest.fixture()
def manager(tmp_path):
    return SettingsManager(str(tmp_path / "settings.json"))


# -- the write path must not invent data ------------------------------------------------------


def test_update_on_a_damaged_file_refuses_rather_than_resetting(manager):
    manager.update_settings({"deviceName": "Kitchen"})
    manager.update_settings({"integrations": {"airplay": {"endpoints": [{"id": "7", "enabled": True}]}}})

    Path(manager.settings_file).write_text("{ this is not json")

    with pytest.raises(Exception):
        manager.update_settings({"deviceName": "Hallway"})

    # The damaged file is left exactly as found — not silently replaced with defaults+patch.
    assert Path(manager.settings_file).read_text() == "{ this is not json"


def test_read_on_a_damaged_file_still_answers_defaults(manager):
    """The read path keeps its never-raise contract, so the GUI renders something."""
    Path(manager.settings_file).write_text("{ this is not json")
    settings = manager.get_settings()
    # Against the module's own default, not a literal: that default now derives from PLUM_UNIT_NAME
    # (see below), so a literal here fails for the wrong reason whenever the var is set in the
    # environment the suite runs in.
    assert settings["deviceName"] == settings_api.DEFAULT_SETTINGS["deviceName"]


def _endpoint_names(mod):
    """Every source endpoint's default name, from the defaults as the module currently holds them."""
    return {
        section: [e["deviceName"] for e in body["endpoints"]]
        for section, body in mod.DEFAULT_SETTINGS["integrations"].items()
        if isinstance(body, dict) and body.get("endpoints")
    }


def test_an_unnamed_unit_and_its_endpoints_boot_under_PLUM_UNIT_NAME(monkeypatch):
    """The env tier of unit_identity's documented precedence has to actually be reachable.

    DEFAULT_SETTINGS is WRITTEN to settings.json on the first read, so a hardcoded literal outranks
    PLUM_UNIT_NAME permanently and on every unit at once. That produced two collisions on the freshly
    imaged .201 units (2026-08-06): both came up as the unit "Plum Sendspin", one name for two units
    in the mesh view / GUI cards / mDNS; and both offered an AirPlay receiver called "Plum Audio",
    indistinguishable to a sender on the LAN. Every source endpoint shares that fallback, so Spotify
    and Bluetooth collided identically once enabled.

    Reloaded rather than monkeypatched in place: the defaults are evaluated at import.
    """
    monkeypatch.setenv("PLUM_UNIT_NAME", "Pi4-02")
    reloaded = importlib.reload(settings_api)
    try:
        assert reloaded.DEFAULT_SETTINGS["deviceName"] == "Pi4-02"
        # airplay, bluetooth and spotify — not just the one that surfaced the bug.
        names = _endpoint_names(reloaded)
        assert set(names) == {"airplay", "bluetooth", "spotify"}, names
        assert all(n == "Pi4-02" for ns in names.values() for n in ns), names

        monkeypatch.delenv("PLUM_UNIT_NAME")
        bare = importlib.reload(settings_api)
        assert bare.DEFAULT_SETTINGS["deviceName"] == "Plum Sendspin"
        assert all(n == "Plum Audio" for ns in _endpoint_names(bare).values() for n in ns)
    finally:
        # Leave the module as the rest of the suite expects to find it.
        monkeypatch.delenv("PLUM_UNIT_NAME", raising=False)
        importlib.reload(settings_api)


def test_a_hostile_PLUM_UNIT_NAME_is_scrubbed_before_it_reaches_the_defaults(monkeypatch):
    """`_sanitize_device_names` runs on the WRITE path only, so a default reaches disk unscrubbed.

    A device name is interpolated into shairport's libconfig and go-librespot's YAML and a daemon is
    respooled to read it, so the env value has to be reduced to the safe charset at import — not
    trusted because units.conf happens to be operator-controlled.
    """
    monkeypatch.setenv("PLUM_UNIT_NAME", 'Kitchen"; system("rm -rf /")')
    reloaded = importlib.reload(settings_api)
    try:
        for name in [reloaded.DEFAULT_SETTINGS["deviceName"], *(
            n for ns in _endpoint_names(reloaded).values() for n in ns
        )]:
            assert reloaded.DEVICE_NAME_ALLOWED.match(name), name
            assert '"' not in name and ";" not in name, name

        # A name with nothing usable in it falls back rather than becoming empty.
        monkeypatch.setenv("PLUM_UNIT_NAME", "!!!@@@###")
        fallback = importlib.reload(settings_api)
        assert fallback.DEFAULT_SETTINGS["deviceName"] == "Plum Sendspin"
        assert all(n == "Plum Audio" for ns in _endpoint_names(fallback).values() for n in ns)
    finally:
        monkeypatch.delenv("PLUM_UNIT_NAME", raising=False)
        importlib.reload(settings_api)


def test_a_missing_file_is_not_damage(manager):
    """No file at all legitimately means defaults — that is how a fresh unit boots."""
    Path(manager.settings_file).unlink()
    assert manager.update_settings({"deviceName": "Fresh"})["deviceName"] == "Fresh"


# -- the migration must converge --------------------------------------------------------------


def test_migration_does_not_rewrite_on_every_read(manager):
    """A unit that has never chosen an output must not rewrite settings.json on every read."""
    manager.get_settings()
    before = Path(manager.settings_file).stat().st_mtime_ns

    for _ in range(5):
        manager.get_settings()

    assert Path(manager.settings_file).stat().st_mtime_ns == before


def test_legacy_placeholder_is_still_cleared_once(manager):
    """The migration must keep firing for the case it was written for."""
    settings = manager.get_settings()
    settings["audio"]["output"]["device"] = "hw:Headphones"
    Path(manager.settings_file).write_text(json.dumps(settings))

    assert manager.get_settings()["audio"]["output"]["device"] is None

    # ...and then stop.
    after = Path(manager.settings_file).stat().st_mtime_ns
    manager.get_settings()
    assert Path(manager.settings_file).stat().st_mtime_ns == after


# -- concurrency -------------------------------------------------------------------------------


def test_concurrent_updates_do_not_lose_each_other(manager):
    """Flask is threaded=True; two endpoint edits must not both read N and both write N+1.

    The version counter is the probe: update_settings reads it and writes it back incremented, so a
    lost update is exactly a version that did not advance. Asserting on the endpoint list instead
    would not detect it — the one-level merge means the last writer legitimately wins on content.
    """
    start = manager.get_settings()["version"]
    threads_count, per_thread = 4, 20

    def bump(name):
        for _ in range(per_thread):
            manager.update_settings({"deviceName": name})

    threads = [threading.Thread(target=bump, args=(f"unit-{i}",)) for i in range(threads_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert manager.get_settings()["version"] == start + threads_count * per_thread


def test_concurrent_writes_never_leave_a_torn_file(manager):
    """A shared temp path let one writer truncate another's buffer mid-write."""
    errors = []

    def churn(name):
        for _ in range(25):
            try:
                manager.update_settings({"deviceName": name})
                json.loads(Path(manager.settings_file).read_text())
            except Exception as exc:  # noqa: BLE001 - the assertion is that nothing raises
                errors.append(exc)

    threads = [threading.Thread(target=churn, args=(f"unit-{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors


# -- device names reach a daemon config template, not just a label -----------------------------


@pytest.mark.parametrize(
    "raw",
    [
        'X"; }; sessioncontrol = { run_this_before_play_begins = "/bin/sh -c id";',
        "Kitchen\nname = \"pwned\";",
        "back\\slash",
        "semi;colon",
        "{braces}",
    ],
)
def test_injection_payloads_are_stripped_on_persist(manager, raw):
    manager.update_settings({"integrations": {"airplay": {"endpoints": [{"id": "1", "deviceName": raw}]}}})
    stored = manager.get_settings()["integrations"]["airplay"]["endpoints"][0]["deviceName"]
    for forbidden in ('"', "\\", "\n", ";", "{", "}"):
        assert forbidden not in stored


def test_ordinary_names_survive_intact():
    """Sanitizing must not mangle names people actually use."""
    for name in ["Mike's Room", "Kitchen (Main)", "Bed & Breakfast", "Study-2", "Loft 3.1"]:
        assert sanitize_device_name(name) == name


def test_a_name_with_nothing_usable_falls_back(manager):
    manager.update_settings({"integrations": {"airplay": {"endpoints": [{"id": "1", "deviceName": '"""'}]}}})
    stored = manager.get_settings()["integrations"]["airplay"]["endpoints"][0]["deviceName"]
    assert stored == "Plum Audio"


# -- audio.output validation: the POST /api/settings bypass ---------------------------------------
#
# POST /api/settings takes arbitrary JSON and merges one level deep, so it REPLACES audio.output
# wholesale without ever passing audio_api's find_device/404/409 checks. Whatever lands here is what
# the player hands to PortAudio — and now also what the output gate reads to decide whether this
# unit runs a player at all.


def test_the_no_output_sentinel_persists(manager):
    manager.update_settings({"audio": {"output": {"device": "none"}}})
    output = manager.get_settings()["audio"]["output"]
    assert output["device"] == "none"
    assert output["device_type"] == "NONE"


def test_the_sentinel_survives_a_subsequent_read(manager):
    """The regression that would silently give a headless unit its player back.

    "none" must never join LEGACY_OUTPUT_PLACEHOLDERS: _migrate_audio_output runs on every read and
    would clear it, so the gate would see an unset output at the next container start and install
    the player program again — on a box with no audio hardware.
    """
    manager.update_settings({"audio": {"output": {"device": "none"}}})
    for _ in range(3):
        assert manager.get_settings()["audio"]["output"]["device"] == "none"


@pytest.mark.parametrize("raw", ["None", "NONE", " none "])
def test_sentinel_spellings_normalise(manager, raw):
    manager.update_settings({"audio": {"output": {"device": raw}}})
    assert manager.get_settings()["audio"]["output"]["device"] == "none"


def test_a_real_device_spec_is_stored_stripped(manager):
    manager.update_settings({"audio": {"output": {"device": "  sndrpihifiberry:0 "}}})
    assert manager.get_settings()["audio"]["output"]["device"] == "sndrpihifiberry:0"


@pytest.mark.parametrize(
    "spec",
    [
        "snd_rpi_hifiberry_dacplus",  # a PortAudio name fragment, resolves nowhere on another unit
        "bcm2835",
        "vc4hdmi0:0",
    ],
)
def test_specs_that_resolve_nowhere_here_are_still_accepted(manager, spec):
    """SHAPE only, never existence — the tier-2 test writes an unopenable device on purpose."""
    manager.update_settings({"audio": {"output": {"device": spec}}})
    assert manager.get_settings()["audio"]["output"]["device"] == spec


def test_the_legacy_placeholder_still_means_unset(manager):
    manager.update_settings({"audio": {"output": {"device": "hw:Headphones"}}})
    assert manager.get_settings()["audio"]["output"]["device"] is None


@pytest.mark.parametrize(
    "bad",
    [
        123,
        {"device": "nested"},
        ["sndrpihifiberry:0"],
        "x" * 65,
        'evil"; }; sessioncontrol = { run_this_before_play_begins = "/bin/sh -c id";',
        "card\nname",
        "card;name",
    ],
)
def test_a_bogus_device_spec_is_refused(manager, bad):
    with pytest.raises(ValueError):
        manager.update_settings({"audio": {"output": {"device": bad}}})


def test_a_refused_update_does_not_persist(manager):
    manager.update_settings({"audio": {"output": {"device": "sndrpihifiberry:0"}}})
    before = manager.get_settings()["version"]
    with pytest.raises(ValueError):
        manager.update_settings({"audio": {"output": {"device": "bad;spec"}}})
    after = manager.get_settings()
    assert after["audio"]["output"]["device"] == "sndrpihifiberry:0"
    assert after["version"] == before  # nothing written, so the GUI's poller has nothing to miss


def test_an_empty_output_dict_does_not_resurrect_the_previous_device(manager):
    """Pins the one-level-merge semantics this validator now sits on top of."""
    manager.update_settings({"audio": {"output": {"device": "sndrpihifiberry:0"}}})
    manager.update_settings({"audio": {"output": {}}})
    assert manager.get_settings()["audio"]["output"].get("device") is None


def test_a_non_dict_output_is_replaced_rather_than_trusted(manager):
    manager.update_settings({"audio": {"output": "sndrpihifiberry:0"}})
    assert manager.get_settings()["audio"]["output"] == {"device": None, "device_type": None}


def test_a_stored_alsa_address_is_refused(manager):
    """`hw:C,D` is a POSITION, not an identity, and card numbers move.

    Left stored, find_device's address pass resolves it against TODAY's numbering: measured,
    `hw:2,0` gave sndrpihifiberry:0 before a reboot and vc4hdmi1:0 after. The next write through
    set_output_device would then canonicalise that wrong card's stable id into settings.json
    permanently, where it is indistinguishable from a deliberate choice.
    """
    with pytest.raises(ValueError, match="stable"):
        manager.update_settings({"audio": {"output": {"device": "hw:2,0"}}})


def test_an_alsa_address_already_on_disk_is_cleared_on_read(manager):
    """A hand-edited or pre-existing file must not keep the hazard — falling back to
    PLUM_DAC_DEVICE is the documented "never chosen" behaviour and is honest."""
    import json as _json
    path = manager.settings_file
    with open(path, "w", encoding="utf-8") as f:
        _json.dump({"version": 3, "audio": {"output": {"device": "hw:2,0", "device_type": "HAT"}}}, f)

    output = manager.get_settings()["audio"]["output"]
    assert output["device"] is None
    assert output["device_type"] is None


def test_the_legacy_headphones_placeholder_still_takes_its_own_path(manager):
    """It is also `hw:`-prefixed; the two clauses must not fight over it."""
    manager.update_settings({"audio": {"output": {"device": "hw:Headphones"}}})
    assert manager.get_settings()["audio"]["output"]["device"] is None
