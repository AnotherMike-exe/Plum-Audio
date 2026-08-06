#!/usr/bin/env python3
"""
Album-art decoding, off the audio event loop.

All three metadata handlers (AirPlay PICT, Spotify cover download, Bluetooth OBEX fetch) ran
`Image.open(...)` followed by `img.load()` directly in a coroutine. `Image.open` is lazy, but
`load()` forces the full raster decode — and every one of those coroutines runs on the same event
loop as `SourceFeeder._pump`, which is what keeps audio flowing into the Sendspin server.

iOS routinely sends 1400x1400 AirPlay cover art. A decode of that size is tens to low hundreds of
milliseconds on a Pi; the server's target buffer plus the player's jitter buffer normally absorb it,
which is why this has never been the reported symptom. It stops being absorbed when several
endpoints change track at once, and the failure then looks like a network glitch rather than an
artwork bug — nothing in the logs points here.

`asyncio.to_thread` is enough: Pillow releases the GIL around the decode, and the work is genuinely
independent of the loop.
"""

from __future__ import annotations

import asyncio
import io
import logging

from PIL import Image

logger = logging.getLogger(__name__)


def _decode(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img.load()  # force the raster decode HERE, on the worker thread, not lazily back on the loop
    return img


async def decode_image(data: bytes, *, what: str = "artwork") -> Image.Image | None:
    """Decode image bytes on a worker thread. Returns None on malformed input.

    Malformed art must never break the stream — every caller previously swallowed the exception
    locally, so that contract is preserved here rather than pushed back onto them.
    """
    if not data:
        return None
    try:
        return await asyncio.to_thread(_decode, data)
    except Exception:  # noqa: BLE001 - malformed art shouldn't break the source
        logger.debug("failed to decode %s", what, exc_info=True)
        return None
