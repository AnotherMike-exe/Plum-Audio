"""Unit tests for scripts/host-setup/configure-audio-hat.sh.

The script needs a Pi to do anything useful, but every bug it has produced so far was in the
config.txt TEXT EDITING, which needs nothing at all. PLUM_CONFIG_TXT points it at a fixture so these
run anywhere.

Four bugs, all found on hardware on 2026-08-04, all cheap to have caught here:

  * `match($0, /re/, m)` is a GAWK extension; Debian ships MAWK, where it is a syntax error. The
    failure was swallowed, so --unity reported "no HAT card found" on a unit with a HAT plainly in
    `aplay -l` — the mixer fix silently did nothing.
  * Each apply prepended a blank separator that the strip pass did not remove, so apply/revert cycles
    grew config.txt a line at a time.
  * The block was APPENDED, which put it after `dtoverlay=vc4-kms-v3d` — measured to cost an HDMI
    audio output (vc4hdmi0 stopped enumerating). The HAT worked either way, which is exactly why it
    would have shipped unnoticed.
  * A hand-written `dtoverlay=` (what Plum-Snapcast's README told users to add) was left in place
    alongside ours, declaring the overlay twice.

Run: `pytest tests/Unit/test_configure_audio_hat.py`.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "host-setup" / "configure-audio-hat.sh"

pytestmark = pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")

# Trimmed from the real /boot/firmware/config.txt on .7.204 — including the hand-written HAT overlay
# a Plum-Snapcast unit arrives with, and the unrelated overlays that must not be touched.
PRISTINE = """\
# For more options and information see http://rptl.io/configtxt

[all]
dtparam=i2c_arm=on
dtparam=audio=off
dtoverlay=hifiberry-amp100

camera_auto_detect=1
display_auto_detect=1

# Enable DRM VC4 V3D driver
dtoverlay=vc4-kms-v3d
max_framebuffers=2

[cm5]
dtoverlay=dwc2,dr_mode=host
"""

ONBOARD_DEFAULT = """\
[all]
dtparam=audio=on
dtoverlay=vc4-kms-v3d
"""


def run(config: Path, *args, expect_ok=True):
    result = subprocess.run(
        ["bash", str(SCRIPT), *args],
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "PLUM_CONFIG_TXT": str(config)},
        capture_output=True,
        text=True,
    )
    if expect_ok:
        assert result.returncode == 0, f"exit {result.returncode}\n{result.stdout}\n{result.stderr}"
    return result


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config.txt"
    path.write_text(PRISTINE)
    return path


def _lines(config):
    return config.read_text().splitlines()


# -- placement ---------------------------------------------------------------------------------


def test_block_is_inserted_before_the_first_overlay(config):
    """Appending instead costs an HDMI audio output — measured, not theorised."""
    run(config, "--overlay", "hifiberry-amp100")
    lines = _lines(config)

    block_at = next(i for i, line in enumerate(lines) if line.startswith("# >>> plum-audio hat"))
    vc4_at = next(i for i, line in enumerate(lines) if line == "dtoverlay=vc4-kms-v3d")
    assert block_at < vc4_at


def test_block_contains_the_overlay_and_disables_onboard(config):
    run(config, "--overlay", "hifiberry-amp100")
    text = config.read_text()
    block = text.split("# >>> plum-audio hat")[1].split("# <<< plum-audio hat")[0]

    assert "dtoverlay=hifiberry-amp100" in block
    assert "dtparam=audio=off" in block


def test_keep_onboard_leaves_the_soc_codec_enabled(tmp_path):
    path = tmp_path / "config.txt"
    path.write_text(ONBOARD_DEFAULT)
    run(path, "--overlay", "hifiberry-dac", "--keep-onboard")

    text = path.read_text()
    assert "#plum-audio-disabled: dtparam=audio=on" not in text
    block = text.split("# >>> plum-audio hat")[1].split("# <<< plum-audio hat")[0]
    assert "dtparam=audio=off" not in block


def test_keep_onboard_asks_for_onboard_audio_explicitly(tmp_path):
    """Omitting the line is not the same as asking for it.

    With no dtparam=audio at all the firmware leaves the bcm2835 codec OFF and no Headphones card
    enumerates. Measured on .7.204: both `off` lines removed, rebooted, still only the two HDMI
    outputs and the HAT. So the block has to say `on` out loud.
    """
    path = tmp_path / "config.txt"
    path.write_text(HAND_DISABLED)
    run(path, "--overlay", "hifiberry-amp100", "--keep-onboard")

    block = path.read_text().split("# >>> plum-audio hat")[1].split("# <<< plum-audio hat")[0]
    assert "dtparam=audio=on" in block


def test_onboard_audio_is_commented_out_by_default(tmp_path):
    path = tmp_path / "config.txt"
    path.write_text(ONBOARD_DEFAULT)
    run(path, "--overlay", "hifiberry-dac")

    assert "#plum-audio-disabled: dtparam=audio=on" in path.read_text()


# A unit configured BEFORE this script existed: someone followed HiFiBerry's own instructions and
# added `dtparam=audio=off` by hand, outside anything we manage. Taken verbatim from .7.204.
HAND_DISABLED = """\
dtparam=i2c_arm=on

# HiFiBerry AMP100 - disabling onboard audio as recommended
dtparam=audio=off

