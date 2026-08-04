#!/usr/bin/env python3
"""
Plum-Audio — ALSA playback device discovery for the output picker.

Ported from Plum-Snapcast's `audio_devices.py`, with three changes that the port forced. Each one
exists because the Snapcast shape produces a picker that looks right and does not work here.

1. THE IDENTITY IS THE CARD NAME, NOT `hw:C,D`.
   Snapcast persisted `hw:C,D` because snapclient hands that straight to ALSA. Card NUMBERS move:
   on the Amp100 unit the HAT is card 2, sitting behind two vc4hdmi cards, while on an onboard unit
   the analogue jack is card 0. An HDMI hotplug or a kernel update reorders them. Persisting the
   number means a unit silently opens the wrong card — or nothing — after a reboot, with every
   slider in the mesh still reading correct. The ALSA card *name* (`sndrpihifiberry`, `Headphones`)
   is stable, so `AudioDevice.id` is `<card_name>:<device>` and `hw:C,D` is re-derived per scan.

2. WE RENDER THROUGH PORTAUDIO, WHICH IS NOT ALSA.
   `sounddevice`'s `device=` is matched as a SUBSTRING of PortAudio's own device-name list; it will
   not accept an arbitrary ALSA PCM string. `sd.RawOutputStream(device="hw:1,0")` raises
   "No output device matching" whenever PortAudio has not enumerated card 1 — which is the normal
   case for vc4hdmi with no display attached. PortAudio does embed the literal `(hw:C,D)` in the
   names it does publish, so that suffix is the join between the two worlds. We resolve to a
   PortAudio *index* and hand that over, rather than passing a string and hoping the match is
   unique — two identical USB DACs would otherwise raise on an ambiguous match.

3. AVAILABILITY CANNOT BE PROBED BY OPENING.
   PortAudio enumerates by opening each PCM. A card already held exclusively therefore DISAPPEARS
   from `query_devices()` — verified on the Amp100, whose pcm512x has a single subdevice: while our
   own player held it, the output list came back EMPTY. Deriving availability from a live probe
   would mark every device unavailable — including the one audibly playing — the moment the player
   is running. So the inventory comes from `aplay -l` (which reads /proc and opens nothing), and
   "is something using it" comes from /proc/asound/<card>/pcm<D>p/sub*/status, which reports
   `closed` or a `state:`/`owner_pid:` block. A card that is idle AND absent from PortAudio is the
   only genuinely unopenable case, and that is the one we grey out. Because the container cannot
   read even that (see PROC_ASOUND), callers also pass the ACTIVE output in, which is the only
   signal that survives everywhere — list_output_devices() documents all three.

The mixer detection Snapcast carried (`amixer scontrols` -> a hardware mixer handed to snapclient)
is deliberately NOT ported: our renderer applies volume as software gain in the PortAudio callback,
and the three-volume model has no slot for an ALSA mixer. A HAT's hardware mixer is pinned to unity
once, by host provisioning — see scripts/host-setup/. The Amp100 ships at -22 dB on `Digital`, which
is exactly the kind of quieter-than-the-GUI-claims failure that model is meant to prevent.
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import re
import subprocess
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum

import unit_identity

logger = logging.getLogger("plum.audio_devices")

WATCH_INTERVAL_S = 5.0

# Serialises every Pa_Terminate/Pa_Initialize/query in this process — see _portaudio_outputs.
_PORTAUDIO_LOCK = threading.Lock()
_portaudio_cache: tuple[float, dict[tuple[int, int], int]] | None = None
PORTAUDIO_CACHE_S = 2.0

try:  # PortAudio is absent on a dev laptop and in unit tests; discovery must still parse.
    import sounddevice as sd
except Exception:  # noqa: BLE001 - a broken PortAudio install raises OSError, not ImportError
    sd = None

# `card 2: sndrpihifiberry [snd_rpi_hifiberry_dacplus], device 0: HiFiBerry DAC+ Pro HiFi ...[...]`
APLAY_LINE = re.compile(r"card\s+(\d+):\s+(\S+)\s+\[([^\]]+)\],\s+device\s+(\d+):\s+(\S+.*?)\s+\[([^\]]+)\]")
# PortAudio names carry the ALSA address verbatim: 'snd_rpi_... pcm512x-hifi-0 (hw:2,0)'
PORTAUDIO_HW = re.compile(r"\(hw:(\d+),(\d+)\)")
# The kernel appends the codec to a HAT's PCM name: 'HiFiBerry DAC+ Pro HiFi pcm512x-hifi-0'
CODEC_SUFFIX = re.compile(r"\s+HiFi\s+\S+-hifi-\d+$", re.IGNORECASE)

# Docker MASKS /proc/asound by default, and runc refuses to bind-mount anything back into /proc
# ("cannot be mounted because it is inside /proc"), so the config API — which runs in the container —
# sees an empty directory and would conclude every card is idle. docker-compose.yml therefore mounts
# the host's copy somewhere legal and points this at it. Default stays /proc/asound for a unit run
# outside the container and for the standalone probe below.
PROC_ASOUND = os.environ.get("PLUM_PROC_ASOUND", "/proc/asound")


class DeviceType(Enum):
    BUILTIN_HEADPHONES = "BUILTIN_HEADPHONES"
    BUILTIN_HDMI = "BUILTIN_HDMI"
    USB = "USB"
    HAT = "HAT"
    OTHER = "OTHER"


# Substrings that mark an I2S add-on board. Checked only AFTER headphones/HDMI/USB, because several
# are short enough to false-positive ("amp", "digi") against a description that is really something
# else. Extend freely — an unmatched HAT degrades to OTHER, which is cosmetic, not broken.
HAT_IDENTIFIERS = (
    "hifiberry",
    "iqaudio",
    "pisound",
    "audioinjector",
    "justboom",
    "allo",
    "boss",
    "khadas",
    "dacberry",
    "respeaker",
    "sndrpi",
    "pcm512x",
    "wm8960",
    "max98357",
    "es9023",
    "dacplus",
    "digi",
    "amp",
)


@dataclass
class AudioDevice:
    """One ALSA playback device, as the picker sees it."""

    card: int  # CURRENT index — volatile across reboots, never persist it
    device: int
    card_name: str  # ALSA card id, e.g. 'sndrpihifiberry' — stable, this is the identity
    card_description: str
    device_description: str
    type: DeviceType
    friendly_name: str
    is_available: bool
    unavailable_reason: str | None = None
    in_use: bool = False
    in_use_by_pid: int | None = None  # best-effort; 0/unknown inside the container
    is_active: bool = False  # this is the output the unit is configured to render to
    portaudio_index: int | None = None

    @property
    def id(self) -> str:
        """Stable identifier — what settings.json stores and the GUI round-trips."""
        return f"{self.card_name}:{self.device}"

    @property
    def hw_id(self) -> str:
        """Current ALSA address. For display and `speaker-test` only — re-derive it every scan."""
        return f"hw:{self.card},{self.device}"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "hw_id": self.hw_id,
            "card": self.card,
            "device": self.device,
            "card_name": self.card_name,
            "card_description": self.card_description,
            "device_description": self.device_description,
            "type": self.type.value,
            "friendly_name": self.friendly_name,
            "is_available": self.is_available,
            "unavailable_reason": self.unavailable_reason,
            "in_use": self.in_use,
            "is_active": self.is_active,
        }


# -- classification ----------------------------------------------------------------------------


def _identify(card_name: str, card_description: str, device_description: str) -> DeviceType:
    combined = f"{card_name} {card_description} {device_description}".lower()
    if "headphone" in combined:
        return DeviceType.BUILTIN_HEADPHONES
    if "hdmi" in combined:
        return DeviceType.BUILTIN_HDMI
    if "usb" in combined:
        return DeviceType.USB
    if any(marker in combined for marker in HAT_IDENTIFIERS):
        return DeviceType.HAT
    return DeviceType.OTHER


def _friendly_name(device_type: DeviceType, card_name: str, card_description: str, device_description: str) -> str:
    if device_type is DeviceType.BUILTIN_HEADPHONES:
        return "Built-in Headphones (3.5mm)"

    if device_type is DeviceType.BUILTIN_HDMI:
        # A Pi 4 exposes vc4hdmi0 and vc4hdmi1; numbering them off the card name keeps the two
        # ports tellable apart, which "Built-in HDMI" twice would not.
        port = re.search(r"(\d+)$", card_name)
        return f"HDMI Output {int(port.group(1)) + 1}" if port else "HDMI Output"

    if device_type is DeviceType.HAT:
        # The kernel's PCM name is the most human of the three ('HiFiBerry DAC+ Pro HiFi
        # pcm512x-hifi-0'); drop the codec tail. Note this reports what the OVERLAY loaded, not what
        # is soldered to the board — an Amp100 uses the DAC+ Pro driver and so names itself that.
        name = CODEC_SUFFIX.sub("", device_description).strip()
        return f"{name or card_description} (HAT)"

    if device_type is DeviceType.USB:
        name = device_description.replace("USB Audio", "").strip() or card_description
        return f"{name} (USB)"

    return device_description or card_description


# -- in-use detection (read-only; opens nothing) -----------------------------------------------


def _pcm_in_use(card: int, device: int) -> tuple[bool, int | None]:
    """(is anything rendering to this PCM, owning pid if knowable).

    Reading /proc is the only check safe to run against a live unit: every alternative (opening the
    PCM, `speaker-test`, PortAudio's own probe) either fails on a busy device or steals it.

    EVERY subdevice is checked, not just sub0 — bcm2835 exposes eight, and a stream can land on any
    of them. The pid is best-effort: read from inside the container it comes back as 0, because the
    holder lives in another PID namespace. `closed` versus a state block is the part that is always
    true, so the boolean is what callers should trust.
    """
    in_use = False
    pid: int | None = None
    for path in sorted(glob.glob(f"{PROC_ASOUND}/card{card}/pcm{device}p/sub*/status")):
        try:
            with open(path, encoding="utf-8") as f:
                status = f.read()
        except OSError:
            continue
        if status.strip() == "closed":
            continue
        in_use = True
        owner = re.search(r"owner_pid\s*:\s*(\d+)", status)
        if owner and int(owner.group(1)) > 0:
            pid = int(owner.group(1))
    return in_use, pid


# -- PortAudio bridge --------------------------------------------------------------------------


def _portaudio_outputs(*, force: bool = False) -> dict[tuple[int, int], int]:
    """Map (alsa_card, alsa_device) -> PortAudio index, for every output PortAudio publishes.

    PortAudio caches its device list at Pa_Initialize, so a card that appeared (a HAT fitted, a USB
    DAC plugged in) is invisible until it is re-initialised. Refreshing costs a few ms and makes a
    rescan mean what the user thinks it means.

    THE LOCK IS NOT OPTIONAL. Pa_Terminate/Pa_Initialize tear down and rebuild PortAudio's PROCESS-
    GLOBAL state; running one thread's terminate while another is enumerating segfaults the
    interpreter outright — no exception, no traceback, just SIGSEGV. That is not hypothetical: the
    GUI fetches the device list and the current output together (one Promise.all), Flask serves them
    on two threads, and the config API crash-looped on exactly that. Sequential calls never show it,
    which is why it survived a round of hardware testing.

    The TTL cache exists for the same request pair: two endpoints a few milliseconds apart do not
    each need to re-enumerate. `force` bypasses it for the player, which MUST re-read after closing
    its stream — a cached map taken while the card was still held would be missing that very card.
    """
    global _portaudio_cache
    if sd is None:
        return {}

    with _PORTAUDIO_LOCK:
        cached = _portaudio_cache
        if not force and cached is not None and (time.monotonic() - cached[0]) < PORTAUDIO_CACHE_S:
            return cached[1]

        try:
            sd._terminate()
            sd._initialize()
        except Exception:  # noqa: BLE001 - a refresh failure just means a staler list
            logger.debug("PortAudio re-initialise failed; using the cached device list", exc_info=True)

        found: dict[tuple[int, int], int] = {}
        try:
            devices = sd.query_devices()
        except Exception:  # noqa: BLE001
            logger.warning("PortAudio device query failed", exc_info=True)
            return {}
        for index, dev in enumerate(devices):
            if dev.get("max_output_channels", 0) < 1:
                continue
            match = PORTAUDIO_HW.search(dev.get("name", ""))
            if match:
                found[(int(match.group(1)), int(match.group(2)))] = index

        _portaudio_cache = (time.monotonic(), found)
        return found


# -- discovery ---------------------------------------------------------------------------------


def parse_aplay_output(output: str) -> list[AudioDevice]:
    """Pure parse of `aplay -l` text. Availability is filled in by list_output_devices()."""
    devices: list[AudioDevice] = []
    for match in APLAY_LINE.finditer(output):
        card = int(match.group(1))
        card_name = match.group(2)
        card_description = match.group(3)
        device = int(match.group(4))
        device_description = match.group(6)

        device_type = _identify(card_name, card_description, device_description)
        devices.append(
            AudioDevice(
                card=card,
                device=device,
                card_name=card_name,
                card_description=card_description,
                device_description=device_description,
                type=device_type,
                friendly_name=_friendly_name(device_type, card_name, card_description, device_description),
                is_available=True,  # decided in list_output_devices()
            )
        )
    return devices


def _run(cmd: list[str], timeout: int = 10) -> tuple[bool, str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.returncode == 0, (result.stdout if result.returncode == 0 else result.stderr).strip()
    except FileNotFoundError:
        return False, f"{cmd[0]} not found"
    except subprocess.TimeoutExpired:
        return False, "timed out"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def configured_output_spec(fallback: str | None = None) -> str | None:
    """The output this unit should render to: settings.json > PLUM_DAC_DEVICE > fallback.

    One definition of the precedence, shared by the config API (which needs to know which device to
    mark active) and the player (which needs to know what to open). Same never-raise contract as
    unit_identity.device_name(): the audio process must come up and play even if settings.json is
    missing, unreadable, or being rewritten by the other process mid-read.

    Env stays meaningful as the value a unit boots with before anyone has chosen an output — which
    is why settings.json ships with no device rather than a plausible-looking one.
    """
    try:
        with open(unit_identity.settings_path(), encoding="utf-8") as f:
            output = ((json.load(f).get("audio") or {}).get("output") or {}).get("device")
        if output and str(output).strip():
            return str(output).strip()
    except (OSError, ValueError):
        pass
    return (os.environ.get("PLUM_DAC_DEVICE") or "").strip() or fallback


async def watch_output_device(
    on_change: Callable[[str | None], Awaitable[None]],
    *,
    interval: float = WATCH_INTERVAL_S,
) -> None:
    """Poll settings.json and invoke `on_change(spec)` whenever the chosen output changes.

    Polling for the same reason unit_identity does: settings.json is a cross-process contract
    written by the config API, and a device switch is a deliberate user action, not something that
    needs sub-second latency. A failing callback is logged and the loop continues — a device that
    would not open must not also stop us noticing the user picking a different one.
    """
    current = configured_output_spec()
    while True:
        await asyncio.sleep(interval)
        latest = configured_output_spec()
        if latest == current:
            continue
        logger.info("output device changed: %r -> %r", current, latest)
        current = latest
        try:
            await on_change(latest)
        except Exception:  # noqa: BLE001
            logger.warning("applying the new output device failed; will retry on the next change", exc_info=True)


def list_output_devices(active_spec: str | None = None, *, force_refresh: bool = False) -> list[AudioDevice]:
    """Every ALSA playback device, annotated with whether it can actually be opened.

    `active_spec` is the output this unit is configured to render to (settings.json, or the
    PLUM_DAC_DEVICE it booted with). It is not a nicety: the player holds that card exclusively, so
    PortAudio cannot enumerate it and /proc may be unreadable — without being told, the one device
    guaranteed to work is the one most likely to be greyed out. Three independent signals, any of
    which is sufficient, because each fails in a different environment:

      is_active  — we are rendering to it; true even in a container with /proc/asound masked
      in_use     — something holds the PCM (needs /proc/asound; catches a third-party holder too)
      PortAudio  — it enumerated, so it is idle and openable right now
    """
    ok, output = _run(["aplay", "-l"])
    if not ok:
        # "no soundcards found" is a legitimate empty result, not a failure worth shouting about.
        if "no soundcards" not in output.lower():
            logger.error("aplay -l failed: %s", output)
        return []

    devices = parse_aplay_output(output)
    exposed = _portaudio_outputs(force=force_refresh)
    active = find_device(active_spec, devices) if active_spec else None

    for dev in devices:
        dev.portaudio_index = exposed.get((dev.card, dev.device))
        dev.in_use, dev.in_use_by_pid = _pcm_in_use(dev.card, dev.device)
        dev.is_active = active is not None and dev.id == active.id

        if dev.is_active or dev.in_use or dev.portaudio_index is not None:
            dev.is_available = True
        else:
            dev.is_available = False
            dev.unavailable_reason = (
                "PortAudio cannot open this device. For an HDMI output this normally means no " "display is attached."
            )

    logger.info("found %d playback device(s); %d selectable", len(devices), sum(d.is_available for d in devices))
    return devices


def find_device(spec: str, devices: list[AudioDevice] | None = None) -> AudioDevice | None:
    """Resolve a stored or configured device spec to a currently-present device.

    Accepts, in descending order of precision: our stable `<card_name>:<device>` id, an `hw:C,D`
    address, a bare ALSA card name, and finally a case-insensitive substring of either description.
    The loose tail is not sloppiness — it is what keeps the existing PLUM_DAC_DEVICE values working
    (`bcm2835`, `snd_rpi_hifiberry_dacplus` are PortAudio name fragments, not ALSA ids), so a unit
    that has never been through the GUI keeps the output it booted with.
    """
    if not spec:
        return None
    if devices is None:
        devices = list_output_devices()
    spec = spec.strip()

    for dev in devices:
        if spec == dev.id:
            return dev
    for dev in devices:
        if spec == dev.hw_id:
            return dev
    for dev in devices:
        if spec == dev.card_name:
            return dev

    lowered = spec.lower()
    matches = [
        dev for dev in devices if lowered in dev.card_description.lower() or lowered in dev.device_description.lower()
    ]
    if len(matches) == 1:
        return matches[0]
    if matches:
        # Two identical DACs and a substring that hits both: picking one at random would give the
        # user a working output today and a different room tomorrow. Refuse instead.
        logger.warning("device spec %r is ambiguous (%d matches) — ignoring", spec, len(matches))
    return None


def resolve_portaudio_index(spec: str) -> tuple[int | None, AudioDevice | None]:
    """(PortAudio index, device) for a spec — what the player needs to open a stream.

    A None index with a non-None device means the card is present but not openable right now; the
    caller should keep whatever it already has rather than tearing down a working output.

    Forces a fresh enumeration: the player calls this right after closing its stream, and a cached
    map taken while that card was still held would be missing the very device we are reopening.
    """
    devices = list_output_devices(active_spec=spec, force_refresh=True)
    dev = find_device(spec, devices)
    if dev is None:
        logger.warning("configured output %r is not present", spec)
        return None, None
    if dev.portaudio_index is None:
        logger.warning("output %r (%s) is present but PortAudio will not open it", spec, dev.hw_id)
    return dev.portaudio_index, dev


def test_device(spec: str, active_spec: str | None = None) -> tuple[bool, str]:
    """Play a one-shot tone on a device, to confirm the right speakers are wired to it.

    Only meaningful for a device we are NOT already rendering to: the player holds its output
    exclusively, so speaker-test against the active card returns EBUSY, which reads as "this device
    is broken" when it means the opposite. Say so plainly instead of running it.
    """
    devices = list_output_devices(active_spec=active_spec)
    dev = find_device(spec, devices)
    if dev is None:
        return False, f"Device {spec!r} not found"
    if dev.in_use or dev.is_active:
        return False, f"{dev.friendly_name} is in use — it is already this unit's output."
    if not dev.is_available:
        return False, dev.unavailable_reason or f"{dev.friendly_name} cannot be opened"

    ok, output = _run(["speaker-test", "-D", dev.hw_id, "-c", "2", "-t", "wav", "-l", "1"], timeout=15)
    return (True, f"Test tone sent to {dev.friendly_name}") if ok else (False, f"Test failed: {output}")


if __name__ == "__main__":  # manual probe: [PLUM_DAC_DEVICE=<spec>] python3 audio_devices.py
    logging.basicConfig(level=os.environ.get("PLUM_LOG_LEVEL", "INFO"), format="%(levelname)s %(message)s")
    for d in list_output_devices(active_spec=os.environ.get("PLUM_DAC_DEVICE")):
        flag = "  " if d.is_available else "! "
        held = f" [in use pid={d.in_use_by_pid or '?'}]" if d.in_use else ""
        pa = f" pa={d.portaudio_index}" if d.portaudio_index is not None else ""
        print(f"{flag}{'* ' if d.is_active else ''}{d.friendly_name}\n    id={d.id} {d.hw_id} {d.type.value}{pa}{held}")
        if d.unavailable_reason:
            print(f"    {d.unavailable_reason}")
