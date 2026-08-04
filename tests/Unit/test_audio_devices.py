"""Unit tests for ALSA playback discovery behind the output picker.

The fixtures are VERBATIM `aplay -l` captures from two real units on 2026-08-04, kept because the
difference between them is the whole reason this module exists: the same three-card Pi 4 numbers its
cards differently depending on what is fitted. On `.201.133` the analogue jack is card 0; on the
Amp100 unit `.7.204` the HAT is card 2, sitting behind two vc4hdmi cards. Anything that persists
`hw:C,D` is therefore one HDMI hotplug or kernel bump away from opening the wrong output while every
level in the mesh still reads correct — see test_identity_survives_card_renumbering, which is the
regression this file exists for.

The availability tests guard the second trap, measured on the same day: PortAudio enumerates by
opening, so a card held exclusively vanishes from query_devices(). With our own player holding the
Amp100's single-subdevice pcm512x, the output list came back EMPTY. Availability that trusts a live
probe would grey out the device that is audibly playing.

No hardware and no PortAudio needed — everything here is pure parsing plus monkeypatched /proc.

Run: `pytest tests/Unit/test_audio_devices.py`.
"""

import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "scripts"))

import audio_devices  # noqa: E402
from audio_devices import AudioDevice, DeviceType, find_device, parse_aplay_output  # noqa: E402

# -- real captures -------------------------------------------------------------------------------

APLAY_AMP100 = """**** List of PLAYBACK Hardware Devices ****
card 0: vc4hdmi0 [vc4-hdmi-0], device 0: MAI PCM i2s-hifi-0 [MAI PCM i2s-hifi-0]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
card 1: vc4hdmi1 [vc4-hdmi-1], device 0: MAI PCM i2s-hifi-0 [MAI PCM i2s-hifi-0]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
card 2: sndrpihifiberry [snd_rpi_hifiberry_dacplus], device 0: HiFiBerry DAC+ Pro HiFi pcm512x-hifi-0 \
[HiFiBerry DAC+ Pro HiFi pcm512x-hifi-0]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
"""

APLAY_ONBOARD = """**** List of PLAYBACK Hardware Devices ****
card 0: Headphones [bcm2835 Headphones], device 0: bcm2835 Headphones [bcm2835 Headphones]
  Subdevices: 7/8
  Subdevice #0: subdevice #0
card 1: vc4hdmi0 [vc4-hdmi-0], device 0: MAI PCM i2s-hifi-0 [MAI PCM i2s-hifi-0]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
card 2: vc4hdmi1 [vc4-hdmi-1], device 0: MAI PCM i2s-hifi-0 [MAI PCM i2s-hifi-0]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
"""

# Same HAT, but fitted to a unit with no HDMI attached — the card lands at 0 instead of 2.
APLAY_AMP100_RENUMBERED = """**** List of PLAYBACK Hardware Devices ****
card 0: sndrpihifiberry [snd_rpi_hifiberry_dacplus], device 0: HiFiBerry DAC+ Pro HiFi pcm512x-hifi-0 \
[HiFiBerry DAC+ Pro HiFi pcm512x-hifi-0]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
"""


# -- parsing + classification --------------------------------------------------------------------


def test_parses_every_card_from_the_amp100_capture():
    devices = parse_aplay_output(APLAY_AMP100)
    assert [d.card for d in devices] == [0, 1, 2]
    assert [d.type for d in devices] == [
        DeviceType.BUILTIN_HDMI,
        DeviceType.BUILTIN_HDMI,
        DeviceType.HAT,
    ]


def test_hat_friendly_name_drops_the_codec_tail():
    hat = parse_aplay_output(APLAY_AMP100)[2]
    # 'HiFiBerry DAC+ Pro HiFi pcm512x-hifi-0' is the kernel's PCM name; the codec suffix is noise.
    assert hat.friendly_name == "HiFiBerry DAC+ Pro (HAT)"


def test_the_two_hdmi_ports_are_tellable_apart():
    names = [d.friendly_name for d in parse_aplay_output(APLAY_AMP100) if d.type is DeviceType.BUILTIN_HDMI]
    assert names == ["HDMI Output 1", "HDMI Output 2"]


