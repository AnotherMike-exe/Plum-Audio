"""Where a source FIFO may live, and what a source id may be made of.

Both rules exist because **a source id reaches the filesystem**: it names the FIFO the server
creates with `os.mkfifo` and then opens for reading.

`POST /api/mesh/source` takes the source id and an optional FIFO path straight from the request
body, and that API is unauthenticated and bound to `0.0.0.0` (docs/OPEN-ITEMS.md). Before this
module, a caller on the VLAN could name any path and have the unit create a FIFO there, or point a
"source" at any file the process could read and hear the contents played out a speaker. CodeQL
`py/path-injection`, three alerts, found on `main` 2026-09-13.

This lives in its own module rather than in `sendspin_server` so that `mesh/api.py` can state the
same rule at the boundary without importing the whole audio engine — the engine already imports
`mesh.model`, and a top-level import back the other way is a cycle waiting to happen.

The rule is enforced in BOTH places on purpose. The API returns 400 so a bad request reads as the
caller's mistake, and `SourceFeeder`'s owner refuses as well, because it is the single choke point
every caller goes through: the mesh API, the source managers, and the calibration tone.
"""

from __future__ import annotations

import os
import re

# Every FIFO this project creates is a flat name in /tmp — see sources/*_config.py and
# calibration_tone.py. Overridable only so a test can point it somewhere writable.
FIFO_DIR = os.environ.get("PLUM_FIFO_DIR", "/tmp")

# What real source ids actually contain, and no more: `airplay-1`, `spotify-2`, `bluetooth-1`, and
# `cal:<player_id>` where the player id is a base64url X25519 key (so `-` and `_` are needed). No
# slash, no path separator, and a leading alphanumeric so nothing can begin with a dot.
SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def valid_source_id(source_id: str) -> bool:
    """Whether `source_id` is safe to interpolate into a path. See SOURCE_ID_RE."""
    if not source_id or ".." in source_id:
        return False
    return SOURCE_ID_RE.match(source_id) is not None


def fifo_path_for(source_id: str) -> str:
    """The FIFO path a source gets when the caller does not name one.

    Callers must validate `source_id` first — this does not, because the answer for an invalid id
    is a refusal, not a path.
    """
    return os.path.join(FIFO_DIR, f"{source_id}-fifo")


def fifo_path_is_safe(fifo_path: str) -> bool:
    """Whether `fifo_path` names a file DIRECTLY inside FIFO_DIR.

    Compares the RESOLVED parent directory, so `/tmp/../etc/passwd` and a symlinked `/tmp` are both
    handled. A nested path is refused too: every FIFO this project creates is a flat name, so there
    is no case to allow one, and allowing one widens the target set for no gain.
    """
    if not fifo_path or "\x00" in fifo_path:
        return False
    parent = os.path.realpath(os.path.dirname(os.path.abspath(fifo_path)))
    return parent == os.path.realpath(FIFO_DIR)
