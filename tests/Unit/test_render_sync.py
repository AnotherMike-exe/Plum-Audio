"""Unit tests for timestamp-locked playback in AlsaRenderer — the multi-room phase lock.

The bug these exist to keep fixed: four units on one AirPlay source played a quarter to half a
second apart, and the offset moved every session. It was never a regression and never a buffer-size
problem. The renderer free-ran — it played each chunk the moment it arrived, so a unit's phase was
whatever its first chunk happened to land on, and every padded underrun pushed that unit
permanently later. `target_buffer_ms` did not gate it; that value only ever reached a log line.

`test_two_units_that_start_300_ms_apart_end_up_in_phase` is the one that matters. Everything else
here holds up one piece of the mechanism so that a failure says WHICH piece.

No PortAudio and no server. The renderer's callback is a pure function of (block, DAC time info,
clock), so the whole lock is testable headless: the fake clock is the client clock aiosendspin would
hand us, and the fake time info is what PortAudio reports about its own output buffer.

Run: `pytest tests/Unit/test_render_sync.py`.
"""

import struct
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "scripts"))

pytest.importorskip("numpy", reason="sendspin_player imports numpy at module scope")
pytest.importorskip("aiosendspin", reason="sendspin_player imports aiosendspin at module scope")

import sendspin_player  # noqa: E402
from sendspin_player import AlsaRenderer  # noqa: E402

RATE = 44100
CHANNELS = 2
BITS = 16
BPF = CHANNELS * BITS // 8
BLOCK = 441  # frames — exactly 10 ms at 44.1 kHz, so the arithmetic below stays readable
BLOCK_US = BLOCK * 1_000_000 // RATE
DAC_LEAD_US = 5_000  # what PortAudio says about how far ahead of the DAC we are filling


class FakeTimeInfo:
    """PaStreamCallbackTimeInfo. Only the GAP between the two is ever read."""

    def __init__(self, lead_us: int = DAC_LEAD_US, *, broken: bool = False) -> None:
        self.currentTime = 100.0  # noqa: N815 - PortAudio's own field name
        self.outputBufferDacTime = self.currentTime + (0.0 if broken else lead_us / 1_000_000)  # noqa: N815


class Unit:
    """One renderer, its clock, and a record of what it actually played when.

    `served` holds (block_play_us, frame_index) for every block that carried audio. frame_index is
    read back out of the PCM itself — each frame is stamped with its own number — so the assertions
    are about the sample that reached the DAC, not about the renderer's own bookkeeping.
    """

    def __init__(self, *, lead_us: int = DAC_LEAD_US, broken_time_info: bool = False) -> None:
        self.now_us = 0
        self.renderer = AlsaRenderer(RATE, CHANNELS, BITS, device=None, target_buffer_ms=300)
        self.renderer.set_clock(lambda: self.now_us)
        self.time_info = FakeTimeInfo(lead_us, broken=broken_time_info)
        self.served: list[tuple[int, int]] = []
        self.blocks = 0

    def block_play_us(self) -> int:
        return self.now_us + DAC_LEAD_US

    def render(self) -> bytes:
        out = memoryview(bytearray(BLOCK * BPF))
        self.renderer._callback(out, BLOCK, self.time_info, 0)  # noqa: SLF001
        data = bytes(out)
        self.blocks += 1
        first = first_frame(data)
        if first is not None:
            self.served.append((self.block_play_us(), first))
        return data


STAMP_WRAP = 30000  # int16 headroom; frame indices are compared modulo this


def stamp(frame_index: int) -> int:
    """Frame n is stamped n+1, so that a silent frame (0) is never mistaken for frame 0."""
    return frame_index % STAMP_WRAP + 1


def pcm(start_frame: int, frames: int) -> bytes:
    """PCM whose every frame carries its own index, so a played sample identifies itself."""
    return b"".join(struct.pack("<hh", stamp(start_frame + i), stamp(start_frame + i)) for i in range(frames))


def frame_at(data: bytes, index: int) -> int | None:
    """The frame index stamped at position `index` of a rendered block, or None where it is silent."""
    value = struct.unpack_from("<h", data, index * BPF)[0]
    return None if value == 0 else value - 1