def test_onboard_jack_is_classified_and_named():
    jack = parse_aplay_output(APLAY_ONBOARD)[0]
    assert jack.type is DeviceType.BUILTIN_HEADPHONES
    assert jack.friendly_name == "Built-in Headphones (3.5mm)"


def test_hdmi_wins_over_the_hat_markers():
    # 'i2s-hifi-0' contains no HAT marker, but the check order still matters: were HAT tested first,
    # a board whose description mentioned 'amp' or 'digi' would shadow a real HDMI card.
    assert parse_aplay_output(APLAY_AMP100)[0].type is DeviceType.BUILTIN_HDMI


# -- the identity regression ----------------------------------------------------------------------


def test_identity_survives_card_renumbering():
    """The same HAT keeps its id when the card number moves; only hw_id follows the hardware."""
    before = parse_aplay_output(APLAY_AMP100)[2]
    after = parse_aplay_output(APLAY_AMP100_RENUMBERED)[0]

    assert before.id == after.id == "sndrpihifiberry:0"  # what settings.json stores
    assert before.hw_id == "hw:2,0"
    assert after.hw_id == "hw:0,0"  # ...and what would have been silently wrong


def test_id_is_not_the_hw_address():
    # Guards against a well-meaning "simplification" back to the Snapcast shape.
    for device in parse_aplay_output(APLAY_AMP100):
        assert not device.id.startswith("hw:")


# -- resolution ------------------------------------------------------------------------------------


def test_find_device_by_stable_id():
    devices = parse_aplay_output(APLAY_AMP100)
    assert find_device("sndrpihifiberry:0", devices).card == 2


def test_find_device_by_hw_address_and_card_name():
    devices = parse_aplay_output(APLAY_AMP100)
    assert find_device("hw:2,0", devices).id == "sndrpihifiberry:0"
    assert find_device("sndrpihifiberry", devices).id == "sndrpihifiberry:0"


@pytest.mark.parametrize(
    "capture, spec, expected_id",
    [
        # The exact values sitting in docker/units.conf today — a unit that has never been through
        # the GUI must keep the output it booted with.
        (APLAY_ONBOARD, "bcm2835", "Headphones:0"),
        (APLAY_AMP100, "snd_rpi_hifiberry_dacplus", "sndrpihifiberry:0"),
    ],
)
def test_legacy_plum_dac_device_values_still_resolve(capture, spec, expected_id):
    assert find_device(spec, parse_aplay_output(capture)).id == expected_id


def test_ambiguous_spec_resolves_to_nothing():
    """Two identical DACs: a coin-flip would give the user a different room tomorrow."""
    twins = [
        AudioDevice(
            card=c,
            device=0,
            card_name=f"Device{c}",
            card_description="Generic USB DAC",
            device_description="USB Audio",
            type=DeviceType.USB,
            friendly_name="Generic USB DAC (USB)",
            is_available=True,
        )
        for c in (1, 2)
    ]
    assert find_device("Generic USB DAC", twins) is None


def test_missing_spec_resolves_to_nothing():
    assert find_device("", parse_aplay_output(APLAY_AMP100)) is None
    assert find_device("no-such-card", parse_aplay_output(APLAY_AMP100)) is None


# -- in-use detection ------------------------------------------------------------------------------

HELD_STATUS = """state: PREPARED
owner_pid   : 1211678
trigger_time: 0.000000000
tstamp      : 860872.031799013
delay       : 0
avail       : 2880
"""


def _fake_proc(tmp_path, card, device, subs):
    for sub, contents in enumerate(subs):
        status = tmp_path / f"card{card}" / f"pcm{device}p" / f"sub{sub}"
        status.mkdir(parents=True, exist_ok=True)
        (status / "status").write_text(contents)


def test_closed_pcm_is_not_in_use(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_devices, "PROC_ASOUND", str(tmp_path))
    _fake_proc(tmp_path, 2, 0, ["closed\n"])
    assert audio_devices._pcm_in_use(2, 0) == (False, None)


