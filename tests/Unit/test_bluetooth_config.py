"""Unit tests for Bluetooth endpoint resolution, capture argv, and the agent's D-Bus signatures.

Pure-logic, no radio required. Two of these guard failure modes that are SILENT on hardware:

  * the pairing agent's D-Bus signatures. dbus-next derives them from parameter annotations, so
    adding `from __future__ import annotations` to bluetooth_agent.py turns "o" into "'o'" and
    BlueZ rejects the agent — with no import error and no traceback, just pairing that never works.
  * the arecord device string. The commas in DEV=..,PROFILE=.. must stay inside one argv token or
    ALSA misparses the slave definition, which surfaces as "audio open error" long after startup.

Run: `pytest tests/Unit`.
"""

import ast
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "scripts"))

from sources import bluetooth_config as bc  # noqa: E402

# dbus-next is a real runtime dependency (backend/requirements.txt), but a bare checkout on a dev
# laptop may not have it. Skip ONLY the two tests that genuinely need it — a module-level
# importorskip would skip this whole file, silently taking the future-import guard with it, and
# that guard is the single most valuable test here precisely because it needs no dependencies.
try:
    import dbus_next  # noqa: F401

    HAVE_DBUS_NEXT = True
except ImportError:
    HAVE_DBUS_NEXT = False

needs_dbus_next = pytest.mark.skipif(HAVE_DBUS_NEXT is False, reason="dbus-next not installed")


def _settings(endpoints, **section):
    return {"integrations": {"bluetooth": {**section, "endpoints": endpoints}}}


# -- endpoint filtering ------------------------------------------------------------------------


def test_enabled_endpoints_drops_disabled_and_caps():
    eps = [{"id": str(i), "enabled": i % 2 == 0} for i in range(1, 12)]
    enabled = bc.enabled_endpoints(_settings(eps))
    assert all(e["enabled"] for e in enabled)
    assert len(enabled) <= bc.MAX_ENDPOINTS
    assert enabled == [e for e in eps[: bc.MAX_ENDPOINTS] if e.get("enabled")]


def test_enabled_endpoints_missing_section_is_empty():
    assert bc.enabled_endpoints({}) == []
    assert bc.enabled_endpoints({"integrations": {"bluetooth": None}}) == []


# -- instance derivation -----------------------------------------------------------------------


def test_instance_derives_paths_from_id():
    (inst,) = bc.instances_from_settings(
        _settings([{"id": "1", "enabled": True, "deviceName": "Kitchen", "adapter": "hci0"}])
    )
    assert inst.source_id == "bluetooth-1"
    assert inst.adapter_path == "/org/bluez/hci0"
    assert inst.fifo_path == "/tmp/bluetooth-1-fifo"
    assert inst.agent_path == "/plum/audio/agent/1"
    assert inst.device_name == "Kitchen"


def test_adapter_falls_back_to_derived_when_absent():
    """An endpoint written by an older build has no `adapter` key; it must still land on a radio."""
    (inst,) = bc.instances_from_settings(_settings([{"id": "2", "enabled": True}]))
    assert inst.adapter == "hci1"  # id 2 -> hci1
    assert bc.adapter_for("1") == "hci0"
    assert bc.adapter_for("bogus") == bc.DEFAULT_ADAPTER


def test_agent_paths_are_unique_per_instance():
    """Two adapters must not collide on the bus — that would silently unregister the first agent."""
    insts = bc.instances_from_settings(
        _settings([
            {"id": "1", "enabled": True, "adapter": "hci0"},
            {"id": "2", "enabled": True, "adapter": "hci1"},
        ])
    )
    assert len({i.agent_path for i in insts}) == 2
    assert len({i.fifo_path for i in insts}) == 2
    assert len({i.source_id for i in insts}) == 2


# -- section-level toggles ---------------------------------------------------------------------


def test_section_toggles_ride_on_every_instance():
    """autoPair/discoverable are section-level in settings but the ADAPTER applies them, so each
    instance has to carry them."""
    (inst,) = bc.instances_from_settings(
        _settings([{"id": "1", "enabled": True}], autoPair=False, discoverable=False)
    )
    assert inst.auto_pair is False
    assert inst.discoverable is False


def test_section_toggles_default_to_on():
    (inst,) = bc.instances_from_settings(_settings([{"id": "1", "enabled": True}]))
    assert inst.auto_pair is True
    assert inst.discoverable is True


# -- capture argv ------------------------------------------------------------------------------


@needs_dbus_next
def test_capture_argv_keeps_slave_definition_in_one_token():
    from sources.bluetooth_adapter import BluetoothAdapter

    (inst,) = bc.instances_from_settings(_settings([{"id": "1", "enabled": True}]))
    argv = BluetoothAdapter(inst)._capture_argv("AA:BB:CC:DD:EE:FF")

    device = argv[argv.index("-D") + 1]
    # The commas must not split across argv entries, or ALSA parses PROFILE as a separate node.
    assert device.count(",") == 1
    assert "DEV=AA:BB:CC:DD:EE:FF" in device and "PROFILE=a2dp" in device
    assert device.startswith("plug:{SLAVE=") and device.endswith("}")
    # -f cd is what makes the stream match DEFAULT_FORMAT (44100/16/2).
    assert argv[argv.index("-f") + 1] == "cd"
    assert argv[-1] == inst.fifo_path


# -- the PEP 563 trap --------------------------------------------------------------------------


def test_agent_module_has_no_future_annotations_import():
    """`from __future__ import annotations` would stringify the dbus type codes and break pairing.

    Parsed rather than grepped: the module's own docstring names the import to warn about it.
    """
    tree = ast.parse((REPO / "backend/scripts/sources/bluetooth_agent.py").read_text())
    offenders = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
    ]
    assert not offenders, "bluetooth_agent.py must not use `from __future__ import annotations`"


@needs_dbus_next
def test_agent_dbus_signatures_are_bare_type_codes():
    """The signatures BlueZ actually receives, computed by dbus-next from the annotations."""
    from dbus_next.service import ServiceInterface

    from sources.bluetooth_agent import AutoPairAgent

    agent = AutoPairAgent("1")
    signatures = {m.name: (m.in_signature, m.out_signature) for m in ServiceInterface._get_methods(agent)}

    assert signatures["AuthorizeService"] == ("os", "")
    assert signatures["RequestConfirmation"] == ("ou", "")
    assert signatures["RequestAuthorization"] == ("o", "")
    assert signatures["RequestPinCode"] == ("o", "s")
    assert signatures["RequestPasskey"] == ("o", "u")
    assert signatures["DisplayPasskey"] == ("ouq", "")
    assert signatures["DisplayPinCode"] == ("os", "")
    assert signatures["Cancel"] == ("", "")
    # Belt and braces: a quote anywhere means PEP 563 stringified them.
    assert not any("'" in sig for pair in signatures.values() for sig in pair)
