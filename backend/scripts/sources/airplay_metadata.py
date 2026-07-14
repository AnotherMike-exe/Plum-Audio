#!/usr/bin/env python3
"""
Plum-Audio — AirPlay metadata/artwork → Sendspin roles (Phase 1, item 3).

shairport-sync writes track metadata + cover art to a *separate* pipe (metadata.pipe_name,
/tmp/airplay-metadata-fifo) as a stream of XML <item> elements. This in-process reader parses
them and emits to the AirPlay source group's **metadata** and **artwork** Sendspin roles —
OUT OF BAND from the audio stream.

Why this is a fraction of the old Plum-Snapcast `airplay-control-script.py` (2280 lines):
on Snapcast every metadata push triggered `onResync()`, so that script needed five guards
(track-change position reset, stale-prgr rejection, GUI-pause suppression, dedup, debounce).
On Sendspin a metadata push is NOT an audio-stream event — the resync storm cannot happen by
construction — so all of that is DROPPED. We just parse and set role state.

shairport metadata item format (each item may span several lines; accumulate to </item>):
    <item><type>HEX4</type><code>HEX4</code><length>N</length>
    <data encoding="base64">BASE64</data></item>
  type: "core" (DMAP track metadata) or "ssnc" (shairport control/session).
  codes we use — core: minm=title, asar=artist, asal=album;
                  ssnc: mdst/mden=bundle start/end, PICT=cover art, prgr=progress (RTP frames),
                        pend/pfls=stop/flush (clear), pbeg/prsm/paus=play state.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import logging
import os
import xml.etree.ElementTree as ET

from PIL import Image

logger = logging.getLogger("plum.airplay_metadata")

# shairport progress RTP timestamps are at the AirPlay stream rate (44.1 kHz).
AIRPLAY_RTP_RATE = 44100
READER_LIMIT = 8 * 1024 * 1024  # generous StreamReader line buffer (base64 art lines)


class AirplayMetadataReader:
    """Reads the shairport metadata FIFO and drives a source group's metadata/artwork roles."""

    def __init__(self, group, fifo_path: str, rtp_rate: int = AIRPLAY_RTP_RATE) -> None:
        self.group = group
        self.fifo_path = fifo_path
        self.rtp_rate = rtp_rate
        self._pending: dict[str, str] = {}  # title/artist/album accumulated within a bundle
        self._task: asyncio.Task | None = None
        self._stop_evt = asyncio.Event()
        self._warned_no_role = False

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self.run())

    async def stop(self) -> None:
        self._stop_evt.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    # -- role access ---------------------------------------------------------

    def _metadata_role(self):
        role = self.group.group_role("metadata")
        if role is None and not self._warned_no_role:
            logger.warning("group has no metadata role; metadata will be dropped")
            self._warned_no_role = True
        return role

    def _artwork_role(self):
        return self.group.group_role("artwork")

    # -- FIFO reader ---------------------------------------------------------

    def _ensure_fifo(self) -> None:
        if not os.path.exists(self.fifo_path):
            os.mkfifo(self.fifo_path, mode=0o660)
            logger.info("created metadata FIFO %s", self.fifo_path)

    async def _open_reader(self):
        self._ensure_fifo()
        loop = asyncio.get_running_loop()
        fd = os.open(self.fifo_path, os.O_RDONLY | os.O_NONBLOCK)
        pipe = os.fdopen(fd, "rb", buffering=0)
        reader = asyncio.StreamReader(limit=READER_LIMIT)
        transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), pipe)
        return reader, transport

    async def run(self) -> None:
        logger.info("airplay metadata reader up: %s", self.fifo_path)
        while not self._stop_evt.is_set():
            reader = transport = None
            try:
                reader, transport = await self._open_reader()
                await self._pump(reader)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - never let metadata parsing kill the reader
                logger.exception("metadata reader error; retrying")
                await asyncio.sleep(0.5)
            finally:
                if transport is not None:
                    transport.close()

    async def _pump(self, reader: asyncio.StreamReader) -> None:
        buf = ""
        while not self._stop_evt.is_set():
            raw = await reader.readline()
            if not raw:
                return  # EOF: shairport closed the metadata writer — reopen
            line = raw.decode("utf-8", errors="ignore").strip()
            if not line:
                continue
            if line.startswith("<item>") and line.endswith("</item>"):
                await self._handle_item(line)
            elif line.startswith("<item>"):
                buf = line
            elif line.endswith("</item>"):
                await self._handle_item(buf + line)
                buf = ""
            else:
                buf += line

    # -- item parsing --------------------------------------------------------

    async def _handle_item(self, item_xml: str) -> None:
        try:
            root = ET.fromstring(item_xml)
        except ET.ParseError:
            return  # partial/corrupt item (buffer cut mid-XML) — skip
        code_elem = root.find("code")
        type_elem = root.find("type")
        if code_elem is None or code_elem.text is None:
            return
        item_type = _hex_to_ascii(type_elem.text) if type_elem is not None and type_elem.text else ""
        code = _hex_to_ascii(code_elem.text)

        data_elem = root.find("data")
        raw_b64 = (data_elem.text or "").strip() if data_elem is not None else ""

        if item_type == "core":
            self._handle_core(code, _decode_text(raw_b64))
        elif item_type == "ssnc":
            await self._handle_ssnc(code, raw_b64)

    def _handle_core(self, code: str, value: str) -> None:
        if code == "minm":
            self._pending["title"] = value
        elif code == "asar":
            self._pending["artist"] = value
        elif code == "asal":
            self._pending["album"] = value

    async def _handle_ssnc(self, code: str, raw_b64: str) -> None:
        if code == "mdst":
            self._pending = {}
        elif code == "mden":
            self._flush_bundle()
        elif code == "prgr":
            self._handle_progress(_decode_text(raw_b64))
        elif code == "PICT":
            await self._handle_picture(raw_b64)
        elif code in ("pend", "pfls"):
            self._clear()
            await self._set_artwork(None)

    def _flush_bundle(self) -> None:
        role = self._metadata_role()
        if role is None or not self._pending:
            return
        kwargs = {k: v for k, v in self._pending.items() if v}
        if kwargs:
            role.update(**kwargs)
            logger.info("metadata: %s", " · ".join(f"{k}={v}" for k, v in kwargs.items()))

    def _handle_progress(self, decoded: str) -> None:
        role = self._metadata_role()
        if role is None:
            return
        try:
            start, current, end = (int(x) for x in decoded.split("/"))
        except (ValueError, AttributeError):
            return
        position_ms = max(0, (current - start) * 1000 // self.rtp_rate)
        duration_ms = max(0, (end - start) * 1000 // self.rtp_rate)
        role.update(track_progress=position_ms, track_duration=duration_ms)

    async def _handle_picture(self, raw_b64: str) -> None:
        if not raw_b64:
            await self._set_artwork(None)  # empty PICT = no art
            return
        try:
            img = Image.open(io.BytesIO(base64.b64decode(raw_b64)))
            img.load()
        except Exception:  # noqa: BLE001 - malformed art shouldn't break the stream
            logger.debug("failed to decode PICT artwork", exc_info=True)
            return
        await self._set_artwork(img)
        logger.info("artwork: %dx%d %s", img.width, img.height, img.format or "?")

    async def _set_artwork(self, img: Image.Image | None) -> None:
        role = self._artwork_role()
        if role is not None:
            with contextlib.suppress(Exception):
                await role.set_album_artwork(img)

    def _clear(self) -> None:
        role = self._metadata_role()
        if role is not None:
            role.clear()
        self._pending = {}


def _hex_to_ascii(hex_text: str) -> str:
    try:
        return bytes.fromhex(hex_text.strip()).decode("ascii", errors="ignore")
    except ValueError:
        return ""


def _decode_text(raw_b64: str) -> str:
    if not raw_b64:
        return ""
    try:
        return base64.b64decode(raw_b64).decode("utf-8", errors="ignore").strip()
    except Exception:  # noqa: BLE001
        return ""
