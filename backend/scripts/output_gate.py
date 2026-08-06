#!/usr/bin/env python3
"""Decide, once per container start, whether this unit runs a player process at all.

A unit with no output does not run a player with nothing open — it runs NO PLAYER. That is not a
stylistic choice, it is forced by the startup order: `SendspinPlayer.start()` calls
`renderer.start()` before it opens the listener and before it publishes `_sendspin._tcp`, and
`AlsaRenderer.start()` raises when PortAudio cannot open a device. So on a host with no sound card
the player dies before it exists, and supervisord — autostart, autorestart, startsecs=3 — restarts
it forever. There is no "run silently" state to fall back to.

Deciding here, before supervisord, is also precisely WHY changing the setting needs a container
restart: by the time anything else is running, the answer has already been used to compose the
process tree. The GUI is told this plainly rather than being left to look broken.

The output is one word on stdout, `none` or `device`, consumed by entrypoint.sh. Everything else is
logged to stderr. It NEVER raises: a gate that fails takes the unit's audio with it, so any
unexpected error falls open to `device`, which is the behaviour every existing unit already has.

Run by hand to see what a unit would decide, without changing anything:

    docker exec plum-audio python3 /app/scripts/output_gate.py --dry-run
"""

from __future__ import annotations

import logging
import os
import sys

_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPTS)
# apis/ has no __init__.py, so `import apis.settings_api` resolves as a NAMESPACE package and
# produces a SECOND module object with its own SETTINGS_FILE and its own _SETTINGS_LOCK — distinct
# from the `settings_api` every other importer gets. Put apis/ on the path and import it flat, the
# way audio_api.py does, so there is one module and one lock.
sys.path.insert(0, os.path.join(_SCRIPTS, "apis"))

import audio_devices  # noqa: E402
import unit_identity  # noqa: E402
from player_state import save_active_output, state_file_path  # noqa: E402

logger = logging.getLogger("output_gate")

MODE_NONE = "none"
MODE_DEVICE = "device"


def decide(
    *, settings_spec: str | None, stored_spec: str | None, player_enabled: bool, hardware_present: bool
) -> tuple[str, str, bool]:
    """(mode, why, auto_selected) — pure, so the precedence is testable without a filesystem.

    Precedence, highest first:
      1. PLUM_PLAYER_ENABLED=0 — the operator's container-level override. deploy.sh writes it for a
         unit whose units.conf row says `none`, so a headless box is correct on its very first boot,
         before any settings.json exists.
      2. The sentinel in settings.json — the user's own choice in Settings -> Audio.
      3. No output hardware at all. Auto-selected ONLY if nothing is STORED — `stored_spec` is
         settings.json alone, so a PLUM_DAC_DEVICE the unit merely booted with does not count as a
         choice, while a device picked in Settings does.
      4. Otherwise a device, which is every unit that exists today.
    """
    if not player_enabled:
        return MODE_NONE, "PLUM_PLAYER_ENABLED=0", False
    if audio_devices.is_no_output(settings_spec):
        return MODE_NONE, "the configured output is 'No output'", False
    if not hardware_present:
        # Auto-select ONLY when nothing was deliberately chosen. A unit whose user picked a real
        # device and whose card is merely LATE is the common case, not the rare one: an I2S HAT
        # registers asynchronously (overlay probe + i2c), which is the very mechanism that makes
        # card numbers move on these units. Persisting `none` there would overwrite that choice
        # permanently, leave the unit with no player, and never re-check.
        #
        # We still answer `none` — a player cannot open a card that is not there, and crash-looping
        # is worse — but with auto_selected False, so nothing is written and the next container
        # start with the card present comes back on its own.
        if stored_spec and not audio_devices.is_no_output(stored_spec):
            return MODE_NONE, f"no playback hardware YET; keeping the stored choice {stored_spec!r}", False
        return MODE_NONE, "this host has no playback hardware", True
    return MODE_DEVICE, f"configured output {settings_spec!r}", False


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [output_gate] %(message)s", stream=sys.stderr)
    dry_run = "--dry-run" in (argv if argv is not None else sys.argv[1:])

    try:
        spec = audio_devices.configured_output_spec()
        mode, why, auto_selected = decide(
            settings_spec=spec,
            stored_spec=audio_devices.stored_output_spec(),
            player_enabled=os.environ.get("PLUM_PLAYER_ENABLED", "1") != "0",
            hardware_present=audio_devices.has_output_hardware(),
        )
    except Exception:  # noqa: BLE001 - a gate that fails must not take the unit's audio with it
        logger.warning("could not decide the output mode; assuming a player is wanted", exc_info=True)
        print(MODE_DEVICE)
        return 0

    logger.info("output mode: %s — %s", mode, why)

    if mode == MODE_NONE and not dry_run:
        try:
            # Write the echo the player would have written. Everything downstream — `pending`,
            # `restart_required`, the GUI banner — is defined as "the choice versus what the player
            # reports it has open", and on a playerless unit nobody else will ever report. Without
            # this the API reads as "the player has not answered yet", forever.
            #
            # _merge_write is read-modify-write, so a persisted volume survives: coming back to a
            # real device must not silently reset the unit to 100%.
            save_active_output(state_file_path(), audio_devices.NO_OUTPUT)
        except Exception:  # noqa: BLE001
            logger.warning("could not write the no-output echo", exc_info=True)

        if auto_selected:
            try:
                # Persist what we picked FOR the user, so Settings -> Audio shows "No output" as the
                # active choice rather than a phantom "Not present (bcm2835)" plus a restart banner
                # that can never be satisfied. Safe here and nowhere else: nothing else is running
                # yet, so the cross-process contract on settings.json is uncontended.
                #
                # Explicit path, not SettingsManager()'s default: that default is bound at module
                # IMPORT time, so it can disagree with unit_identity.settings_path() — which is what
                # configured_output_spec() read three lines above to make this very decision.
                from settings_api import SettingsManager

                SettingsManager(unit_identity.settings_path()).update_settings(
                    {
                        "audio": {
                            "output": {
                                "device": audio_devices.NO_OUTPUT,
                                "device_type": audio_devices.DeviceType.NONE.value,
                            }
                        }
                    }
                )
                logger.info("persisted the auto-selected 'No output' — no playback hardware to choose from")
            except Exception:  # noqa: BLE001
                logger.warning("could not persist the auto-selected output", exc_info=True)

    print(mode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