camera_auto_detect=1
dtoverlay=vc4-kms-v3d
max_framebuffers=2
"""


def test_keep_onboard_disarms_a_hand_written_audio_off(tmp_path):
    """--keep-onboard has to REMOVE an existing off, not merely decline to add one.

    The block is the only region this script rewrites, so a `dtparam=audio=off` anywhere else in the
    file still kills the jack. On .7.204 exactly that line sat 17 lines above the managed block, and
    without this the flag would have "run successfully" and changed nothing observable.
    """
    path = tmp_path / "config.txt"
    path.write_text(HAND_DISABLED)
    run(path, "--overlay", "hifiberry-amp100", "--keep-onboard")

    text = path.read_text()
    assert "#plum-audio-disabled: dtparam=audio=off" in text, "the hand-written off must be disarmed"
    assert not re.search(r"^\s*dtparam=audio=off\s*$", text, re.M), "no live audio=off may remain"


def test_the_default_still_leaves_a_hand_written_audio_off_alone(tmp_path):
    """Without --keep-onboard, an existing off is already what we want — do not churn it."""
    path = tmp_path / "config.txt"
    path.write_text(HAND_DISABLED)
    run(path, "--overlay", "hifiberry-amp100")

    text = path.read_text()
    assert "#plum-audio-disabled: dtparam=audio=off" not in text
    block = text.split("# >>> plum-audio hat")[1].split("# <<< plum-audio hat")[0]
    assert "dtparam=audio=off" in block


def test_revert_puts_a_disarmed_audio_off_back(tmp_path):
    """--revert is meant to be exact, in both directions."""
    path = tmp_path / "config.txt"
    original = HAND_DISABLED
    path.write_text(original)
    run(path, "--overlay", "hifiberry-amp100", "--keep-onboard")
    run(path, "--revert")

    assert path.read_text() == original


# -- adoption ----------------------------------------------------------------------------------


def test_a_hand_written_overlay_is_commented_not_duplicated(config):
    """Plum-Snapcast's README told users to add this line by hand; units arrive carrying it."""
    run(config, "--overlay", "hifiberry-amp100")
    lines = _lines(config)

    assert "#plum-audio-disabled: dtoverlay=hifiberry-amp100" in lines
    assert lines.count("dtoverlay=hifiberry-amp100") == 1  # ours, once


def test_unrelated_overlays_are_left_alone(config):
    run(config, "--overlay", "hifiberry-amp100")
    lines = _lines(config)

    assert "dtoverlay=vc4-kms-v3d" in lines
    assert "dtoverlay=dwc2,dr_mode=host" in lines
    assert "dtparam=i2c_arm=on" in lines


def test_switching_overlay_replaces_rather_than_stacks(config):
    run(config, "--overlay", "hifiberry-amp100")
    run(config, "--overlay", "iqaudio-dacplus")
    lines = _lines(config)

    assert lines.count("dtoverlay=iqaudio-dacplus") == 1
    assert "dtoverlay=hifiberry-amp100" not in lines  # the old one is commented out


# -- idempotency + revert ------------------------------------------------------------------------


def test_repeated_applies_leave_exactly_one_block(config):
    for _ in range(4):
        run(config, "--overlay", "hifiberry-amp100")
    text = config.read_text()

    assert text.count("# >>> plum-audio hat") == 1
    assert text.count("# <<< plum-audio hat") == 1


def test_repeated_applies_do_not_grow_the_file(config):
    run(config, "--overlay", "hifiberry-amp100")
    after_first = config.read_text()
    for _ in range(3):
        run(config, "--overlay", "hifiberry-amp100")

    assert config.read_text() == after_first


def test_revert_restores_the_original_byte_for_byte(config):
    for _ in range(3):
        run(config, "--overlay", "hifiberry-amp100")
    run(config, "--revert")

    assert config.read_text() == PRISTINE


def test_revert_on_an_untouched_file_is_a_no_op(config):
    run(config, "--revert")
    assert config.read_text() == PRISTINE


def test_revert_restores_onboard_audio(tmp_path):
    path = tmp_path / "config.txt"
    path.write_text(ONBOARD_DEFAULT)
    run(path, "--overlay", "hifiberry-dac")
    run(path, "--revert")

    assert path.read_text() == ONBOARD_DEFAULT


# -- arguments ---------------------------------------------------------------------------------


def test_listing_overlays_needs_no_config(config):
    result = run(config, "--list")
    assert "hifiberry-amp100" in result.stdout
    assert "iqaudio-dacplus" in result.stdout


def test_no_action_is_an_error(config):
    result = run(config, expect_ok=False)
    assert result.returncode != 0
    assert config.read_text() == PRISTINE  # and nothing was written


def test_unknown_argument_is_rejected(config):
    result = run(config, "--wat", expect_ok=False)
    assert result.returncode != 0


def test_a_backup_is_written_beside_the_config(config):
    run(config, "--overlay", "hifiberry-amp100")
    assert (config.parent / "config.txt.plum.bak").read_text() == PRISTINE


def test_no_temporary_files_are_left_behind(config):
    run(config, "--overlay", "hifiberry-amp100")
    leftovers = [p.name for p in config.parent.iterdir() if p.name.endswith((".plumtmp", ".plumblock"))]
    assert leftovers == []
