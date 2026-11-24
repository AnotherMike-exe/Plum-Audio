"""Unit tests for mesh/avahi.py: recovery from an Avahi daemon restart.

An EntryGroup and a ServiceBrowser are objects INSIDE the Avahi daemon. When the daemon restarts
(`systemctl restart avahi-daemon`, a package upgrade) they are gone, while our D-Bus connection
stays up — so nothing failed, the unit was simply no longer advertised and saw no speakers until
our own process restarted. D-Bus announces the restart as `NameOwnerChanged` for the Avahi name.

No bus: a fake stands in for dbus-next's MessageBus and proxies. Run: `pytest tests/Unit`.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend" / "scripts"))

from mesh import avahi  # noqa: E402


class FakeDaemon:
    """One Avahi daemon process. A restart is a NEW instance: its objects do not carry over."""

    def __init__(self, fail_publishes=0):
        self.groups = []  # committed (name, type, port) records
        self.browsers = []  # started browser types
        self.fail_publishes = fail_publishes  # the first N EntryGroupNew calls raise


class FakeIface:
    """Every interface at once: Server, Server2, EntryGroup, ServiceBrowser and org.freedesktop.DBus."""

    def __init__(self, bus, path):
        self.bus = bus
        self.path = path
        self.daemon = bus.daemon  # bound at creation: a proxy outlives its daemon
        self.handlers = {}

    def _alive(self):
        if self.daemon is not self.bus.daemon:
            raise RuntimeError("org.freedesktop.DBus.Error.ServiceUnknown")

    # Server / Server2
    async def call_entry_group_new(self):
        self._alive()
        if self.daemon.fail_publishes > 0:
            self.daemon.fail_publishes -= 1
            raise RuntimeError("daemon not ready")
        return f"/group/{len(self.daemon.groups)}"

    async def call_service_browser_prepare(self, _i, _p, service_type, _d, _f):
        self._alive()
        return f"/browser/{service_type}"

    async def call_resolve_service(self, *_a):
        self._alive()
        raise RuntimeError("not used")

    # EntryGroup
    async def call_add_service(self, _i, _p, _f, name, service_type, _d, _h, port, _txt):
        self._alive()
        self._pending = (name, service_type, port)

    async def call_commit(self):
        self._alive()
        self.daemon.groups.append(self._pending)

    async def call_reset(self):
        self._alive()
        self.daemon.groups.remove(self._pending)

    async def call_free(self):
        self._alive()

    # ServiceBrowser
    def on_item_new(self, cb):
        self.handlers["new"] = cb
        self.bus.item_new[self.path] = cb

    def on_item_remove(self, cb):
        self.handlers["remove"] = cb

    async def call_start(self):
        self._alive()
        self.daemon.browsers.append(self.path.rsplit("/", 1)[1])

    # org.freedesktop.DBus
    def on_name_owner_changed(self, cb):
        self.bus.owner_watchers.append(cb)


class FakeProxy:
    def __init__(self, bus, path):
        self._iface = FakeIface(bus, path)

    def get_interface(self, _name):
        return self._iface


class FakeBus:
    def __init__(self):
        self.daemon = FakeDaemon()
        self.owner_watchers = []
        self.item_new = {}
        self.disconnected = False

    async def introspect(self, bus_name, path):
        if bus_name == avahi.AVAHI_BUS and self.daemon is None:
            raise RuntimeError("org.freedesktop.DBus.Error.ServiceUnknown")
        return (bus_name, path)

    def get_proxy_object(self, _bus_name, path, _intro):
        return FakeProxy(self, path)

    def disconnect(self):
        self.disconnected = True

    def restart_daemon(self, **kw):
        """Stop the daemon and start a new one, announcing both as D-Bus does."""
        self.daemon = None
        for cb in self.owner_watchers:
            cb(avahi.AVAHI_BUS, ":1.10", "")
        self.daemon = FakeDaemon(**kw)
        for cb in self.owner_watchers:
            cb(avahi.AVAHI_BUS, "", ":1.11")


def make_client(monkeypatch):
    monkeypatch.setattr(avahi, "RESTORE_BACKOFF_S", (0.0, 0.01, 0.01))
    client = avahi.AvahiClient()
    client._bus = FakeBus()
    return client, client._bus


async def settle():
    for _ in range(20):
        await asyncio.sleep(0.005)


def test_a_daemon_restart_re_registers_the_record_and_the_browsers(monkeypatch):
    async def scenario():
        client, bus = make_client(monkeypatch)
        assert await client.publish("unit-a", avahi.SERVER_SERVICE, 8927, {"path": "/sendspin"})
        assert await client.browse(avahi.CLIENT_SERVICE, lambda _s: None)
        assert await client.browse(avahi.SERVER_SERVICE, lambda _s: None)
        assert bus.daemon.groups == [("unit-a", avahi.SERVER_SERVICE, 8927)]

        bus.restart_daemon()
        assert bus.daemon.groups == []  # the new daemon knows nothing about us
        await settle()

        assert bus.daemon.groups == [("unit-a", avahi.SERVER_SERVICE, 8927)]
        assert sorted(bus.daemon.browsers) == sorted([avahi.CLIENT_SERVICE, avahi.SERVER_SERVICE])
        assert len(client._groups) == 1 and len(client._browsers) == 2  # no stale proxies kept

    asyncio.run(scenario())


def test_the_restore_retries_while_the_new_daemon_is_not_ready(monkeypatch):
    async def scenario():
        client, bus = make_client(monkeypatch)
        await client.publish("unit-a", avahi.SERVER_SERVICE, 8927, {})
        await client.browse(avahi.CLIENT_SERVICE, lambda _s: None)

        bus.restart_daemon(fail_publishes=1)
        await settle()

        assert bus.daemon.groups == [("unit-a", avahi.SERVER_SERVICE, 8927)]  # once, not twice
        assert bus.daemon.browsers.count(avahi.CLIENT_SERVICE) >= 1
        assert len(client._browsers) == 1

    asyncio.run(scenario())


def test_a_rename_before_the_restart_restores_the_new_name_only(monkeypatch):
    async def scenario():
        client, bus = make_client(monkeypatch)
        await client.publish("unit-a", avahi.SERVER_SERVICE, 8927, {"name": "Old"})
        await client.republish("unit-a", avahi.SERVER_SERVICE, 8927, {"name": "New"})
        assert client._published == [("unit-a", avahi.SERVER_SERVICE, 8927, {"name": "New"})]

        bus.restart_daemon()
        await settle()

        assert bus.daemon.groups == [("unit-a", avahi.SERVER_SERVICE, 8927)]

    asyncio.run(scenario())


def test_a_restart_flushes_the_services_seen_before_it(monkeypatch):
    """A speaker that left while the daemon was down sends no remove, so the list must be rebuilt."""

    async def scenario():
        client, bus = make_client(monkeypatch)
        removed = []
        await client.browse(avahi.CLIENT_SERVICE, lambda _s: None, removed.append)
        key = f"{avahi.CLIENT_SERVICE}/esp32"
        client._seen[key] = avahi.DiscoveredService("esp32", avahi.CLIENT_SERVICE, 8928)

        bus.restart_daemon()
        await settle()

        assert removed == [key]
        assert client._seen == {}

    asyncio.run(scenario())


def test_another_name_changing_owner_is_ignored(monkeypatch):
    async def scenario():
        client, bus = make_client(monkeypatch)
        await client.publish("unit-a", avahi.SERVER_SERVICE, 8927, {})
        groups = list(client._groups)
        for cb in bus.owner_watchers:
            cb("org.bluez", ":1.5", "")
            cb("org.bluez", "", ":1.6")
        await settle()
        assert client._groups == groups
        assert bus.daemon.groups == [("unit-a", avahi.SERVER_SERVICE, 8927)]

    asyncio.run(scenario())


def test_a_daemon_that_starts_after_us_gets_the_record(monkeypatch):
    """Avahi down at boot used to mean no advert until our process restarted."""

    async def scenario():
        client, bus = make_client(monkeypatch)
        bus.daemon = None
        assert await client.publish("unit-a", avahi.SERVER_SERVICE, 8927, {}) is False

        bus.daemon = FakeDaemon()
        for cb in bus.owner_watchers:
            cb(avahi.AVAHI_BUS, "", ":1.11")
        await settle()

        assert bus.daemon.groups == [("unit-a", avahi.SERVER_SERVICE, 8927)]

    asyncio.run(scenario())


def test_nothing_is_restored_after_close(monkeypatch):
    async def scenario():
        client, bus = make_client(monkeypatch)
        await client.publish("unit-a", avahi.SERVER_SERVICE, 8927, {})
        watchers = list(bus.owner_watchers)
        await client.close()
        bus.daemon = FakeDaemon()
        for cb in watchers:
            cb(avahi.AVAHI_BUS, "", ":1.11")
        await settle()
        assert bus.daemon.groups == []

    asyncio.run(scenario())
