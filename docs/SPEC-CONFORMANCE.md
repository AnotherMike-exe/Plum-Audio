# Sendspin spec conformance — where Plum-Audio stands

> Audited 2026-07-21 against <https://www.sendspin-audio.com/spec/> and `aiosendspin` 6.0.5, with
> live evidence from the VLAN-7 rig (Music Assistant 2.9.9 + a Home Assistant Voice PE).
> Interop is the reason we build on this protocol, so this file tracks conformance as a first-class
> property, not a footnote. See ARCHITECTURE §8 for the design behind each item.

We speak the protocol from **three** places, and they are not equally mature:

| Speaker | Implementation | Conformance |
|---|---|---|
| Unit **server** | `aiosendspin` server + `sendspin_server.py` | good — library owns the wire format |
| Unit **player** | `aiosendspin` client + `sendspin_player.py` | good — library owns hello/time/state |
| **GUI controller** | hand-written TS (`services/sendspinControllerClient.ts`) | good — client/time + client/state added 2026-07-23; two minor items remain |

The GUI client is hand-written because the browser has no `aiosendspin`. It now implements the
REQUIRED clock sync and state reporting; the remaining items are quality-of-life, not conformance
blockers. Verified on hardware (2026-07-23): the shipped build sends client/hello, continuous
client/time (adaptive cadence), and client/state{synchronized}, and our server replies with
server/time and accepts the state.

---

## Discovery — conformant

| Requirement | Status |
|---|---|
| Players advertise `_sendspin._tcp` (8928), TXT `path` + optional `name` | ✅ `mesh/avahi.py` via the system Avahi |
| Servers advertise `_sendspin-server._tcp` (8927), TXT `path` + `name` | ✅ same |
| Browse both types to find clients/servers | ✅ `mesh/neighbourhood.py` |
| "Do not manually connect to servers if you are advertising `_sendspin._tcp`" | ✅ the player refuses a home-server dial while advertising |

Evidence: Music Assistant discovered our player and dialed it **1.0 s** after it began advertising.