def test_held_pcm_reports_its_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_devices, "PROC_ASOUND", str(tmp_path))
    _fake_proc(tmp_path, 2, 0, [HELD_STATUS])
    assert audio_devices._pcm_in_use(2, 0) == (True, 1211678)


def test_a_stream_on_any_subdevice_counts(tmp_path, monkeypatch):
    """bcm2835 exposes eight subdevices; checking only sub0 misses a stream on sub3."""
    monkeypatch.setattr(audio_devices, "PROC_ASOUND", str(tmp_path))
    _fake_proc(tmp_path, 0, 0, ["closed\n"] * 3 + [HELD_STATUS] + ["closed\n"] * 4)
    in_use, pid = audio_devices._pcm_in_use(0, 0)
    assert in_use is True
    assert pid == 1211678


def test_owner_pid_zero_still_counts_as_in_use(tmp_path, monkeypatch):
    """Read from inside the container the holder is in another PID namespace and reads as 0.

    The boolean has to survive that; only the pid is best-effort.
    """
    monkeypatch.setattr(audio_devices, "PROC_ASOUND", str(tmp_path))
    _fake_proc(tmp_path, 0, 0, ["state: RUNNING\nowner_pid   : 0\ndelay       : 0\n"])
    assert audio_devices._pcm_in_use(0, 0) == (True, None)


def test_masked_proc_asound_is_not_an_error(tmp_path, monkeypatch):
    """Docker masks /proc/asound: the directory exists and is empty. Must read as 'unknown'."""
    monkeypatch.setattr(audio_devices, "PROC_ASOUND", str(tmp_path))
    assert audio_devices._pcm_in_use(9, 0) == (False, None)


# -- availability ----------------------------------------------------------------------------------


def _list_with(monkeypatch, capture, *, exposed, in_use=(), active=None):
    monkeypatch.setattr(audio_devices, "_run", lambda *a, **k: (True, capture))
    monkeypatch.setattr(audio_devices, "_portaudio_outputs", lambda **k: exposed)
    monkeypatch.setattr(
        audio_devices, "_pcm_in_use", lambda c, d: ((c, d) in in_use, 999 if (c, d) in in_use else None)
    )
    return audio_devices.list_output_devices(active_spec=active)


def test_a_held_device_stays_available_even_though_portaudio_lost_it(monkeypatch):
    """The measured Amp100 case: our player holds the card, so PortAudio publishes NOTHING.

    Availability derived from the probe alone would grey out the output that is currently playing.
    """
    devices = _list_with(monkeypatch, APLAY_AMP100, exposed={}, in_use={(2, 0)})
    hat = devices[2]
    assert hat.is_available is True
    assert hat.in_use is True
    assert hat.unavailable_reason is None


def test_active_device_stays_available_with_no_portaudio_and_no_proc(monkeypatch):
    """The container case: /proc/asound masked AND PortAudio blind, because we hold the card.

    Nothing but `active_spec` can save the picker here — without it the unit would report that the
    output it is audibly playing through cannot be selected.
    """
    devices = _list_with(monkeypatch, APLAY_AMP100, exposed={}, in_use=(), active="sndrpihifiberry:0")
    hat = devices[2]
    assert hat.is_active is True
    assert hat.is_available is True
    assert hat.unavailable_reason is None


def test_idle_card_absent_from_portaudio_is_the_only_unavailable_case(monkeypatch):
    # HDMI with no display: closed, yet PortAudio still will not open it.
    devices = _list_with(monkeypatch, APLAY_AMP100, exposed={(2, 0): 0})
    hdmi, hat = devices[0], devices[2]
    assert hdmi.is_available is False
    assert "no display is attached" in hdmi.unavailable_reason
    assert hat.is_available is True
    assert hat.portaudio_index == 0


def test_portaudio_index_is_carried_not_assumed_from_the_card_number(monkeypatch):
    """ALSA card 2 was PortAudio index 0 on the real unit; the two numbering schemes are unrelated."""
    devices = _list_with(monkeypatch, APLAY_AMP100, exposed={(2, 0): 0})
    assert devices[2].card == 2
    assert devices[2].portaudio_index == 0


