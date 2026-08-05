#!/usr/bin/env python3
"""
Shared rendering primitives for daemon config templates.

Two concerns that every source's `render_configs` had to get right independently, and that are
easy to get wrong in a way nothing notices until it matters:

1. **Escaping.** A device name is user input that arrives over an unauthenticated REST API and ends
   up inside a quoted scalar in a config file, after which a daemon is respooled to read it. An
   unescaped quote does not corrupt the file harmlessly — it closes the string and lets the rest of
   the name become configuration. shairport-sync's `sessioncontrol` block can run shell commands,
   so that is remote code execution as root. settings_api sanitizes on the way in; this is the
   layer that makes the guarantee, because it is the one that knows the target syntax.

2. **Atomicity.** These were truncate-then-write. A crash or a full disk mid-write leaves a
   half-rendered config, and the managers' guard checks only that the file EXISTS — so the daemon
   launches, dies on a parse error, and `_reap_and_respawn` retries it forever.
"""

from __future__ import annotations

import os
import tempfile

# libconfig (shairport-sync) string escapes. The grammar is C-like: only the backslash and the
# closing quote can terminate or alter a double-quoted string, and a raw newline is a syntax error.
_LIBCONFIG_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def escape_libconfig(value: str) -> str:
    """Escape a string for interpolation into a double-quoted libconfig scalar."""
    return "".join(_LIBCONFIG_ESCAPES.get(ch, ch) for ch in value)


def escape_yaml_double_quoted(value: str) -> str:
    """Escape a string for interpolation into a double-quoted YAML scalar.

    YAML's double-quoted style uses the same backslash escapes as JSON, so the rules match
    escape_libconfig — but they are spelled out separately rather than shared, because the two
    formats are only coincidentally alike and a future divergence should not silently apply the
    wrong one.
    """
    out = []
    for ch in value:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        else:
            out.append(ch)
    return "".join(out)


def write_atomic(path: str, content: str) -> None:
    """Write `content` to `path` via a unique temp file + os.replace.

    Unique temp name, not `<path>.tmp`: the rename is atomic but a shared temp path is not, and
    these renderers can run for several endpoints in one pass.
    """
    directory = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".render-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
