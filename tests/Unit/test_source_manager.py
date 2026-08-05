"""Unit tests for SourceManagerBase reconcile — the live add/rename/disable/remove state machine.

The manager is what makes GUI endpoint edits apply without a restart, and its rules are subtle: a
source is keyed on the endpoint id and survives an edit, only the daemons respool; the source comes
up BEFORE its daemons (so the feeder's FIFO exists first); a launch that never took must retry; one
bad endpoint must not stall the others. All of that was previously proven only by a throwaway
smoke script against a fake server. This pins it.

We subclass SourceManagerBase and replace the real process layer (_spawn_daemons/_kill_daemons)
with pure bookkeeping, so `_reconcile` runs its real logic with no subprocesses. Run: `pytest tests/Unit`.
"""

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "scripts"))

from sources.source_manager import SourceManagerBase  # noqa: E402


@dataclass
class FakeInstance:
    instance_id: str
    device_name: str

    @property
    def source_id(self) -> str:
        return f"fake-{self.instance_id}"


class FakeProc:
    """Stand-in for asyncio.subprocess.Process — only `returncode` is read by the reconcile."""

    def __init__(self, returncode=None):
        self.returncode = returncode


class FakeServer:
    """Stand-in for PlumSendspinServer — the reconcile reads `sources` to spot a vanished source."""

    def __init__(self):
        self.sources = {}


class FakeManager(SourceManagerBase):
    """A SourceManagerBase whose sources and daemons are just recorded events."""

    def __init__(self, endpoints):
        super().__init__(server=FakeServer(), name="fake", settings_file="/does/not/exist")
        self._endpoints = endpoints  # list of {id, name, enabled}
        self.events = []  # ordered log of everything that happened
        self.render_count = 0

    def set_endpoints(self, endpoints):
        self._endpoints = endpoints

    # -- override the settings source + subclass hooks --------------------------------------

    def _read_settings(self):
        return {"_eps": self._endpoints}

    def desired(self, settings):
        return {
            e["id"]: ((e["name"],), FakeInstance(e["id"], e["name"]))
            for e in settings["_eps"]
            if e.get("enabled")
        }

    def render(self, settings):
        self.render_count += 1

    def daemons(self, instance):
        return []  # unused: the process layer is overridden below

    async def start_source(self, instance):
        self.events.append(("source_up", instance.instance_id))
        self.server.sources[instance.source_id] = object()

    async def stop_source(self, instance):
        self.events.append(("source_down", instance.instance_id))
        self.server.sources.pop(instance.source_id, None)

    async def update_source(self, instance):
        self.events.append(("source_rename", instance.instance_id, instance.device_name))

    # -- override the real process layer with bookkeeping -----------------------------------

    async def _spawn_daemons(self, running):
        self.events.append(("daemon_spawn", running.instance.instance_id))
        running.procs = [FakeProc(returncode=None)]  # pretend one live daemon

    async def _kill_daemons(self, running):
        self.events.append(("daemon_kill", running.instance.instance_id))
        running.procs = []


def reconcile(mgr):
    asyncio.run(mgr._reconcile())


# -- tests -------------------------------------------------------------------------------------


def test_start_brings_source_up_before_its_daemons():
    mgr = FakeManager([{"id": "1", "name": "Kitchen", "enabled": True}])
    reconcile(mgr)
    # The source FIFO must exist before the daemon opens it, so source_up strictly precedes spawn.
    assert mgr.events == [("source_up", "1"), ("daemon_spawn", "1")]
    assert set(mgr._running) == {"1"}


def test_disable_tears_down_daemons_before_the_source():
    mgr = FakeManager([{"id": "1", "name": "Kitchen", "enabled": True}])
    reconcile(mgr)
    mgr.events.clear()
    mgr.set_endpoints([{"id": "1", "name": "Kitchen", "enabled": False}])
    reconcile(mgr)
    assert mgr.events == [("daemon_kill", "1"), ("source_down", "1")]
    assert mgr._running == {}


def test_remove_is_the_same_teardown_as_disable():
    mgr = FakeManager([{"id": "1", "name": "K", "enabled": True}])
    reconcile(mgr)
    mgr.events.clear()
    mgr.set_endpoints([])  # endpoint gone entirely
    reconcile(mgr)
    assert mgr.events == [("daemon_kill", "1"), ("source_down", "1")]


