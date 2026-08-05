#!/usr/bin/env python3
"""
Plum-Audio mesh — mDNS publish/browse through the SYSTEM Avahi (D-Bus), not our own responder.

Sendspin discovery is mDNS, and the spec defines two service types in opposite directions:

  _sendspin._tcp         port 8928, TXT path=/sendspin [+ name]  — CLIENTS advertise; servers dial
  _sendspin-server._tcp  port 8927, TXT path=/sendspin [+ name]  — SERVERS advertise; clients dial

The spec is emphatic that a client picks one direction: "Do not manually connect to servers if you
are advertising _sendspin._tcp", and "Do not advertise _sendspin._tcp if the client plans to
initiate the connection."

Why this module exists: aiosendspin does its own mDNS via python-zeroconf, which binds UDP 5353 —
and the host Avahi already owns that port. Avahi is not optional for us: it is what advertises
AirPlay (shairport-sync) and Spotify Connect (go-librespot's avahi zeroconf backend). So we run the
Sendspin server/player with their built-in mDNS OFF and register the very same records through
Avahi over D-Bus instead. Same wire result, one responder on the host. (go-librespot solves it
exactly this way; so does every well-behaved daemon on a Linux box.)

This is what makes us a peer on a standard network rather than a closed mesh: third-party servers
(Music Assistant et al.) can discover our players, third-party clients can discover our servers, and
we can see both. NOTE mDNS is link-local — discovery only spans the L2 segment a unit sits on.

No Avahi (a dev laptop, a container without the socket)? Every call degrades to a warning + no-op,
so the audio path never depends on discovery being available.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import socket
from collections.abc import Callable
from dataclasses import dataclass, field

from dbus_next.aio import MessageBus
from dbus_next.constants import BusType

logger = logging.getLogger("plum.mesh.avahi")

AVAHI_BUS = "org.freedesktop.Avahi"
AVAHI_PATH = "/"
SERVER_IFACE = "org.freedesktop.Avahi.Server"
SERVER2_IFACE = "org.freedesktop.Avahi.Server2"  # adds *Prepare: create a browser, THEN start it
ENTRY_GROUP_IFACE = "org.freedesktop.Avahi.EntryGroup"
BROWSER_IFACE = "org.freedesktop.Avahi.ServiceBrowser"

IF_UNSPEC = -1  # AVAHI_IF_UNSPEC — all interfaces
PROTO_UNSPEC = -1  # AVAHI_PROTO_UNSPEC — v4 and v6

# The two Sendspin service types (spec §Discovery).
CLIENT_SERVICE = "_sendspin._tcp"
SERVER_SERVICE = "_sendspin-server._tcp"
DEFAULT_PATH = "/sendspin"


def local_ip() -> str | None:
    """This host's primary LAN address (no traffic sent — just asks the routing table)."""
    with contextlib.suppress(OSError):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 53))
            return s.getsockname()[0]
    return None


def _rank(addr: str, prefer_subnet_of: str | None) -> tuple:
    """Sort key for candidate addresses: lower is better.

    Avahi resolves a service once PER INTERFACE AND FAMILY, so one player comes back as loopback,
    link-local v6, docker0 AND the real LAN address. Dialing the wrong one is not hypothetical —
    a docker0-only advert is exactly what made spotifyd unreachable earlier in this project. So:
    an address on our own subnet wins, then other routable IPv4, then IPv6; loopback and
    link-local are last resorts.
    """
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return (9, addr)
    if ip.is_loopback:
        return (7, addr)
    if ip.is_link_local:
        return (8, addr)
    if ip.version == 4 and prefer_subnet_of:
        with contextlib.suppress(ValueError):
            mine = ipaddress.ip_address(prefer_subnet_of)
            if ipaddress.ip_network(f"{mine}/24", strict=False).supernet_of(
                ipaddress.ip_network(f"{ip}/32")
            ):
                return (0, addr)
    return (1 if ip.version == 4 else 2, addr)


