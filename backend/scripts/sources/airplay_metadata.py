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
import logging
import os
import time
import xml.etree.ElementTree as ET
from dataclasses import replace

from PIL import Image

from .artwork import decode_image

logger = logging.getLogger("plum.airplay_metadata")

# shairport progress RTP timestamps are at the AirPlay stream rate (44.1 kHz).
AIRPLAY_RTP_RATE = 44100
READER_LIMIT = 8 * 1024 * 1024  # generous StreamReader line buffer (base64 art lines)
# How often we re-emit progress. shairport's own prgr is sparse (seconds to a minute apart), which
# lets the SERVER's metadata role extrapolate a stale anchor and clamp to duration — every joining
# client then reads 100%. Re-anchoring at a steady cadence keeps that extrapolation window tiny.
PROGRESS_TICK_S = 1.0
# When a GUI issues pause, AirPlay keeps draining its buffer and shairport reports "still playing"
# (pbeg/prsm/prgr with speed) for several seconds before it confirms `paus`. We optimistically apply
# the command's state to every controller at once and IGNORE those reverting reports until shairport
# confirms — or these timeouts elapse (the command never took), after which reality wins again.
PAUSE_CONFIRM_TIMEOUT_S = 12.0
RESUME_CONFIRM_TIMEOUT_S = 5.0


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
        self._last_title: str | None = None  # to detect track changes at bundle flush
        self._waiting_for_fresh_prgr = False  # reject stale prgr from the old track after a change
        # Progress anchor we advance ourselves (see PROGRESS_TICK_S). `_anchor_pos_ms` is the position
        # at monotonic time `_anchor_at`; `_duration_ms` the track length; `_is_playing` gates the tick.
        self._anchor_pos_ms = 0
        self._anchor_at = 0.0
        self._duration_ms = 0
        self._is_playing = False
        self._ticker_task: asyncio.Task | None = None
        # GUI-command optimism: 'paused'/'playing' while a command awaits shairport's confirmation;
        # `_command_deadline` is the monotonic time we give up waiting and let reality resume.
        self._command_state: str | None = None
        self._command_deadline = 0.0

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self.run())
        if self._ticker_task is None:
            self._ticker_task = asyncio.ensure_future(self._progress_ticker())

    async def stop(self) -> None:
        self._stop_evt.set()
        for task in (self._task, self._ticker_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._task = self._ticker_task = None

    # -- progress anchor -----------------------------------------------------

    def _current_pos_ms(self) -> int:
        """Position right now: the last anchor advanced by real elapsed time while playing, capped
        at duration. Frozen at the anchor when paused."""
        if not self._is_playing:
            return self._anchor_pos_ms
        pos = self._anchor_pos_ms + int((time.monotonic() - self._anchor_at) * 1000)
        if self._duration_ms > 0:
            pos = min(pos, self._duration_ms)
        return max(0, pos)

    def _emit_progress(self, pos_ms: int, speed: int) -> None:
        role = self._metadata_role()
        if role is None or role.metadata is None or self._duration_ms <= 0:
            return
        # Force a FRESH server timestamp. role.update()/set_metadata otherwise INHERIT the previous
        # metadata's timestamp_us (a library quirk: replace() copies it, and set_metadata only stamps
        # anew when it is None). A frozen timestamp makes the client extrapolate from an ever-older
        # anchor — the position runs to 100% and a re-anchored (ticker) value gets double-counted on
        # top. Passing timestamp_us=None makes set_metadata stamp NOW, so every emit is a clean
        # (position @ now) pair the client reads directly. It also refreshes the server's own
        # join-snapshot anchor, so late-joining clients no longer read a clamped 100%.
        md = replace(
            role.metadata, track_progress=pos_ms, track_duration=self._duration_ms,
            playback_speed=speed, timestamp_us=None,
        )
        role.set_metadata(md)

    async def _progress_ticker(self) -> None:
        """Re-emit progress every PROGRESS_TICK_S while playing, so the server's own extrapolation
        (used for late-joining clients and continuous display) always works off a fresh anchor and
        can never drift/clamp to 100%. Idle when paused, between tracks, or before the first prgr."""
        while not self._stop_evt.is_set():
            await asyncio.sleep(PROGRESS_TICK_S)
            if self._command_state is not None and time.monotonic() >= self._command_deadline:
                # The source never confirmed our optimistic command — stop suppressing its reports.
                logger.info("command %s unconfirmed; releasing optimistic hold", self._command_state)
                self._command_state = None
            if self._is_playing and not self._waiting_for_fresh_prgr and self._duration_ms > 0:
                self._emit_progress(self._current_pos_ms(), 1000)

    # -- GUI command optimism ------------------------------------------------

    def apply_command(self, command: str) -> None:
        """Reflect a GUI transport command on the group IMMEDIATELY, before AirPlay round-trips.

        The controller command has already been forwarded to shairport (airplay_remote); here we
        also apply its effect to the metadata role so every GUI on this group flips together, then
        suppress shairport's lagging "still playing" reports until it confirms (see _handle_ssnc /
        _handle_progress). Without this, one GUI's pause would revert to playing for the seconds the
        AirPlay buffer takes to drain, disagreeing with any other GUI watching the same group."""
        if self._duration_ms <= 0:
            return
        if command == "pause":
            self._command_state = "paused"
            self._command_deadline = time.monotonic() + PAUSE_CONFIRM_TIMEOUT_S
            self._set_paused()
        elif command == "play":
            self._command_state = "playing"
            self._command_deadline = time.monotonic() + RESUME_CONFIRM_TIMEOUT_S
            self._set_playing()

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
        elif code == "asaa":
            # daap.songalbumartist — a UTF-8 string like the others, so it decodes the same way (the
            # numeric DMAP fields year/track are binary and are deliberately not parsed here).
            self._pending["album_artist"] = value

    async def _handle_ssnc(self, code: str, raw_b64: str) -> None:
        if code == "mdst":
            self._pending = {}
        elif code == "mden":
            self._flush_bundle()
        elif code == "prgr":
            # While a GUI pause is pending confirmation, shairport keeps sending advancing prgr from
            # the draining buffer — ignore it so the bar stays frozen where the user paused.
            if self._command_state != "paused":
                self._handle_progress(_decode_text(raw_b64))
        elif code == "PICT":
            await self._handle_picture(raw_b64)
        elif code in ("pbeg", "prsm"):
            if self._command_state == "playing":
                self._command_state = None  # source confirmed our resume
            if self._command_state != "paused":  # else: buffer still draining after a GUI pause
                self._set_playing()
        elif code in ("pend", "paus"):
            if self._command_state == "paused":
                self._command_state = None  # source confirmed our pause
            if self._command_state != "playing":  # else: a stale stop mid-resume
                self._set_paused()
        # pfls (flush/seek): no state change — a fresh prgr follows and re-anchors the position.

    def _flush_bundle(self) -> None:
        role = self._metadata_role()
        if role is None or not self._pending:
            return
        kwargs = {k: v for k, v in self._pending.items() if v}
        if not kwargs:
            return
        new_title = kwargs.get("title")
        if new_title and new_title != self._last_title:
            # Guard against stale prgr ONLY on a real track change (a previous track existed).
            # shairport may emit a prgr frame or two for the OLD track before the new one's timing
            # arrives; reject those until a fresh frame lands (see _handle_progress). But the FIRST
            # track of a session is legitimately mid-position (the sender can connect partway
            # through), so its prgr must be accepted — else the anchor never advances past the first
            # frame and the extrapolated position drifts to 100%.
            if self._last_title is not None:
                self._waiting_for_fresh_prgr = True
                # Stop the ticker and blank the bar: the old track's position (often near 100%) must
                # not linger on the new track for the seconds before its first fresh prgr arrives.
                self._is_playing = False
                self._anchor_pos_ms = 0
                self._duration_ms = 0
                role.update(track_progress=0, track_duration=0, playback_speed=0)
            self._last_title = new_title
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
        logger.debug(
            "prgr raw=%s -> pos=%d dur=%d wait=%s", decoded, position_ms, duration_ms, self._waiting_for_fresh_prgr
        )
        # shairport intermittently emits a frame with a STALE anchor where `current` runs past `end`
        # (observed 38 s past the end of a 290 s track), i.e. position > duration — physically
        # impossible. Written verbatim it pins the GUI bar to 100% and, worse, poisons
        # the anchor, so a later pause/resume freezes/resumes at "full". Reject such frames and keep
        # the last good position; the next valid frame re-anchors normally.
        if end <= start or current > end:
            logger.info("prgr rejected (current past end / non-positive duration): %s", decoded)
            return
        if self._waiting_for_fresh_prgr:
            prev = role.metadata
            prev_dur = prev.track_duration if prev is not None else None
            fresh = position_ms < 10_000 or (prev_dur is not None and abs(duration_ms - prev_dur) > 10_000)
            if not fresh:
                return  # stale frame from the previous track — keep the reset position
            self._waiting_for_fresh_prgr = False
        # Re-anchor from this real frame; the ticker advances it from here until the next one.
        self._anchor_pos_ms = position_ms
        self._anchor_at = time.monotonic()
        self._duration_ms = duration_ms
        self._is_playing = True
        self._emit_progress(position_ms, 1000)

    def _set_playing(self) -> None:
        """Resume: re-anchor the clock at the frozen position and let the ticker advance again."""
        if self._metadata_role() is None or self._duration_ms <= 0:
            return
        self._anchor_at = time.monotonic()  # elapsed measured from now, position unchanged
        self._is_playing = True
        logger.info("RESUME speed=1000 (pos=%d)", self._anchor_pos_ms)
        self._emit_progress(self._anchor_pos_ms, 1000)

    def _set_paused(self) -> None:
        """Freeze at the position we're actually showing right now (speed → 0), without clearing
        metadata/artwork. Uses the live anchor position, not the last raw prgr, so it neither jumps
        back to a stale frame nor forward past where the bar sits."""
        if self._metadata_role() is None or self._duration_ms <= 0:
            return
        self._anchor_pos_ms = self._current_pos_ms()
        self._anchor_at = time.monotonic()
        self._is_playing = False
        logger.info("PAUSE freeze at pos=%d", self._anchor_pos_ms)
        self._emit_progress(self._anchor_pos_ms, 0)

    async def _handle_picture(self, raw_b64: str) -> None:
        if not raw_b64:
            await self._set_artwork(None)  # empty PICT = no art
            return
        try:
            raw = base64.b64decode(raw_b64)
        except Exception:  # noqa: BLE001 - malformed art shouldn't break the stream
            logger.debug("failed to base64-decode PICT artwork", exc_info=True)
            return
        # Decoded on a worker thread: this coroutine shares its loop with SourceFeeder._pump, and
        # iOS sends 1400x1400 PICTs. See sources/artwork.py.
        img = await decode_image(raw, what="PICT artwork")
        if img is None:
            return
        await self._set_artwork(img)
        logger.info("artwork: %dx%d %s", img.width, img.height, img.format or "?")

    async def _set_artwork(self, img: Image.Image | None) -> None:
        role = self._artwork_role()
        if role is not None:
            with contextlib.suppress(Exception):
                await role.set_album_artwork(img)


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