def test_rename_respools_the_daemon_but_keeps_the_source():
    mgr = FakeManager([{"id": "1", "name": "Kitchen", "enabled": True}])
    reconcile(mgr)
    mgr.events.clear()
    mgr.set_endpoints([{"id": "1", "name": "Den", "enabled": True}])
    reconcile(mgr)
    # The Sendspin source is keyed on the id and survives; only its label follows and the daemon
    # respools. Crucially NO source_down/source_up — that would drop routed players.
    assert mgr.events == [
        ("source_rename", "1", "Den"),
        ("daemon_kill", "1"),
        ("daemon_spawn", "1"),
    ]
    assert set(mgr._running) == {"1"}


def test_dead_daemon_is_respawned():
    mgr = FakeManager([{"id": "1", "name": "K", "enabled": True}])
    reconcile(mgr)
    mgr.events.clear()
    mgr._running["1"].procs = [FakeProc(returncode=1)]  # the daemon exited
    reconcile(mgr)  # same settings, so no rename — just a respawn of the dead set
    assert mgr.events == [("daemon_kill", "1"), ("daemon_spawn", "1")]


def test_empty_proc_set_counts_as_needing_a_respawn():
    # A launch that never took (missing binary) leaves procs empty; the endpoint must not sit dead.
    mgr = FakeManager([{"id": "1", "name": "K", "enabled": True}])
    reconcile(mgr)
    mgr.events.clear()
    mgr._running["1"].procs = []  # nothing running
    reconcile(mgr)
    assert ("daemon_spawn", "1") in mgr.events


def test_render_runs_only_when_the_desired_set_changes():
    mgr = FakeManager([{"id": "1", "name": "K", "enabled": True}])
    reconcile(mgr)
    assert mgr.render_count == 1
    reconcile(mgr)  # identical settings
    assert mgr.render_count == 1, "render should not fire when nothing changed"
    mgr.set_endpoints([{"id": "1", "name": "K2", "enabled": True}])  # a real change
    reconcile(mgr)
    assert mgr.render_count == 2


def test_one_broken_endpoint_does_not_stall_the_others():
    mgr = FakeManager([
        {"id": "1", "name": "Good", "enabled": True},
        {"id": "bad", "name": "Boom", "enabled": True},
    ])
    # Make just the bad endpoint blow up when its source is brought up.
    orig_start = mgr.start_source

    async def start_source(instance):
        if instance.instance_id == "bad":
            raise RuntimeError("boom")
        await orig_start(instance)

    mgr.start_source = start_source
    reconcile(mgr)
    # The good endpoint still came all the way up despite its neighbour throwing.
    assert ("source_up", "1") in mgr.events
    assert ("daemon_spawn", "1") in mgr.events
    assert "1" in mgr._running


def test_an_endpoint_whose_source_failed_to_start_is_retried():
    """A start_source that raises must not leave the endpoint permanently half-up.

    Registering in _running BEFORE awaiting start_source meant a failure left an entry with
    procs == [], which every later cycle routed to _reap_and_respawn — and that retries DAEMONS,
    never the source. The endpoint sat with a daemon feeding a FIFO nothing read, logged at DEBUG.
    """
    mgr = FakeManager([{"id": "1", "name": "K", "enabled": True}])
    orig_start = mgr.start_source
    fail = {"now": True}

    async def start_source(instance):
        if fail["now"]:
            raise RuntimeError("boom")
        await orig_start(instance)

    mgr.start_source = start_source

    reconcile(mgr)
    assert "1" not in mgr._running, "a failed start must not be recorded as running"

    fail["now"] = False
    reconcile(mgr)  # the next cycle must genuinely retry the source, not just the daemons
    assert ("source_up", "1") in mgr.events
    assert ("daemon_spawn", "1") in mgr.events
    assert "1" in mgr._running


def test_a_vanished_sendspin_source_is_rebuilt():
    """POST /api/mesh/source/stop pops the source handle without telling the manager.

    The daemons stay healthy, so a reconcile that only inspects running.procs sees nothing wrong —
    and the endpoint is dead until the container restarts, with no error anywhere.
    """
    mgr = FakeManager([{"id": "1", "name": "K", "enabled": True}])
    reconcile(mgr)
    mgr.events.clear()

    mgr.server.sources.pop("fake-1")  # what /api/mesh/source/stop does
    assert mgr._running["1"].procs, "precondition: the daemons are still healthy"

    reconcile(mgr)
    assert ("source_up", "1") in mgr.events, "the Sendspin source must be rebuilt"
    # ...and the daemons re-pointed at the fresh FIFO, not left writing into the old one.
    assert ("daemon_kill", "1") in mgr.events
    assert ("daemon_spawn", "1") in mgr.events