**Deliberate deviation:** the library's own zeroconf stays off (`advertise_addresses=[],
discover_clients=False`, `advertise_mdns=False`) because python-zeroconf binds UDP 5353 against the
host Avahi that AirPlay and Spotify Connect need. We publish the identical records through Avahi
instead — same wire result, one responder per host.

## Connection lifecycle — conformant, with one known gap

| Requirement | Status |
|---|---|
| `client/hello` → `server/hello` handshake | ✅ all three speakers |
| `client/goodbye` reason `another_server` when switching servers | ✅ `sendspin_player.py` |
| Server dials clients with `connection_reason` | ✅ always `playback` (we removed the DISCOVERY tier — ARCHITECTURE §2) |
| **Multi-server arbitration on the client** | ❌ **GAP** |

**The gap:** the spec has the client accept the new handshake, then choose by `connection_reason`
(`playback` beats `discovery`), tie-breaking on the persisted `server_id` of the last server with
`playback_state: playing`. Our player implements only the first branch — it always yields to the
newest dialer — and persists nothing. Plum-to-Plum this is indistinguishable from conformant, since
we only ever dial `playback`. Against a foreign server running a discovery sweep it is wrong: we
would hand over a playing speaker.

Not *cleanly* locally fixable. `SendspinClient.server_info` exposes `connection_reason` only *after*
`attach_websocket`, which refuses a second socket — so "accept both, then decide" cannot be expressed
on the one client. A throwaway second client could peek at `server/hello` and arbitrate before
committing, but it's racy and ugly; not worth shipping over a clean library fix. **Tracked as
upstream ask #1 in [docs/UPSTREAM-AIOSENDSPIN.md](UPSTREAM-AIOSENDSPIN.md).**

Observed live: our unit's boot-time dial took a speaker back off Music Assistant about a minute
after MA claimed it. This is the gap, not a theory.

## Playback state — conformant (fixed 2026-07-21)

`group/update.playback_state` is the only way the protocol says "nothing is playing"; there is no
distinct idle/unrouted state. A stream now exists only while a sender feeds the source: first audio
→ `start_stream()` (`playing`), EOF or `PLUM_SOURCE_IDLE_TIMEOUT` silence → `group.stop()`
(`stopped`, pushed to every client, metadata progress frozen). Note `stop_stream()` deliberately
does NOT announce — it keeps clients logically PLAYING across a handover.

Before this we held a stream from boot and announced `playing` forever, on every source.

## Roles

| Role | Server side | Player | GUI controller |
|---|---|---|---|
| player@v1 | ✅ | ✅ PCM, static delay, volume/mute | n/a |
| metadata@v1 | ✅ emits title/artist/album (+ **album_artist**; Spotify **track**) + progress trio | ✅ consumes (for the self-report) | ✅ consumes; clock-synced |
| artwork@v1 | ✅ 512×512 JPEG, channel 0 | n/a | ✅ declares 1 channel, decodes types 8–11 |
| controller@v1 | ✅ advertises play/pause/next/previous per source; **repeat/shuffle for sources that support it (Spotify)** | n/a | ✅ sends all commands incl. `switch`; renders repeat/shuffle when advertised |
| visualizer@v1 | ✅ computed by the aiosendspin viz role when a viz client is present (library DSP, not ours) | ✅ consumes — incl. from a foreign server it is a group member of (MA) | ✅ decodes spectrum(19)/loudness(16), renders |
| color@v1 | ❌ not implemented | ❌ | ❌ |

**Codec negotiation (confirmed against the spec 2026-08-04).** A client's `supported_formats` is in
**priority order — first is preferred** — and the server activates the first match it implements.
`aiosendspin` does exactly that, and we do not override it: encoding is per-client, so a group can
legitimately carry FLAC to one endpoint and PCM to another. Servers must support `opus`, `flac` and
`pcm`; the spec gives **no** guidance preferring one for constrained players. A player that cannot
sustain its own first choice is expected to renegotiate CLIENT-side via `stream/request-format`
(the spec's stated purpose: adapt to "changing network conditions or CPU constraints"), which the
server answers with a fresh `stream/start` carrying a new `codec_header`.

Sample-rate conversion is the library's job and it does it: our sources are 44.1 kHz (AirPlay
native) and a 48 kHz-only client is served resampled 48 kHz FLAC with no work from us. A
server-side codec override was written and **reverted** (`f19a428`) — the device that motivated it
turned out not to play from Music Assistant either, so it bought nothing and left us deviating from
the client-preference rule for no reason. Do not re-add one without a case that survives a
cross-server check.

**Repeat/shuffle (added 2026-07-25).** Fully wired for **Spotify** (go-librespot exposes it natively):
the source advertises the repeat/shuffle commands on the controller role, honours them
(`/player/repeat_context|repeat_track|shuffle_context`), and publishes state back (`set_repeat`/
`set_shuffle`) so every GUI reflects it; the same state relays over the foreign consume path (MA).
**AirPlay deliberately does not advertise them** — shairport-sync's DACP relay of repeat/shuffle is
unverified, and advertising a command we can't honour is worse than omitting it, so the GUI simply
hides those controls for AirPlay (capability-gated on `supported_commands`). This is the intended
per-source shape, not a gap.

## GUI controller client — the open gaps

| Requirement | Status | Consequence |
|---|---|---|
| `client/time` sent continuously; clock via a filter | ✅ `TimeFilter`, best-of-window min-delay; NTP formula verbatim from the library; adaptive 0.2→3 s cadence | done 2026-07-23 — see note below |
| `client/state` with `state` (REQUIRED) | ✅ sends `{state:'synchronized'}` once the clock settles | done 2026-07-23 |
| Controller `switch` command | ✅ generic `client/command` sends it (all commands MA advertises are sendable) | done |
| Group volume "preserving relative levels" | ✅ the controller `volume`/`mute` command, redistributed by the library | done 2026-08-03 — see note below |
| `stream/request-format` | ❌ | REMAINING — cannot renegotiate artwork size at runtime; we hardcode 512×512 |

The two REQUIRED items (client/time, client/state) are done and hardware-verified, so the GUI is
now spec-safe to point at a third-party server for reading state and issuing whatever commands that
server advertises. The one remaining item is quality-of-life and does not affect conformance.

**Note — group volume was never ours to implement (corrected 2026-08-03).** `ControllerGroupRole`
advertises `[volume, mute, switch]` unconditionally and redistributes a group volume across the
group's players preserving their relative levels (`roles/player/group.py`), republishing the average
as `controller.volume`. The earlier "naive per-stream volume" reading was wrong about the library —
what we actually lacked was the other half of the loop: **`PlayerV1Role.set_volume()` does not
update the server's view of a player**; only the player's own `client/state` does, and the client
library sends exactly one, at connect, carrying `initial_volume`. Our player never re-reported, so
every level in the mesh (and the redistribution baseline) sat at 100 forever. The player now echoes
after each command and persists its level. **Any player implementation must do this** — a server
cannot read a speaker's volume, only command it.

**Note — the GUI clock filter is display-grade.** `TimeFilter` is a minimum-delay NTP filter (the
offset/delay formulas match the library verbatim), **not** the library's Kalman filter with a drift
term. That is correct for a *controller*: it only advances a progress bar and re-anchors from
metadata ~1×/s, so residual drift is invisible. It would **not** be adequate for a *player* — but
players use `aiosendspin`'s real filter, never this one. Do not reuse `TimeFilter` for audio timing.

**Polish shipped 2026-07-25.** `client/hello` now carries `device_info` (player and GUI), so a
foreign controller shows a real identity; the AirPlay reader emits `album_artist` (DMAP `asaa`, a
text field — the binary numeric fields year/track are still skipped) and Spotify emits the `track`
number.

## What we learned about Music Assistant (2.9.9)

- Accepts a controller-role client, places it in a group, pushes group + metadata + controller
  state. Reports its own session as `connection_reason=discovery`.
- **Observe/control a foreign session by being a group MEMBER, not a fresh controller.** A
  controller that merely connects lands in an isolated solo group (below). But our PLAYER, as the
  renderer, is already a member of the playing group — and to a member MA emits the FULL controller
  command set (play/pause/next/previous/stop/volume/mute/repeat/shuffle), metadata, AND the
  visualizer role (256-bin spectrum + loudness), and honors transport commands sent back. This is
  how Plum observes/controls/visualizes MA-served audio (commit 6148204): the player negotiates
  PLAYER+METADATA+CONTROLLER+VISUALIZER+ARTWORK and relays to the GUI (album art included). Fully spec-native at the MA boundary.
- **A freshly connected CONTROLLER (not a member) lands in its OWN solo group, not the session.**
  Resolved 2026-07-23 with MA actively streaming to our player: our player was in MA group
  `af2c0caf…` (`playing`, "1 Last Cigarette") while a controller connecting at the same moment
  landed in a different group `d1c40416…` (`stopped`), advertising only
  `supported_commands = [volume, mute, switch]`. So the earlier "no transport" was **not**
  state-dependent — a controller simply is not placed in the playing group, so it can neither see
  nor drive the active session.
- **Consequence, corrected**: an earlier note here said remote-controlling MA "is not achievable" —
  that was for a fresh controller. Via the player-as-member path above, we DO fully observe,
  control and visualize MA-served audio over standard Sendspin roles. What remains outside Sendspin
  is MA's library/browse/queue surface (its own Home Assistant API) — the protocol has no such
  concept, as expected.
- Discovers players by mDNS `_sendspin._tcp`, with manual IP entry as a fallback.

## Deliberate deviations (not gaps)

1. **No DISCOVERY-tier dialing.** A client holds one websocket, so a discovery dial cannot warm a
   player — it steals it. Removed; the roam is inaudible without it (ARCHITECTURE §2).
2. **Library mDNS off, Avahi on.** Above.
3. **The mesh API and discovery beacon are ours, not the protocol's.** Sendspin has no
   server-to-server anything; cross-unit topology and routing are outside its scope.
