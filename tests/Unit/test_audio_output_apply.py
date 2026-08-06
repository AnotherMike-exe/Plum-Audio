"""Unit tests for applying an output change: the renderer swap and the echo behind it.

Three failures are guarded here, all of them the silent kind this project keeps meeting.

`test_a_failed_switch_restores_the_previous_device` — a switch that cannot open its target must fall
back to what was playing, not leave the room quiet. Silence with a correct-looking settings.json is
the exact shape of the missing-`client/state` bug: everything reads fine and nothing is audible.

`test_saving_the_output_keeps_the_volume` — player_state.json now has two writers on different
schedules. A whole-file dump from either drops the other, and a speaker that forgets its level on a
device switch looks like a volume bug rather than an output one.

`test_pending_*` — the config API must report the player's ECHO, not the user's choice. Reporting
the choice would render every switch as instantly successful, including the ones that never opened.

AlsaRenderer is exercised with a fake sounddevice, so no PortAudio and no hardware.

Run: `pytest tests/Unit/test_audio_output_apply.py`.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "scripts"))

import audio_devices  # noqa: E402
import player_state  # noqa: E402


# -- the state file (two writers, different schedules) ---------------------------------------------


def test_saving_the_output_keeps_the_volume(tmp_path):
    path = str(tmp_path / "player_state.json")
    player_state.save_render_state(path, 42, True)
    player_state.save_active_output(path, "sndrpihifiberry:0")

    assert player_state.load_render_state(path, default_volume=100) == (42, True)
    assert player_state.load_active_output(path) == "sndrpihifiberry:0"


def test_saving_the_volume_keeps_the_output(tmp_path):
    path = str(tmp_path / "player_state.json")
    player_state.save_active_output(path, "sndrpihifiberry:0")
    player_state.save_render_state(path, 17, False)

    assert player_state.load_active_output(path) == "sndrpihifiberry:0"
    assert player_state.load_render_state(path, default_volume=100) == (17, False)


def test_no_output_recorded_reads_as_none(tmp_path):
    path = str(tmp_path / "player_state.json")
    assert player_state.load_active_output(path) is None
    player_state.save_render_state(path, 50, False)
    assert player_state.load_active_output(path) is None


def test_a_malformed_state_file_does_not_raise(tmp_path):
    path = tmp_path / "player_state.json"
    path.write_text("{ not json")
    assert player_state.load_active_output(str(path)) is None
    assert player_state.load_render_state(str(path), default_volume=100) == (100, False)


# -- the renderer swap -----------------------------------------------------------------------------


class FakeStream:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        pass


class FakeSoundDevice:
    """Stands in for the sounddevice module; refuses whatever is in `unopenable`."""

    def __init__(self, unopenable=()):
        self.unopenable = set(unopenable)
        self.opened = []

    def RawOutputStream(self, **kwargs):  # noqa: N802 - mirrors the real API's name
        device = kwargs.get("device")
        if device in self.unopenable:
            raise OSError(f"cannot open {device}")
        self.opened.append(device)
        return FakeStream(**kwargs)


@pytest.fixture
def renderer_cls():
    # numpy + aiosendspin are real runtime deps imported at sendspin_player's module scope; skipped
    # on a bare checkout, the same as the other tests that touch the audio path.
    pytest.importorskip("numpy", reason="sendspin_player imports numpy at module scope")
    pytest.importorskip("aiosendspin", reason="sendspin_player imports aiosendspin at module scope")
    import sendspin_player

    return sendspin_player


def _renderer(module, fake_sd, device, monkeypatch, *, resolves=None):
    monkeypatch.setattr(module, "sd", fake_sd)
    # Resolution is exercised in test_audio_devices; here it just maps spec -> PortAudio index.
    resolves = resolves or {}

    class _Resolved:  # stands in for AudioDevice; only hw_id is read on this path
        hw_id = "hw:2,0"

    monkeypatch.setattr(
        audio_devices,
        "resolve_portaudio_index",
        lambda spec: (resolves[spec], _Resolved()) if spec in resolves else (None, None),
    )
    return module.AlsaRenderer(44100, 2, 16, device=device, target_buffer_ms=300)


def test_start_opens_the_configured_device(renderer_cls, monkeypatch):
    fake = FakeSoundDevice()
    renderer = _renderer(renderer_cls, fake, "bcm2835", monkeypatch)
    renderer.start()

    assert fake.opened == ["bcm2835"]  # unresolved: passed through for PortAudio to name-match
    assert renderer.device == "bcm2835"


def test_a_resolved_spec_is_opened_by_portaudio_index(renderer_cls, monkeypatch):
    """The index, not the string — two identical DACs would make a name match ambiguous."""
    fake = FakeSoundDevice()
    renderer = _renderer(renderer_cls, fake, "sndrpihifiberry:0", monkeypatch, resolves={"sndrpihifiberry:0": 0})
    renderer.start()

    assert fake.opened == [0]


def test_reopen_switches_the_device(renderer_cls, monkeypatch):
    fake = FakeSoundDevice()
    renderer = _renderer(renderer_cls, fake, "bcm2835", monkeypatch)
    renderer.start()

    assert renderer.reopen("Headphones:0") is True
    assert renderer.device == "Headphones:0"
    assert fake.opened == ["bcm2835", "Headphones:0"]


def test_reopen_to_the_same_device_is_a_no_op(renderer_cls, monkeypatch):
    """Guards against the settings poll churning the DAC every tick."""
    fake = FakeSoundDevice()
    renderer = _renderer(renderer_cls, fake, "bcm2835", monkeypatch)
    renderer.start()

    assert renderer.reopen("bcm2835") is True
    assert fake.opened == ["bcm2835"]  # not reopened


def test_a_failed_switch_restores_the_previous_device(renderer_cls, monkeypatch):
    """The headline case: a bad output must be obvious, never silent."""
    fake = FakeSoundDevice(unopenable={"vc4hdmi0:0"})
    renderer = _renderer(renderer_cls, fake, "bcm2835", monkeypatch)
    renderer.start()

    assert renderer.reopen("vc4hdmi0:0") is False
    assert renderer.device == "bcm2835"  # ...and still playing
    assert fake.opened == ["bcm2835", "bcm2835"]


def test_a_switch_that_fails_both_ways_admits_it(renderer_cls, monkeypatch):
    """If even the restore fails we ARE silent; the state must say so rather than claim a device."""
    fake = FakeSoundDevice()
    renderer = _renderer(renderer_cls, fake, "bcm2835", monkeypatch)
    renderer.start()
    fake.unopenable = {"vc4hdmi0:0", "bcm2835"}

    assert renderer.reopen("vc4hdmi0:0") is False
    assert renderer.device is None


def test_buffered_audio_survives_a_switch(renderer_cls, monkeypatch):
    """Changing speakers is not restarting the track."""
    fake = FakeSoundDevice()
    renderer = _renderer(renderer_cls, fake, "bcm2835", monkeypatch)
    renderer.start()
    renderer.enqueue(b"\x00\x01" * 400)
    before = len(renderer._buf)

    renderer.reopen("Headphones:0")
    assert len(renderer._buf) == before


def test_the_software_gain_survives_a_switch(renderer_cls, monkeypatch):
    fake = FakeSoundDevice()
    renderer = _renderer(renderer_cls, fake, "bcm2835", monkeypatch)
    renderer.start()
    renderer.set_volume(volume=25, muted=False)

    renderer.reopen("Headphones:0")
    assert renderer._gain == pytest.approx(0.25)


# -- the watcher -------------------------------------------------------------------------------------


def test_watcher_fires_only_on_a_change(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"audio": {"output": {"device": "bcm2835"}}}))
    monkeypatch.setenv("PLUM_SETTINGS_FILE", str(settings))
    monkeypatch.delenv("PLUM_DAC_DEVICE", raising=False)

    seen = []

    async def on_change(spec):
        seen.append(spec)

    async def drive():
        task = asyncio.ensure_future(audio_devices.watch_output_device(on_change, interval=0.01))
        await asyncio.sleep(0.05)
        unchanged = list(seen)

        settings.write_text(json.dumps({"audio": {"output": {"device": "sndrpihifiberry:0"}}}))
        await asyncio.sleep(0.05)
        task.cancel()
        return unchanged

    unchanged = asyncio.run(drive())
    assert unchanged == []  # a settled file must not churn the DAC every tick
    assert seen == ["sndrpihifiberry:0"]


def test_a_failing_callback_does_not_kill_the_watcher(tmp_path, monkeypatch):
    """A device that will not open must not also stop us noticing the next choice."""
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"audio": {"output": {"device": "a"}}}))
    monkeypatch.setenv("PLUM_SETTINGS_FILE", str(settings))
    monkeypatch.delenv("PLUM_DAC_DEVICE", raising=False)

    seen = []

    async def on_change(spec):
        seen.append(spec)
        raise RuntimeError("could not open")

    async def drive():
        task = asyncio.ensure_future(audio_devices.watch_output_device(on_change, interval=0.01))
        await asyncio.sleep(0)  # let the watcher read its baseline before we move the file
        settings.write_text(json.dumps({"audio": {"output": {"device": "b"}}}))
        await asyncio.sleep(0.05)
        settings.write_text(json.dumps({"audio": {"output": {"device": "c"}}}))
        await asyncio.sleep(0.05)
        task.cancel()

    asyncio.run(drive())
    assert seen == ["b", "c"]


# -- "No output" must never reach the renderer ------------------------------------------------------


def test_switching_to_no_output_keeps_the_current_stream(renderer_cls, monkeypatch):
    """Refused BEFORE the teardown. "No output" applies at the next container start, not now.

    Tearing the stream down here would go silent early, then fail to open anything anyway, and would
    move the echo — which is precisely what tells the API to report restart_required rather than
    claiming the change already took effect.
    """
    fake = FakeSoundDevice()
    renderer = _renderer(renderer_cls, fake, "sndrpihifiberry:0", monkeypatch)
    renderer.start()
    opened_before = list(fake.opened)

    assert renderer.reopen("none") is False
    assert renderer.device == "sndrpihifiberry:0"  # the echo source is unmoved
    assert fake.opened == opened_before  # nothing was torn down and nothing reopened
    assert renderer._stream is not None


def test_opening_the_sentinel_raises_rather_than_reaching_portaudio(renderer_cls, monkeypatch):
    fake = FakeSoundDevice()
    renderer = _renderer(renderer_cls, fake, "none", monkeypatch)

    with pytest.raises(ValueError, match="no output"):
        renderer.start()
    assert fake.opened == []


def test_the_no_output_watcher_leaves_the_echo_alone(renderer_cls, monkeypatch, tmp_path):
    """The API's restart_required is 'choice vs echo'; moving the echo here would erase the signal."""
    state = tmp_path / "player_state.json"
    player_state.save_active_output(str(state), "sndrpihifiberry:0")

    fake = FakeSoundDevice()
    renderer = _renderer(renderer_cls, fake, "sndrpihifiberry:0", monkeypatch)
    renderer.start()

    # The watcher body, as sendspin_player.main defines it: the sentinel returns before reopen().
    async def apply(spec):
        if audio_devices.is_no_output(spec):
            return
        await asyncio.get_running_loop().run_in_executor(None, renderer.reopen, spec)
        player_state.save_active_output(str(state), renderer.device)

    asyncio.run(apply("none"))
    assert player_state.load_active_output(str(state)) == "sndrpihifiberry:0"
    assert renderer.device == "sndrpihifiberry:0"