def test_no_soundcards_is_an_empty_list_not_a_crash(monkeypatch):
    monkeypatch.setattr(audio_devices, "_run", lambda *a, **k: (False, "aplay: device_list:279: no soundcards found..."))
    assert audio_devices.list_output_devices() == []


# -- the PortAudio bridge --------------------------------------------------------------------------


class _RacyPortAudio:
    """Stands in for sounddevice, and reports if two threads are ever inside it at once.

    The real consequence of overlap is a SIGSEGV, not an exception — Pa_Terminate frees state that
    the other thread is walking. There is nothing to catch, so the only way to test it is to detect
    the overlap ourselves.
    """

    def __init__(self):
        self.inside = 0
        self.overlapped = False
        self.enumerations = 0

    def _terminate(self):
        self.inside += 1
        if self.inside > 1:
            self.overlapped = True
        time.sleep(0.002)  # widen the window a real Pa_Terminate would occupy

    def _initialize(self):
        time.sleep(0.002)
        self.inside -= 1
        if self.inside < 0:
            self.overlapped = True

    def query_devices(self):
        self.enumerations += 1
        return [{"name": "bcm2835 Headphones: - (hw:0,0)", "max_output_channels": 2}]


@pytest.fixture
def racy_portaudio(monkeypatch):
    fake = _RacyPortAudio()
    monkeypatch.setattr(audio_devices, "sd", fake)
    monkeypatch.setattr(audio_devices, "_portaudio_cache", None)
    return fake


def test_concurrent_enumeration_never_overlaps(racy_portaudio, monkeypatch):
    """The config API crash-looped on this exact pattern (SIGSEGV, no traceback).

    The GUI fetches the device list and the current output in one Promise.all; Flask serves them on
    two threads; both re-initialised PortAudio at once. Sequential calls never reproduce it, which is
    why it survived a round of hardware testing.
    """
    monkeypatch.setattr(audio_devices, "PORTAUDIO_CACHE_S", 0)  # force every call to do real work

    barrier = threading.Barrier(8)

    def hammer():
        barrier.wait()  # all threads enter together
        audio_devices._portaudio_outputs()

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert racy_portaudio.overlapped is False


def test_repeated_calls_are_served_from_the_cache(racy_portaudio):
    """Two endpoints milliseconds apart should cost one enumeration, not two."""
    audio_devices._portaudio_outputs()
    audio_devices._portaudio_outputs()
    audio_devices._portaudio_outputs()

    assert racy_portaudio.enumerations == 1


def test_force_bypasses_the_cache(racy_portaudio):
    """The player re-reads after closing its stream; a stale map would omit the card it just freed."""
    audio_devices._portaudio_outputs()
    audio_devices._portaudio_outputs(force=True)

    assert racy_portaudio.enumerations == 2


@pytest.mark.parametrize(
    "name, expected",
    [
        ("snd_rpi_hifiberry_dacplus: HiFiBerry DAC+ Pro HiFi pcm512x-hifi-0 (hw:2,0)", (2, 0)),
        ("bcm2835 Headphones: - (hw:0,0)", (0, 0)),
        ("sysdefault", None),
        ("default", None),
    ],
)
def test_alsa_address_is_recovered_from_the_portaudio_name(name, expected):
    match = audio_devices.PORTAUDIO_HW.search(name)
    assert (match and (int(match.group(1)), int(match.group(2)))) == (expected or None)


# -- test_device guardrail ---------------------------------------------------------------------------


def test_testing_the_active_device_explains_itself(monkeypatch):
    """speaker-test on a held card returns EBUSY, which reads as 'broken' when it means 'in use'."""
    devices = parse_aplay_output(APLAY_AMP100)
    devices[2].in_use = True
    monkeypatch.setattr(audio_devices, "list_output_devices", lambda **k: devices)
    ran = []
    monkeypatch.setattr(audio_devices, "_run", lambda *a, **k: ran.append(a) or (True, ""))

    ok, message = audio_devices.test_device("sndrpihifiberry:0")
    assert ok is False
    assert "already this unit's output" in message
    assert ran == []  # and we never shelled out to find that out
