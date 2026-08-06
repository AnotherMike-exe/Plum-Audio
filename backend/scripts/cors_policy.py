#!/usr/bin/env python3
"""Which browser origins may call this unit's APIs.

Both APIs were `Access-Control-Allow-Origin: *` with no auth, on 0.0.0.0 — so any page on the LAN
could rename a unit, disable a source or re-route its audio. This narrows that to the origins a
Plum GUI is actually served from.

Three facts shape the whole design, and getting any of them wrong takes the mesh view down on every
unit at once:

1. **The GUI's mesh writes are cross-origin even to the unit serving the page.** It POSTs to
   `http://<unit-ip>:5001` directly for EVERY unit including the local one (the data service builds
   the URL from the view's `host`), while its GETs go through nginx same-origin. A policy that
   allowed peers but not self would break every route/volume/adopt on the page you are looking at.

2. **Server-to-server and loopback requests carry NO `Origin` header at all.** Peer snapshot polls,
   delegated routes, and the player's own player-state POST are not browser requests and were never
   subject to CORS. They must pass untouched — rejecting Origin-less requests would break mesh
   aggregation and cross-unit routing from the inside, with nothing in the browser to show for it.

3. **An origin's host may be an IP, `<host>.local`, a bare hostname, or a dev `:5173`,** while the
   peer table only ever learns IPs. Hence `hostname` on the snapshot: each unit publishes what it
   calls itself so its peers can recognise a page served BY it. `PLUM_ALLOWED_ORIGINS` covers
   whatever is left (a reverse proxy, a dev box, an alias).

Escape hatch: `PLUM_ALLOWED_ORIGINS=*` restores the old blanket behaviour without a rebuild. It
exists because this is the kind of change that fails on a rig at 2am, and a unit whose GUI is dead is
worse than one whose GUI is reachable from the wrong page.
"""

from __future__ import annotations

import contextlib
import logging
import os
import socket
from urllib.parse import urlsplit

logger = logging.getLogger("plum.cors")

# Always allowed: the page is being served from the box it is talking to.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})

_WILDCARD = "*"


def configured_extra_origins() -> list[str]:
    """`PLUM_ALLOWED_ORIGINS` — comma-separated absolute origins, or `*` to allow everything."""
    raw = (os.environ.get("PLUM_ALLOWED_ORIGINS") or "").strip()
    return [item.strip() for item in raw.split(",") if item.strip()]


def _host_of(origin: str) -> str | None:
    """The bare hostname of an origin, lowercased, without port. None if it is not parseable."""
    try:
        parts = urlsplit(origin)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    try:
        host = parts.hostname
    except ValueError:  # malformed IPv6 literal
        return None
    return host.lower() if host else None


def name_forms(host: str) -> set[str]:
    """A hostname and the other spellings the same box answers to.

    `plum-amp100`, `plum-amp100.local` and `plum-amp100.lan` are one machine; which one is in the
    address bar is the user's choice, and the unit cannot know which they used.
    """
    host = host.lower().rstrip(".")
    bare = host.split(".", 1)[0]
    return {host, bare, f"{bare}.local", f"{bare}.lan"}


def local_ip() -> str | None:
    """This box's own IP on the default-route interface.

    The same trick mesh/aggregator.py uses, and for the same reason: a unit cannot learn the address
    peers reach it on from its own beacon. It sends no packets — it asks the kernel which source
    address the default route would use.

    Deliberately NOT `gethostbyname(gethostname())`: inside the container that resolves to 127.0.1.1
    from /etc/hosts, so the unit's real LAN address never enters the allowlist and a page loaded at
    `http://<ip>` is refused. Measured on .2.10 (2026-08-06).
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))  # RFC 5737 TEST-NET-1 — unroutable, never contacted
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def own_hosts() -> set[str]:
    """Every host form that means "this box" — for an API with no mesh view to derive one from.

    Recomputed per request rather than cached: the mDNS hostname is user-editable from Settings and
    applies live, so a cached set would lock the config API to the OLD name and lock the user out of
    the page they just renamed.
    """
    hosts = set(LOOPBACK_HOSTS)
    ip = local_ip()
    if ip:
        hosts.add(ip)
    with contextlib.suppress(OSError):
        hosts |= name_forms(socket.gethostname())
    return hosts


def known_hosts(units) -> set[str]:
    """Every host form the units in a mesh view can legitimately serve a GUI from.

    `units` is any iterable of objects with `.host` and (optionally) `.hostname` — a MeshView's
    units, in practice. Both are included: the IP is how peers reach each other, the hostname is how
    a person reaches the page.
    """
    hosts: set[str] = set(LOOPBACK_HOSTS)
    for unit in units:
        host = (getattr(unit, "host", None) or "").strip().lower()
        if host:
            hosts.add(host)
        name = (getattr(unit, "hostname", None) or "").strip()
        if name:
            hosts |= name_forms(name)
    return hosts


def is_allowed(origin: str | None, hosts: set[str], extras: list[str] | None = None) -> bool:
    """May `origin` call us?

    A None/empty origin is NOT decided here — see `origin_header`. This answers only the browser
    question, and is deliberately port-agnostic: the GUI is served on :80, dev runs on :5173, and a
    unit that is reachable at all is reachable on any of its own ports anyway.
    """
    if not origin:
        return False
    extras = extras if extras is not None else configured_extra_origins()
    if _WILDCARD in extras:
        return True
    if origin in extras:
        return True
    host = _host_of(origin)
    if host is None:
        return False
    return host in hosts


def origin_header(origin: str | None, hosts: set[str], extras: list[str] | None = None) -> str | None:
    """The value for `Access-Control-Allow-Origin`, or None to send no CORS headers at all.

    Sending nothing is the right answer in BOTH the cases that produce it:

    - No `Origin`: not a browser request (a peer, the loopback player). CORS never applied; adding
      headers would be noise, and refusing would break the mesh from the inside.
    - A rejected origin: the response still returns normally, but with no CORS headers the browser
      refuses to hand it to the calling page. That is how CORS denies — it is not our job to 403,
      and doing so would change what a same-origin caller sees.

    Echoes the exact origin rather than `*`: `*` is what we are moving away from, and an echo is
    also required if credentials are ever added.
    """
    if not origin:
        return None
    return origin if is_allowed(origin, hosts, extras) else None
