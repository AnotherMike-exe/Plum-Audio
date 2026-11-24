"""Unit tests for the security headers in `backend/nginx/plum-audio.conf`.

nginx has one rule that makes these headers easy to lose: a `location` that sets ANY `add_header`
inherits none from the `server` block. The GUI's own page (`/index.html`) sets Cache-Control, so a
server-level header alone would miss the one document the headers exist for.

Run: `pytest tests/Unit/test_nginx_conf.py`.
"""

import re
from pathlib import Path

import pytest

CONF = Path(__file__).resolve().parents[2] / "backend" / "nginx" / "plum-audio.conf"

HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "SAMEORIGIN",
}


def _directives(block: str) -> dict[str, str]:
    """`add_header` name -> the rest of the directive, for one block's own lines."""
    return dict(re.findall(r"^\s*add_header\s+(\S+)\s+([^;]+);", block, re.M))


def _code() -> str:
    return "\n".join(line for line in CONF.read_text().splitlines() if not line.lstrip().startswith("#"))


def _locations() -> dict[str, str]:
    return {m.group(1).strip(): m.group(2) for m in re.finditer(r"location\s+([^{]+)\{([^}]*)\}", _code())}


def _server_level() -> str:
    return re.sub(r"location\s+[^{]+\{[^}]*\}", "", _code())


@pytest.mark.parametrize(("name", "value"), HEADERS.items())
def test_the_server_block_sets_each_header_on_every_reply(name, value):
    directive = _directives(_server_level())[name]
    assert f'"{value}"' in directive
    assert directive.endswith("always")  # without it nginx omits the header on a 4xx or 5xx


def test_a_location_with_its_own_header_repeats_all_three():
    checked = 0
    for path, block in _locations().items():
        own = _directives(block)
        if not own:
            continue  # inherits the server block's headers
        checked += 1
        for name, value in HEADERS.items():
            assert name in own, f"location {path} sets add_header and so drops {name}"
            assert f'"{value}"' in own[name]
            assert own[name].endswith("always")
    assert checked >= 2  # /index.html and /assets/


def test_the_page_itself_is_covered():
    assert "X-Frame-Options" in _directives(_locations()["= /index.html"])


def test_no_frame_ancestors_policy_is_set():
    """Decided with X-Frame-Options only. A CSP would also need every other directive reviewed."""
    assert "frame-ancestors" not in _code()
    assert "Content-Security-Policy" not in _code()
