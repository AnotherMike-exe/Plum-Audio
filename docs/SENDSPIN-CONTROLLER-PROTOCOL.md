# Sendspin controller/metadata/artwork wire format

> The hand-audited protocol the GUI implements (`frontend/services/sendspinControllerClient.ts`).
> Audited against aiosendspin 6.0.5 and re-verified against the code on 2026-08-05. Read it before
> touching the controller client or the now-playing card — this level of detail is **not** in the
> published spec.

## Connecting

A plain WebSocket to a unit's Sendspin **server** at `ws://<unit-ip>:8927`, first message
`client/hello` advertising `["controller@v1","metadata@v1","artwork@v1"]` plus an
`artwork@v1_support` object (`{source:'album', format:'jpeg', media_width:512, media_height:512}`).
Never request `player@v1` — that pulls the audio stream.

`visualizer@v1` **is** requested when the GUI wants spectrum, with a `visualizer@v1_support` object
(`buffer_capacity: 65536`, `rate_max: 30`, `types: ['spectrum','loudness']`, 256 log-spaced bins
from 40 Hz to 16 kHz). The server computes the DSP per source.

**One socket per SOURCE, not per unit.** The client id is `ctrl:<source_id>:<nonce>` and the `ctrl:`
prefix is what tells the unit which source group to join us to — a controller sees only its own
group's state. The server pushes current state immediately on join; there is no polling.

Nothing goes on the wire between `client/hello` and `server/hello`. The spec is explicit, and
aiosendspin's tolerance of an early `client/time` is why violating it never showed on the rig.
After `server/hello`: send `client/state` (`{state: 'synchronized'}`, no player payload) and start
the clock loop.

## Messages

| Direction | Message | Carries |
|---|---|---|
| ← | `server/state` | `payload.metadata` (diffs) · `payload.controller` |
| ← | `group/update` | `payload.playback_state`, `group_id`, `group_name` — authoritative transport state |
| ← | `stream/end`, `stream/clear` | audio stopped/flushed; the **track did not change** |
| ← | binary frames | artwork, spectrum, loudness |
| → | `client/command` | `payload.controller = { command, volume?, mute? }` |
| → | `client/time` | `{client_transmitted}` → `server/time` reply, continuously |

**Metadata is a diff**: key omitted = unchanged, explicit `null` = cleared. Fields: `timestamp`
(server µs), `title`, `artist`, `album`, `artwork_url`, and `progress { track_progress /*ms*/,
track_duration /*ms; 0 = live*/, playback_speed /*×1000; 0 = paused*/ }`.

**Controller state**: `supported_commands: MediaCommand[]`, `volume` (0-100), `muted`, `repeat`
(`off|one|all`), `shuffle`. Drive button enablement from `supported_commands` — the backend
advertises `play/pause/next/previous` for any source with a transport remote, and adds
`repeat_*`/`shuffle`/`unshuffle` for Spotify (`sendspin_server._supported_commands_for`).

**Position is never pushed periodically.** Extrapolate:
`cur_ms = track_progress + (now_server_us − timestamp) · playback_speed / 1e6`, clamped to
`[0, track_duration]`, halted when `playback_speed == 0` or the state is not `playing`.
`now_server_us` comes from the clock filter, not `Date.now()`.

**Clock sync is REQUIRED**, not an optimisation: every progress timestamp rides the server's
monotonic clock, and a strict server may treat a controller that never sends `client/time` as not
operational. The client runs the classic NTP minimum filter — an 8-sample window, the sample with
the lowest one-way delay wins — with formulas verbatim from aiosendspin's `_handle_server_time`.

## Artwork and visualizer binary framing

Every binary frame is `[type:1][ts_us:8 big-endian][payload]` — a 9-byte header, so a frame shorter
than 9 bytes is discarded. `type` selects the role:

| `type` | Role |
|---|---|
| 8-11 | artwork channels 0-3 — **channel 0 is type 8**, and the only one we request (album, 512×512 JPEG) |
| 16 | loudness |
| 19 | spectrum (uint16 per bin, scaled to 0-255 for display) |

**An empty artwork payload means clear.** A non-empty one becomes
`Blob([bytes], {type:'image/jpeg'})` → `URL.createObjectURL`; the previous object URL is revoked on
replacement and on close. `metadata.artwork_url` is a separate *text* pointer — our AirPlay and
Bluetooth paths deliver art as binary, so the binary object URL wins when both are present.

`stream/end` / `stream/clear` must **not** revoke the artwork. The track has not changed, so
clearing there blanks the cover on every pause, idle and roam until the next binary frame — which
only arrives on a track change. Only the visualizer falls to rest there.

## Seek — verified 2026-08-05: there is still no seek command

The `⚠ NO SEEK` warning carried by the old FRONTEND-PORT notes **still holds**, and the Bluetooth AVRCP
position work does not change it. Evidence:

- `ControllerCommand` (`sendspinControllerClient.ts:55`) is exactly
  `play · pause · stop · next · previous · volume · mute · repeat_off · repeat_one · repeat_all ·
  shuffle · unshuffle · switch`.
- `sendspin_server._supported_commands_for` builds from `MediaCommand.PLAY/PAUSE/NEXT/PREVIOUS`
  (+ repeat/shuffle for Spotify). No seek is ever advertised, so no seek could ever be dispatched.
- Every `seek` in the backend is **inbound**: `bluetooth_avrcp._apply_position_signal` re-anchors
  our progress from a scrub the *phone* performed, and `spotify_golibrespot` handles go-librespot's
  `seek` **event**. Both are the source telling us where it moved to. Neither is a command we can
  send.

The AVRCP patches in `backend/config/bluez/` made the *inbound* direction work — a scrub on the
phone now reaches the GUI within ~2 s, where before it could not arrive at all. Outbound absolute
seek toward a phone is separately impossible: AVRCP has no such command at any version, only
press-and-hold FF/REW (`MediaPlayer1.Hold(0x49/0x48)` + `Release()`), which we do not expose.

**The scrub bar is therefore read-only** — it displays the extrapolated position and cannot seek. A
product decision is still needed if seeking is required.

## Commands

`client/command` → `payload.controller = { command, volume?, mute? }`. `volume` requires `volume`
(clamped 0-100); `mute` requires `mute`. A command sent while the socket is mid-reconnect is
**queued, not dropped** (max 8, discarded after 5 s as stale intent) and flushed ~600 ms after
reconnect — long enough for the server to re-group us, since a command issued while still in our
solo group would go nowhere.

Note the volume here is the **group** volume for the source, one of the three levels in the volume
model. Per-player volume and source volume ride different paths entirely — see CLAUDE.md.