def first_frame(data: bytes) -> int | None:
    """The index stamped on the first non-silent frame of a rendered block, or None if silent."""
    for i in range(len(data) // BPF):
        played = frame_at(data, i)
        if played is not None:
            return played
    return None


def assert_frame(played: int | None, expected: int, tolerance: int, message: str) -> None:
    """Compare two frame indices modulo the stamp wrap, so a long run stays comparable."""
    assert played is not None, message
    delta = (played - expected + STAMP_WRAP // 2) % STAMP_WRAP - STAMP_WRAP // 2
    assert abs(delta) <= tolerance, f"{message}: played frame {played}, expected {expected} (+-{tolerance})"


def timeline(unit: Unit, epoch_us: int, chunk: int, *, at_us: int | None = None) -> None:
    """Enqueue chunk number `chunk` of a stream whose frame 0 is due at `epoch_us`."""
    if at_us is not None:
        unit.now_us = at_us
    unit.renderer.enqueue(pcm(chunk * BLOCK, BLOCK), epoch_us + chunk * BLOCK_US)


def expected_frame(block_play_us: int, epoch_us: int) -> int:
    return (block_play_us - epoch_us) * RATE // 1_000_000 % STAMP_WRAP


# -- the deadline ----------------------------------------------------------------------------------


def test_audio_that_is_not_due_yet_is_held_as_silence():
    unit = Unit()
    timeline(unit, epoch_us=500_000, chunk=0)

    data = unit.render()

    assert first_frame(data) is None, "played a chunk 500 ms before it was due"
    assert unit.renderer.hold_frames == BLOCK
    # A deadline hold is the mechanism working, not a dropout. Charging it to starvation would make
    # every session start report the speaker as failing — and _health reads exactly that counter.
    assert unit.renderer.starved_frames == 0
    assert unit.renderer.starvations == 0


def test_the_first_frame_is_served_in_the_block_that_covers_its_play_time():
    unit = Unit()
    epoch = 50_000
    for k in range(12):
        timeline(unit, epoch, k)

    while unit.block_play_us() + BLOCK_US <= epoch:
        assert first_frame(unit.render()) is None, "played before the deadline"
        unit.now_us += BLOCK_US

    data = unit.render()

    offset = (epoch - unit.block_play_us()) * RATE // 1_000_000  # frames of hold left in this block
    assert first_frame(data) == 0, "frame 0 was not the first frame played"
    assert frame_at(data, offset) == 0, "frame 0 did not land on its deadline"
    assert frame_at(data, offset - 1) is None, "audio started before the deadline"


def test_overdue_audio_is_skipped_rather_than_queued():
    """A late join must not play the backlog. It must jump to the frame that is due NOW."""
    unit = Unit()
    epoch = 0
    for k in range(60):  # 600 ms of stream, all of it already due
        timeline(unit, epoch, k)
    unit.now_us = 300_000  # we only got here 300 ms in

    data = unit.render()

    assert_frame(first_frame(data), expected_frame(unit.block_play_us(), epoch), 2, "did not skip the backlog")
    # The acquisition is a lock, not a step: `steps` means "something moved a unit that was already
    # in phase", and a late join has never been in phase.
    assert unit.renderer.locks == 1
    assert unit.renderer.steps == 0


# -- the headline ----------------------------------------------------------------------------------


def test_two_units_that_start_300_ms_apart_end_up_in_phase():
    """The bug, as a test: same source, same timestamps, two units that begin 300 ms apart.

    Before the lock each unit played whatever it had the moment it had it, so `late` stayed 300 ms
    behind `early` forever. Both now serve the frame their own DAC deadline asks for, so the only
    thing that can separate them is the deadline itself.
    """
    epoch = 400_000
    early, late = Unit(), Unit()
    late.now_us = 3_300  # a different DAC callback phase, deliberately not a whole block

    for k in range(120):  # 1.2 s of stream
        arrival = k * BLOCK_US
        timeline(early, epoch, k, at_us=arrival)
        # `late` joins 300 ms after the fact and then keeps up.
        timeline(late, epoch, k, at_us=arrival + 300_000)
        for unit in (early, late):
            unit.render()
            unit.now_us += BLOCK_US

    for unit, name in ((early, "early"), (late, "late")):
        assert unit.served, f"{name} unit played nothing at all"
        for block_play_us, played in unit.served[-20:]:
            assert_frame(played, expected_frame(block_play_us, epoch), 2, f"{name} unit is off its own deadline")

    # And therefore with each other: the same instant on the shared clock, the same sample.
    early_map = dict(early.served)
    late_map = dict(late.served)
    shared = sorted(set(early_map) & set(late_map))
    assert shared, "the two units never filled a block for the same instant"
    for when in shared[-10:]:
        assert abs(early_map[when] - late_map[when]) <= 2


def test_a_re_alignment_counts_as_one_step_not_one_per_block():
    """A correction spans many callbacks. `steps` must say how many times a unit was moved, not how
    long the correction took — the rig read 16 for a single 150 ms re-alignment before this."""
    unit = Unit()
    epoch = DAC_LEAD_US
    for k in range(20):
        timeline(unit, epoch, k, at_us=k * BLOCK_US)
        unit.now_us = k * BLOCK_US
        unit.render()
    assert unit.renderer.locks == 1
    assert unit.renderer.steps == 0

    # The timeline jumps 150 ms into the future, as a stream restart does.
    jump = 150_000
    for k in range(20, 60):
        timeline(unit, epoch + jump, k, at_us=k * BLOCK_US)
        unit.now_us = k * BLOCK_US
        unit.render()

    assert unit.renderer.steps == 1, "counted one step per callback of the hold"
    assert unit.renderer.locks == 2, "did not report landing back on the deadline"


# -- drift -----------------------------------------------------------------------------------------


def test_a_unit_inside_the_deadband_is_left_alone():
    unit = Unit()
    epoch = DAC_LEAD_US  # frame 0 is due exactly when the first block reaches the DAC
    for k in range(40):
        timeline(unit, epoch, k)
    unit.now_us = 0

    for _ in range(40):
        unit.render()
        unit.now_us += BLOCK_US

    assert unit.renderer.trims == 0, "trimmed a unit that was already in phase"
    assert unit.renderer.steps == 0


def test_drift_is_corrected_one_frame_at_a_time():
    """A DAC running slow shows up as a steady error. It must be trimmed, never stepped."""
    unit = Unit()
    epoch = DAC_LEAD_US
    blocks = 1000  # 10 s of playback
    drift_us = 0
    for k in range(blocks):
        # Each block the DAC falls 3 us further behind the client clock — 300 ppm, about triple the
        # worst crystal error between two Pis, so 10 s is enough to leave the deadband.
        drift_us += 3
        timeline(unit, epoch, k, at_us=k * BLOCK_US)
        unit.now_us = k * BLOCK_US + drift_us
        unit.render()

    assert unit.renderer.trims > 0, "drift was never corrected"
    assert unit.renderer.steps == 0, "drift was corrected with an audible step"
    # Single frames only: 23 us each, and rate-limited. A run this long cannot have trimmed more
    # than one frame per SYNC_TRIM_EVERY callbacks.
    assert unit.renderer.trims <= blocks // sendspin_player.SYNC_TRIM_EVERY + 1
    # And the point of trimming: the error stays bounded instead of growing with the drift.
    assert abs(unit.renderer.sync_ema_us) < 2 * sendspin_player.SYNC_DEADBAND_US


# -- the fallbacks ---------------------------------------------------------------------------------


def test_a_chunk_with_no_play_time_free_runs():
    """No schedule means the pre-2026-09-13 renderer: play it now, and say we are not locked."""
    unit = Unit()
    unit.renderer.enqueue(pcm(0, BLOCK), None)

    data = unit.render()

    assert first_frame(data) == 0
    assert unit.renderer.locked is False
    assert unit.renderer.sync_report()["locked"] is False


def test_useless_dac_time_falls_back_to_the_streams_latency():
    unit = Unit(broken_time_info=True)
    unit.renderer._latency_us = 20_000  # noqa: SLF001 - what PortAudio reported at open
    epoch = 20_000
    for k in range(20):
        timeline(unit, epoch, k)
    unit.now_us = 0

    unit.render()

    assert unit.renderer.locked is True, "gave up on the lock instead of using the known latency"


def test_no_dac_time_and_no_latency_free_runs_rather_than_guessing():
    unit = Unit(broken_time_info=True)
    unit.renderer._latency_us = 0  # noqa: SLF001
    unit.renderer.enqueue(pcm(0, BLOCK), 900_000)  # due far in the future

    data = unit.render()

    assert first_frame(data) == 0, "held audio against a deadline it cannot locate"
    assert unit.renderer.locked is False


def test_the_kill_switch_returns_the_free_running_drain(monkeypatch):
    monkeypatch.setattr(sendspin_player, "SYNC_LOCK", False)
    unit = Unit()
    unit.renderer.enqueue(pcm(0, BLOCK), 900_000)

    assert first_frame(unit.render()) == 0
    assert unit.renderer.locked is False


# -- bookkeeping -----------------------------------------------------------------------------------


def test_the_buffer_cap_drops_the_oldest_chunks():
    unit = Unit()
    over = int(sendspin_player.MAX_BUFFER_MS * RATE / 1000) * BPF * 2
    frames = over // BPF
    unit.renderer.enqueue(pcm(0, frames), 0)

    assert unit.renderer._buffered <= unit.renderer._max_bytes  # noqa: SLF001
    assert unit.renderer.overruns == 1


def test_a_flush_forgets_the_timeline():
    unit = Unit()
    for k in range(10):
        timeline(unit, 0, k)
    unit.render()

    unit.renderer.flush()

    assert unit.renderer._buffered == 0  # noqa: SLF001
    assert unit.renderer.sync_report() == {
        "locked": False,
        "aligned": False,
        "sync_err_ms": None,
        "sync_avg_ms": None,
        "locks": unit.renderer.locks,
        "steps": 0,
        "trims": 0,
    }
