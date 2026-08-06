#!/usr/bin/env python3
"""
Plum-Audio — the BlueZ pairing agent (org.bluez.Agent1).

!! THIS MODULE MUST NOT USE `from __future__ import annotations` !!

dbus-next derives each method's D-Bus signature from its parameter annotations, which are the
single-character type codes below ("o" = object path, "s" = string, "u" = uint32, "q" = uint16).
PEP 563 stringifies annotations, so with the future import active `device: "o"` is stored as the
five-character string `'o'` — quotes included — and dbus-next builds a malformed signature that
BlueZ rejects. Every other module in this package uses the future import; this one deliberately
cannot, which is why the agent lives here instead of in bluetooth_adapter.py.

Behaviourally this is Plum-Snapcast's bluetooth-auto-pair-agent.py, minus dbus-python and the GLib
main loop it needed a thread for — as an exported ServiceInterface it just lives in the audio loop.

Security posture is unchanged and deliberate: we accept every pairing request, which is how a
commercial Bluetooth speaker behaves. The gate is the `discoverable` toggle plus physical radio
range, not a pairing code. Modern phones (iOS 8+, Android 6+) only do SSP numeric comparison
anyway — forcing a legacy PIN makes pairing fail outright on them, so RequestConfirmation and
RequestAuthorization simply return. The PIN/passkey methods exist for genuinely legacy devices.
"""

import logging

from dbus_next.service import ServiceInterface, method

logger = logging.getLogger("plum.bluetooth_agent")

AGENT_IFACE = "org.bluez.Agent1"


class AutoPairAgent(ServiceInterface):
    """A BlueZ Agent1 that accepts everything."""

    def __init__(self, instance_id: str) -> None:
        super().__init__(AGENT_IFACE)
        self._id = instance_id

    @method()
    def Release(self) -> None:
        logger.info("[bluetooth-%s] pairing agent released by BlueZ", self._id)

    @method()
    def AuthorizeService(self, device: "o", uuid: "s") -> None:  # noqa: F821 - dbus type codes
        logger.info("[bluetooth-%s] authorising service %s for %s", self._id, uuid, device)

    @method()
    def RequestPinCode(self, device: "o") -> "s":  # noqa: F821
        logger.info("[bluetooth-%s] legacy PIN requested by %s", self._id, device)
        return "0000"

    @method()
    def RequestPasskey(self, device: "o") -> "u":  # noqa: F821
        logger.info("[bluetooth-%s] legacy passkey requested by %s", self._id, device)
        return 0

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q") -> None:  # noqa: F821
        logger.info("[bluetooth-%s] passkey %06d for %s (entered=%d)", self._id, passkey, device, entered)

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s") -> None:  # noqa: F821
        logger.info("[bluetooth-%s] pin %s for %s", self._id, pincode, device)

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u") -> None:  # noqa: F821
        logger.info("[bluetooth-%s] auto-confirming pairing with %s (passkey %06d)", self._id, device, passkey)

    @method()
    def RequestAuthorization(self, device: "o") -> None:  # noqa: F821
        logger.info("[bluetooth-%s] auto-authorising %s", self._id, device)

    @method()
    def Cancel(self) -> None:
        logger.info("[bluetooth-%s] pairing cancelled", self._id)