@dataclass
class DiscoveredService:
    """One resolved mDNS service instance (all the addresses Avahi gave us for it)."""

    name: str  # instance name, e.g. "player-113"
    service_type: str
    port: int
    addresses: list[str] = field(default_factory=list)
    txt: dict[str, str] = field(default_factory=dict)
    _prefer_subnet_of: str | None = None

    @property
    def host(self) -> str:
        """The address to actually dial."""
        return sorted(self.addresses, key=lambda a: _rank(a, self._prefer_subnet_of))[0] if self.addresses else ""

    @property
    def path(self) -> str:
        return self.txt.get("path", DEFAULT_PATH)

    @property
    def friendly_name(self) -> str:
        return self.txt.get("name", self.name)

    @property
    def ws_url(self) -> str:
        return f"ws://{self.host}:{self.port}{self.path}"

    @property
    def key(self) -> str:
        return f"{self.service_type}/{self.name}"


def _txt_to_bytes(txt: dict[str, str]) -> list[bytes]:
    """Avahi wants TXT as a list of raw `key=value` byte strings."""
    return [f"{k}={v}".encode() for k, v in txt.items()]


def _txt_from_bytes(raw: list[bytes]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in raw:
        try:
            entry = bytes(item).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - never let a malformed TXT kill discovery
            continue
        key, _, value = entry.partition("=")
        if key:
            out[key] = value
    return out


class AvahiClient:
    """Shared system-bus connection to Avahi. Degrades to a no-op when unavailable."""

    def __init__(self) -> None:
        self._bus: MessageBus | None = None
        self._server = None
        self._server2 = None  # Server2 if the daemon has it (Avahi >= 0.7)
        self._groups: list = []  # published EntryGroup proxies (kept alive = record stays up)
        self._browsers: list = []
        self._seen: dict[str, DiscoveredService] = {}  # key -> service (merged across interfaces)
        self._local_ip = local_ip()

    async def connect(self) -> bool:
        if self._server is not None:
            return True
        try:
            self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            intro = await self._bus.introspect(AVAHI_BUS, AVAHI_PATH)
            obj = self._bus.get_proxy_object(AVAHI_BUS, AVAHI_PATH, intro)
            self._server = obj.get_interface(SERVER_IFACE)
            with contextlib.suppress(Exception):
                self._server2 = obj.get_interface(SERVER2_IFACE)
        except Exception:  # noqa: BLE001 - no Avahi here; discovery is optional, audio is not
            logger.warning("Avahi unavailable — Sendspin mDNS publish/browse disabled", exc_info=True)
            self._bus = self._server = self._server2 = None
            return False
        return True

    async def close(self) -> None:
        for group in self._groups:
            with contextlib.suppress(Exception):
                await group.call_reset()
        self._groups.clear()
        for browser in self._browsers:
            with contextlib.suppress(Exception):
                await browser.call_free()
        self._browsers.clear()
        if self._bus is not None:
            self._bus.disconnect()
        self._bus = self._server = self._server2 = None

    # -- publish -------------------------------------------------------------

    async def publish(self, name: str, service_type: str, port: int, txt: dict[str, str]) -> bool:
        """Advertise one service. The record lives until close(); returns False if Avahi is absent."""
        if not await self.connect():
            return False
        assert self._server is not None and self._bus is not None
        try:
            group_path = await self._server.call_entry_group_new()
            intro = await self._bus.introspect(AVAHI_BUS, group_path)
            group = self._bus.get_proxy_object(AVAHI_BUS, group_path, intro).get_interface(ENTRY_GROUP_IFACE)
            await group.call_add_service(
                IF_UNSPEC, PROTO_UNSPEC, 0,  # interface, protocol, flags
                name, service_type,  # Avahi wants the bare type ("_sendspin._tcp"), NOT ".local"-suffixed
                "", "",  # domain, host — empty = Avahi's defaults for this machine
                port, _txt_to_bytes(txt),
            )
            await group.call_commit()
            self._groups.append(group)
        except Exception:  # noqa: BLE001
            logger.warning("Avahi publish failed for %s %s", name, service_type, exc_info=True)
            return False
        logger.info("mDNS published %s.%s on :%d (%s)", name, service_type, port, txt)
        return True

    async def republish(self, name: str, service_type: str, port: int, txt: dict[str, str]) -> bool:
        """Replace everything this client has published with this one record.

        Avahi has no "rename a record" — the group has to be reset and re-committed. Note this drops
        ALL of this client's publications, which is correct only because each publisher here owns one
        record (the unit's server advert, or the player's). It deliberately does NOT close the bus:
        the Neighbourhood publishes and BROWSES on the same connection, so tearing it down to rename
        ourselves would silently stop peer discovery.
        """
        for group in self._groups:
            with contextlib.suppress(Exception):
                await group.call_reset()
                await group.call_free()
        self._groups.clear()
        return await self.publish(name, service_type, port, txt)

    # -- browse --------------------------------------------------------------

    async def browse(
        self,
        service_type: str,
        on_add: Callable[[DiscoveredService], None],
        on_remove: Callable[[str], None] | None = None,
    ) -> bool:
        """Watch a service type. `on_add` gets resolved services; `on_remove` gets their key."""
        if not await self.connect():
            return False
        assert self._server is not None and self._bus is not None
        # ServiceBrowserNew starts announcing IMMEDIATELY, so entries already in Avahi's cache can
        # be delivered before our signal handlers are attached a round-trip later — a unit that
        # booted first would then be invisible to a unit that booted second, which is exactly the
        # asymmetry this hit on hardware. Server2's Prepare/Start pair exists for this: build the
        # browser, subscribe, then start it. Fall back to the racy call only on ancient daemons.
        deferred = self._server2 is not None
        try:
            if deferred:
                browser_path = await self._server2.call_service_browser_prepare(
                    IF_UNSPEC, PROTO_UNSPEC, service_type, "", 0
                )
            else:
                browser_path = await self._server.call_service_browser_new(
                    IF_UNSPEC, PROTO_UNSPEC, service_type, "", 0
                )
            intro = await self._bus.introspect(AVAHI_BUS, browser_path)
            browser = self._bus.get_proxy_object(AVAHI_BUS, browser_path, intro).get_interface(BROWSER_IFACE)
        except Exception:  # noqa: BLE001
            logger.warning("Avahi browse failed for %s", service_type, exc_info=True)
            return False

        def _on_item_new(interface: int, protocol: int, name: str, stype: str, domain: str, flags: int) -> None:
            # Resolution is a round trip; do it off the signal handler.
            asyncio.ensure_future(self._resolve(interface, protocol, name, stype, domain, on_add))

        def _on_item_remove(_i: int, _p: int, name: str, stype: str, _d: str, _f: int) -> None:
            self._seen.pop(f"{stype}/{name}", None)
            if on_remove is not None:
                on_remove(f"{stype}/{name}")

        browser.on_item_new(_on_item_new)
        browser.on_item_remove(_on_item_remove)
        if deferred:
            with contextlib.suppress(Exception):
                await browser.call_start()  # now that we are listening
        self._browsers.append(browser)
        logger.info("mDNS browsing %s", service_type)
        return True

    async def _resolve(
        self, interface: int, protocol: int, name: str, service_type: str, domain: str,
        on_add: Callable[[DiscoveredService], None],
    ) -> None:
        assert self._server is not None
        try:
            resolved = await self._server.call_resolve_service(
                interface, protocol, name, service_type, domain, PROTO_UNSPEC, 0
            )
        except Exception:  # noqa: BLE001 - service vanished between announce and resolve
            logger.debug("resolve failed for %s %s", name, service_type, exc_info=True)
            return
        # (interface, protocol, name, type, domain, host, aprotocol, address, port, txt, flags)
        address, port, txt = resolved[7], int(resolved[8]), _txt_from_bytes(resolved[9])
        key = f"{service_type}/{name}"
        service = self._seen.get(key)
        if service is None:
            service = DiscoveredService(
                name=name, service_type=service_type, port=port, txt=txt,
                _prefer_subnet_of=self._local_ip,
            )
            self._seen[key] = service
        if address and address not in service.addresses:
            service.addresses.append(address)
        service.txt.update(txt)
        logger.info("mDNS discovered %s at %s (addrs=%s)", service.key, service.ws_url, service.addresses)
        with contextlib.suppress(Exception):
            on_add(service)
