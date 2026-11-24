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
import ipaddress
import json
import logging
import os
import re
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
    return hosts | mdns_hosts()


_SETTINGS_FILE_DEFAULT = "/data/settings.json"
_HOSTNAME_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
# (path, st_mtime_ns) -> names. One stat per request; the file is read again only when it changed.
_mdns_cache: tuple[tuple[str, int], set[str]] | None = None


def mdns_hosts() -> set[str]:
    """The name forms of the mDNS hostname a user set from Settings (`settings.json` `hostname`).

    `socket.gethostname()` is the HOST's name. Settings -> Device can give Avahi a different name
    at runtime (`SetHostName`), and the page is then at `http://<that-name>.local`. The kernel
    hostname never learns it, so without this the guard refuses the page the user just renamed.

    Read from the file and not through SettingsManager: the audio process loads this module, and
    it must not import Flask. Read-only, and never raises.
    """
    global _mdns_cache
    path = os.environ.get("PLUM_SETTINGS_FILE", _SETTINGS_FILE_DEFAULT)
    try:
        key = (path, os.stat(path).st_mtime_ns)
    except OSError:
        return set()
    if _mdns_cache is not None and _mdns_cache[0] == key:
        return set(_mdns_cache[1])
    names: set[str] = set()
    try:
        with open(path, encoding="utf-8") as fh:
            hostname = json.load(fh).get("hostname")
        if isinstance(hostname, str) and _HOSTNAME_LABEL.fullmatch(hostname.lower()):
            names = name_forms(hostname)
    except (OSError, ValueError, AttributeError):
        names = set()  # torn or damaged file: the next change to it is read again
    _mdns_cache = (key, names)
    return set(names)


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
    - A rejected origin: no CORS headers, so the browser refuses to hand the reply to the calling
      page. That is how CORS denies a READ. It does not stop the handler, so `guard` below also
      answers 403 before the handler runs. With `PLUM_API_GUARD=0` only this half applies.

    Echoes the exact origin rather than `*`: `*` is what we are moving away from, and an echo is
    also required if credentials are ever added.
    """
    if not origin:
        return None
    return origin if is_allowed(origin, hosts, extras) else None


# -- the request guard ------------------------------------------------------------------------------
#
# Everything above decides which CORS headers a reply carries. CORS only stops a page from READING
# a reply: the handler still ran. So a `text/plain` POST from any web page (a "simple" request, no
# preflight) re-routed audio blind, and a websocket upgrade, which CORS does not cover at all, read
# the cached pairing PIN from /api/mesh/consume. The guard refuses BEFORE the handler. Both APIs
# call it: mesh/api.py as aiohttp middleware, apis/server.py as a Flask `before_request`.
#
# PLUM_API_GUARD=0 turns the guard off and leaves the CORS headers as they were. It exists for the
# same reason PLUM_ALLOWED_ORIGINS=* does: this change needs a rig test, and a unit whose GUI is
# dead is worse than one that is reachable from the wrong page. Set it in plum-audio.env and
# restart the container. PLUM_ALLOWED_ORIGINS is the narrower fix for one refused origin or host.

BODY_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})


def guard_enabled() -> bool:
    """False only when `PLUM_API_GUARD` is `0`, `false`, `no` or `off`."""
    return (os.environ.get("PLUM_API_GUARD") or "1").strip().lower() not in ("0", "false", "no", "off")


def _host_header_name(host: str) -> str | None:
    """The bare hostname of a `Host` header, lowercased, without port. None if it is not parseable."""
    if not host or host != host.strip() or any(ch in host for ch in "/@?# \t"):
        return None
    try:
        parts = urlsplit(f"//{host}")
        _ = parts.port  # raises ValueError on a port that is not a number
        name = parts.hostname
    except ValueError:
        return None
    return name.lower().rstrip(".") if name else None


def _is_ip_literal(name: str) -> bool:
    try:
        ipaddress.ip_address(name.split("%", 1)[0])  # drop an IPv6 zone id
    except ValueError:
        return False
    return True


def host_is_trusted(host: str | None, hosts: set[str], extras: list[str] | None = None) -> bool:
    """May a request with NO `Origin` and this `Host` header reach a handler?

    This is the DNS-rebinding check. A page at evil.example whose name now resolves to the unit is
    same-origin to the browser, so its GETs carry no Origin, and the only trace of the attacker's
    name is the Host header. An IP literal cannot be rebound, so it always passes. That is also
    what every server-side caller sends (peers, the loopback player, Home Assistant by IP).

    An absent Host passes: only a browser can be rebound, and a browser always sends one.
    """
    if host is None:
        return True
    extras = extras if extras is not None else configured_extra_origins()
    if _WILDCARD in extras:
        return True
    name = _host_header_name(host)
    if name is None:
        return False
    if _is_ip_literal(name) or name in LOOPBACK_HOSTS or name in hosts:
        return True
    # A reverse proxy or an alias named in PLUM_ALLOWED_ORIGINS: its same-origin GETs carry that
    # name in Host and no Origin.
    return any(_host_of(extra) == name for extra in extras)


def guard(
    *,
    method: str,
    origin: str | None,
    host: str | None,
    content_type: str | None,
    has_body: bool,
    hosts: set[str],
    extras: list[str] | None = None,
) -> tuple[int, str] | None:
    """Decide if a request may reach its handler. None to pass, else (HTTP status, message).

    (a) `Origin` present and not allowed: 403, for every method. A websocket upgrade is a GET with
        an Origin, so this is the check that covers it.
    (b) `Origin` absent: the `Host` must be an IP literal, loopback, or a name we know. See
        `host_is_trusted`. An allowed Origin needs no Host check: its host is in the same set.
    (c) A POST, PUT or DELETE with a body must be `application/json`. A cross-site page can send
        `text/plain` with no preflight, and it cannot send JSON without one.
    """
    if not guard_enabled():
        return None
    extras = extras if extras is not None else configured_extra_origins()
    if origin:
        if not is_allowed(origin, hosts, extras):
            return 403, f"origin {origin[:200]!r} is not allowed (set PLUM_ALLOWED_ORIGINS to allow it)"
    elif not host_is_trusted(host, hosts, extras):
        return 403, f"host {str(host)[:200]!r} is not a name of this unit (set PLUM_ALLOWED_ORIGINS to allow it)"
    if method.upper() in BODY_METHODS and has_body:
        mimetype = (content_type or "").split(";", 1)[0].strip().lower()
        if mimetype != "application/json":
            return 415, "a request body must be Content-Type: application/json"
    return None


def has_body(content_length: str | int | None, transfer_encoding: str | None) -> bool:
    """Does a request carry a body, from its framing headers? Both APIs ask the same way."""
    if transfer_encoding:
        return True
    try:
        return int(content_length or 0) > 0
    except (TypeError, ValueError):
        return True  # an unreadable length is not proof of an empty body
