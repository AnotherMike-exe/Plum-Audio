#!/usr/bin/env python3
"""
Plum-Audio — the names speakers declare over the protocol, remembered per unit.

A speaker names itself twice and which one you can see depends on where it is. ATTACHED, it is in
its server's `players` carrying the name it gave at the Sendspin handshake ("Home Assistant Voice
PE - 01"). IDLE, no server holds it, so the only trace is its mDNS advertisement — and a
third-party device usually publishes no `name` TXT key, leaving the bare instance name
("home-assistant-voice-a1b2c3"). One device therefore reads as two.

The GUI already memoised this against the speaker's listener URL — the only identifier both views
share, since the client id is not (mDNS names by instance, the handshake by MAC). But it did so in
**localStorage**, which made the memo per-browser and per-origin:

  * a browser that had never watched that speaker attach had nothing to fall back to, so the first
    idle sighting showed the technical name (reported from the rig 2026-08-05);
  * two units' GUIs learned independently, so they could disagree;
  * clearing site data lost it.

The name is a property of the speaker, not of whoever happened to be looking, so it belongs here.
The audio process is the only thing that ever sees a handshake name, and it writes this file the
same way and for the same reason it writes player_state.json — runtime state it is the source of
truth for, kept out of settings.json because a different process owns that.

Read back by mesh/api.py when it serves the neighbourhood, so an idle speaker is announced under the
name it actually calls itself. That keeps the whole fix server-side: the GUI already prefers
`friendly_name` and needs no change.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from urllib.parse import urlparse

logger = logging.getLogger("plum.speaker_names")

DEFAULT_FILE = "/data/speaker_names.json"
# A ceiling, so a segment full of transient speakers cannot grow this without bound. Far above any
# real installation; entries are tiny and only ever added when a speaker actually attaches.
MAX_ENTRIES = 256


def names_file_path() -> str:
    return os.environ.get("PLUM_SPEAKER_NAMES_FILE", DEFAULT_FILE)


def _hostport(url: str | None) -> tuple[str, int] | None:
    """(host, port) from a ws:// URL, or None: the comparable part of a listener URL.

    The same rule as `mesh.neighbourhood._hostport`, for the same reason. The path and the scheme
    differ between what a device advertises over mDNS and what the handshake registered, so the
    exact URL text is not a reliable key by itself.
    """
    if not url:
        return None
    try:
        parsed = urlparse(url)
        if not parsed.hostname:
            return None
        return (parsed.hostname, parsed.port or 0)
    except ValueError:  # a malformed IPv6 literal or a port that is not a number
        return None


class SpeakerNames:
    """A URL -> declared-name memo, persisted best-effort.

    Never raises: a speaker whose name we cannot remember still works, it just reads as its mDNS
    instance name while idle — exactly the behaviour this exists to improve on.

    TWO instances share one file: the audio server learns names, the mesh API serves them. So each
    read checks if the file changed (`_refresh`) and loads it again when it did. The file was read
    once, in `__init__`, and a name learned after startup was then on disk but not served until a
    restart.
    """

    def __init__(self, path: str | None = None) -> None:
        self.path = path or names_file_path()
        self._names: dict[str, str] = {}
        self._by_hostport: dict[tuple[str, int], str] = {}
        # What the file looked like when `_names` was last in step with it. None: no file.
        self._seen: tuple[int, int, int] | None = None
        self._refresh()

    def _signature(self) -> tuple[int, int, int] | None:
        """The file's identity, for one `stat`. None when there is no file to read.

        The inode as well as `st_mtime_ns`: `_save` replaces the file by rename, so every write is
        a new inode, and two writes inside one timestamp tick still read as a change.
        """
        try:
            st = os.stat(self.path)
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_ino, st.st_size)

    def _refresh(self) -> None:
        """Load the file again if it changed since the last look. One `stat` when it did not."""
        signature = self._signature()
        if signature == self._seen:
            return
        self._seen = signature
        self._set(self._load() if signature is not None else {})

    def _set(self, names: dict[str, str]) -> None:
        self._names = names
        self._by_hostport = {}
        for url in names:
            key = _hostport(url)
            if key is not None:
                self._by_hostport[key] = url

    def _load(self) -> dict[str, str]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                return {}
            return {str(k): str(v) for k, v in data.items() if k and v}
        except FileNotFoundError:
            return {}
        except Exception:  # noqa: BLE001 - malformed/unreadable: start empty rather than fail to boot
            logger.warning("speaker names %s unreadable — starting empty", self.path)
            return {}

    def _save(self) -> None:
        try:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=directory or ".", prefix=".speaker-names-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(self._names, fh, indent=2)
                os.replace(tmp, self.path)
                # Our own write is not news to us: do not load it back on the next read.
                self._seen = self._signature()
            finally:
                # A failure before the rename leaves the temp behind; mkstemp names are unique, so
                # without this every failed save leaks a file into /data.
                if os.path.exists(tmp):
                    with contextlib.suppress(OSError):
                        os.unlink(tmp)
        except Exception:  # noqa: BLE001 - persistence is a convenience; never break the audio path
            logger.debug("could not persist speaker names to %s", self.path, exc_info=True)

    def get(self, url: str | None) -> str | None:
        """The name learned for this listener URL. Exact text first, then the same host and port."""
        if not url:
            return None
        self._refresh()
        name = self._names.get(url)
        if name is not None:
            return name
        key = _hostport(url)
        known = self._by_hostport.get(key) if key is not None else None
        return self._names.get(known) if known is not None else None

    def all(self) -> dict[str, str]:
        self._refresh()
        return dict(self._names)

    def learn(self, url: str | None, name: str | None) -> bool:
        """Record what a speaker calls itself. Returns True if anything changed."""
        if not url or not name:
            return False
        # Start from what is on disk, so this write keeps a name another instance learned.
        self._refresh()
        if self._names.get(url) == name:
            return False
        if url not in self._names and len(self._names) >= MAX_ENTRIES:
            logger.warning("speaker name memo full (%d); not learning %s", MAX_ENTRIES, url)
            return False
        self._names[url] = name
        key = _hostport(url)
        if key is not None:
            self._by_hostport[key] = url
        self._save()
        logger.info("learned speaker name %s -> %r", url, name)
        return True
