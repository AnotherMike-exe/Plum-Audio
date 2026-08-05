# Hard-Won Lessons

> **Purpose**: why the code is shaped the way it is — the failures that produced each rule and the
> measurements that settled them. Read this before "simplifying" anything, and before investigating
> something that smells like it may already have been chased. This is **not** a troubleshooting KB:
> no symptom→fix index, only reasoning and evidence. Operational recipes live in `docs/OPERATIONS.md`,
> host provisioning in `docs/HOST-PROVISIONING.md`, current design in `docs/ARCHITECTURE.md`, and
> what shipped when in `docs/PHASE-HISTORY.md`.
>
> Every measured number and date below is load-bearing — they cost hardware time and are not
> re-derivable from the code or the commit log. Do not trim them.

---

## Do not re-investigate

**The Home Assistant Voice PE does not render our audio, and it is not our bug.** It discovers us,
joins a group, acknowledges our `stream/start` codec header, reports `PLAYING` — and plays nothing.
It also plays nothing from **Music Assistant**, under both FLAC and PCM. Device-side. Chased at
length 2026-08-04. **Play the device from MA first** before spending a minute blaming our server.

**Server-side codec override: written, then reverted (`f19a428`).** The spec says a client's
`supported_formats` is in priority order and the server takes the first match it implements;
aiosendspin does exactly that, and a player that cannot sustain its own choice renegotiates with
`stream/request-format`. The override existed only for the Voice PE above. Do not re-add it without
a live, proven case — per-client encoding means a heterogeneous group is normal, not a problem.

**DISCOVERY pre-connect is impossible, not merely unnecessary** (see Mesh & routing). Refuted on
hardware 2026-07-14, removed in `de50035`.

**spotifyd is not a candidate.** 0.4.x dropped standard MPRIS (only TransferPlayback and volume
remain) and has no arm64 build that keeps it. go-librespot ships native arm64 with richer metadata
and transport over a loopback HTTP+WS API, and no D-Bus at all.

**Absolute seek toward the phone over Bluetooth is impossible at any AVRCP version.** No such
command exists — only press-and-hold FF/REW (`MediaPlayer1.Hold(0x49/0x48)` + `Release()`), a coarse
seek we do not expose. Position *reporting* was fixable and is fixed (below); seek is not.

**Bluetooth cover art cannot appear for the track already playing when a session opens.** BlueZ
issues `GetElementAttributes` (the call that requests art) only on a *track change*, and no D-Bus
method triggers a re-query, so the phone is never re-asked. Art lands on the first track change.
Closing it needs a third `bluetoothd` patch exposing a metadata re-query.

**Client-side multi-server arbitration is an upstream gap.** `SendspinClient.server_info` exposes
`connection_reason` only *after* `attach_websocket`, which refuses a second socket — so the spec's
"accept both handshakes, then decide" cannot be written locally. Our player always yields to the
newest dialer; it now **persists the `server_id` of whoever most recently had it playing**
(`player_state.json`), which is the storage half of the MUST, but it cannot yet act on it.
Harmless in a Plum-only mesh, where we only ever dial `playback`. Tracked in `docs/UPSTREAM-AIOSENDSPIN.md`.

**Alpine.** The base is `python:3.13-slim-trixie` (glibc) deliberately — glibc makes PyAV /
PortAudio / numpy wheels trivial and removes the Alpine packaging pain Plum-Snapcast had. Trixie
specifically, because two integrations depend on what that release ships; see
`docs/HOST-PROVISIONING.md`.

---

## Audio output / PortAudio

**PortAudio is not ALSA, and availability cannot be probed by opening.** `sounddevice`'s `device=`
matches a substring of PortAudio's **own** name list, so an arbitrary ALSA PCM string is rejected;
the `(hw:C,D)` suffix PortAudio embeds in its names is the only join between the two namespaces.
Worse, **PortAudio enumerates by opening** — a card held exclusively disappears from
`query_devices()`. With our own player holding the Amp100's single-subdevice pcm512x, the output list
came back **empty**. Availability therefore rests on three signals that fail in different places and
must never be collapsed into one: `is_active` (what we are configured to render to — survives
everywhere, including a container with `/proc/asound` masked), `in_use`
(`/proc/asound/.../sub*/status`), and PortAudio exposure.
See: `backend/scripts/audio_devices.py`.

**Two threads re-initialising PortAudio at once SIGSEGVs the interpreter — silently.**
`sd._terminate()` / `sd._initialize()` rebuild **process-global** state. No exception, no traceback,
just a dead process. The GUI fetches the device list and the current output in one `Promise.all`,
Flask runs `threaded=True`, and the config API crash-looped on exactly that. `_portaudio_outputs()`
holds a module lock and a 2 s TTL cache; the player passes `force=True` because it re-reads
immediately after closing its stream. **Sequential `curl` cannot reproduce this** — the reproduction
has to be concurrent.
See: `backend/scripts/audio_devices.py::_portaudio_outputs`.

**`/proc/asound` is masked in the container and runc refuses to bind anything back into `/proc`.**
Compose mounts the host copy at `/host/asound` (`PLUM_PROC_ASOUND`). Read from inside the container,
`owner_pid` is **0** — different PID namespace — so only `closed` versus the presence of a state
block is trustworthy; the pid is noise. And every subdevice must be checked, not `sub0`: bcm2835 has
eight.
See: `backend/scripts/audio_devices.py` (`PROC_ASOUND`, the `sub*/status` walk).

**Card numbers move; the ALSA card *name* does not.** The HiFiBerry on `.7.204` was card 2, then 1,
then 2, then 0 across four reboots with the config unchanged. Snapcast persisted the number and got
away with it only because `get-settings.py` translated to `default:CARD=<name>` at launch — but from
the **stale** number, so a reboot that renumbered would have resolved to the wrong card. The bug was
latent there, not absent.

**A player must echo the output it actually opened, the same contract as the volume echo.**
`/data/player_state.json` carries `output_device`; the config API compares it against the choice in
`settings.json` to report `pending`. Without the echo the GUI marks a switch applied the moment it is
*saved*, including switches that never opened anything. A failed switch **restores** the previous
device rather than leaving silence — measured at **42 ms, still playing**.
See: `backend/scripts/sendspin_player.py`, `backend/scripts/player_state.py`.

**A HAT's hardware mixer is not at unity, and nothing in Plum-Audio can see that it isn't.** An
Amp100 comes up at `Digital` **163/207 — i.e. -22 dB** — and `alsa-restore` reinstates that every
boot. Our volume is software gain in the PortAudio callback, so the loss is invisible to every level
the GUI shows. Snapcast never hit this because snapclient owned the control via `--mixer hardware:`.
Related: the `dtoverlay` block must go **before** the first existing `dtoverlay=` line — appending it
after `vc4-kms-v3d` costs an HDMI audio output, measured over 5 boots. Both are host provisioning:
see `docs/HOST-PROVISIONING.md` and `scripts/host-setup/configure-audio-hat.sh --unity`.

---

## Bluetooth

**The position/seek ceiling is in `bluetoothd`, not in our relay, the GUI, or the metadata role.**
Three separate hunts searched our own code first. `avrcp.c: avrcp_register_notification()` registers
`EVENT_PLAYBACK_POS_CHANGED` with an interval of `UINT32_MAX / 1000` — **49.7 days** — commented "as
we only use it to resync". AVRCP 1.5 §6.7.2 trigger condition 1 (registered interval reached) can
therefore never fire, leaving play-status change, track change and end/start of track: exactly the
"position only ever arrives bundled with something else" pattern seen on hardware. It also kills
**seek detection**, because targets size their jump-detection window from that same interval (AOSP
notifies when position leaves `[pos ± interval]`), so an in-track scrub reads as no change at all.

There is no fallback. `GetPlayStatus` (PDU 0x30, the only *measured* position) is issued only from
the GetCapabilities response, a status change, a track change and the media-player-list parse; no
D-Bus method triggers one. `MediaPlayer1.Position` is a local wall-clock interpolation from the last
notification, **unclamped** — which is how a track of 400346 ms reported **454520 ms**. That number
is the tell: an interpolation running past the end of its own track.

Fixed by patching the host `bluetoothd` (two DEP-3 patches; the distro package is rebuilt at
`<version>+plumN` so Raspberry Pi's `+rptN` patches survive). Patch 1 polls `GetPlayStatus` every 2 s
while playing — it works even against a target that never advertises event 0x05, which iOS commonly
does not, and it corrects the interpolation drift. Patch 2 registers position-changed with a 1 s
interval, restoring 1 Hz push ticks and ~1 s jump detection on targets that *do* advertise 0x05; it
is last in series because it is the droppable half. Scrubs land in **~2 s**, verified. Nothing in our
Python depends on the patches — `_apply_position_signal` compares an incoming position against our
own anchor, so extra re-reads are discarded and an unpatched unit behaves as before, minus scrub
reporting.
See: `backend/config/bluez/`, `backend/scripts/sources/bluetooth_avrcp.py`; provisioning in
`docs/HOST-PROVISIONING.md`.

**Cover art dies three silent ways, and "no art and no errors" means we never asked.** Art rides a
separate OBEX (BIP) conversation: `MediaPlayer1.ObexPort` is the phone's L2CAP PSM, `Track.ImgHandle`
names the image, and we fetch over a private per-endpoint obexd. All three failures look identical
from outside — a stale image or none, with **nothing in the log**, because nothing was attempted.

1. *A replaced `bluetoothd` gives no teardown event.* Its objects do not depart with
   `InterfacesRemoved`; the service just vanishes. The relay sees no "player gone", the cached
   session path is never invalidated, and `prepare()` early-returns forever. The rebuild is keyed off
   the player **BIND** — the one event that reliably follows any disruption — and only one rebuild
   runs at a time, because binds arrive in bursts.
2. *`ObexPort` is never signalled.* BlueZ fills it from the AVRCP SDP record, which routinely lands
   *after* the player object is exported, and `media_player_set_obex_port()` is the one setter in
   `player.c` with no `g_dbus_emit_property_changed`. `obexport_exists()` also hides the property
   while it is 0, so an early `GetAll` shows no key and no signal follows. It must be polled after a
   bind.
3. *Our own obexd starts after the bind* — measured at **10 s on `.201.113`**. Losing that race is
   permanent, not transient, so `prepare()` is retried for ~30 s.

Underneath all three: **a phone publishes `ImgHandle` only while a BIP session exists.** No session,
no handle, no fetch, no session. Two device-side facts worth not re-deriving: the handle is **not a
track identity** (iOS reuses one value, so fetches key on handle *plus* track), and a phone serves
**one BIP session at a time**, so the distro's D-Bus-activated user `obexd` steals the channel and
ours is refused `ECONNREFUSED` — hence `systemctl --user mask obex.service`.

*Stale art is worse than none.* A track change with no art clears the artwork role after a short
grace period rather than leaving the last album's cover under the new title (handles arrive late, and
BlueZ re-sends partial `Track` dicts). The GUI defaults `albumArtUrl` to an inline SVG placeholder
for the same class of reason — an empty `src` drew the browser's broken-image glyph on every
reconnect and hard refresh.
See: `backend/scripts/sources/bluetooth_coverart.py`, `frontend/services/albumArtPlaceholder.ts`.

**A missing host D-Bus policy costs 178 restarts and buries every unrelated diagnosis.** Without
`backend/config/bluealsa-plum-dbus.conf` at `/etc/dbus-1/system.d/`, `bluealsa` cannot acquire
`org.bluealsa` and exits `rc=1` about **3 s** after every start; the source manager respawns it
forever. On `.7.204` that was **178 restarts and a new dbus-daemon every 9.5 s**. The real damage was
not the churn but the log volume: a crash-looping source writes fast enough to push the lines you
need thousands back, and a filtered `tail` then reads as "this never happened". **Two wrong diagnoses
on 2026-08-04 came from exactly that.**

---

## mDNS & naming

**Avahi's `SetHostName` reports success as failure and failure as success.** Two verified behaviours,
both of which the code must handle rather than trust:

- Setting the name it already has raises **"invalid because redundant"** — a no-op, not an error.
- A *real* change makes Avahi reset and **drop the D-Bus connection mid-call**. The reply never
  arrives and dbus-next raises "Message recipient disconnected", so a **successful** set surfaces as
  an error. Always reconnect and read the name back rather than believing either outcome.

It is runtime state: a host reboot reverts it, and we deliberately do **not** re-apply on boot — every
unit ships with the same default hostname, so replaying it would collide all four units onto one
name. And it goes through the **host** bus: there is no `avahi` program in our supervisord, so
writing `/etc/avahi` or restarting a service is not an option that exists.
See: `backend/scripts/apis/settings_api.py` (~L418-475).

**A speaker has two names, and which one you see depends on where it is.** Attached, it lives in its
server's `players` under the name it declared at the Sendspin handshake ("Home Assistant Voice
PE - 01"). Idle, no server holds it, so the only trace is its mDNS advertisement — and a third-party
device usually publishes no `name` TXT key, leaving the bare instance name
("home-assistant-voice-a1b2c3"). One device therefore read as two and appeared to rename itself on
every join and leave. The two views share exactly **one** identifier: the **listener URL**. Not the
client id — mDNS names by instance, the handshake by MAC, the same asymmetry that forces `adopt` to
match by URL. The protocol name is memoised against the URL and **persisted to localStorage**,
because "idle at page load" is the common case and an in-memory memo would still flip on the first
reload.
See: `frontend/services/sendspinDataService.ts` (~L300).

**Avahi resolves once per interface *and* family.** One player arrives as loopback, link-local v6,
`docker0` and the real LAN address. Addresses are merged per instance and ranked — a `docker0`-only
advertisement is what made spotifyd unreachable earlier. Also, `ServiceBrowserNew` announces before
D-Bus signal handlers can attach, so cached entries were silently missed until we moved to Server2's
`ServiceBrowserPrepare` / `Start` pair.
See: `backend/scripts/mesh/avahi.py`, `backend/scripts/mesh/neighbourhood.py`.

---

## GUI

**Themed scrollbars need `color-scheme`, not just `::-webkit-scrollbar` — and the two standards must
not be combined.** Two independent mechanisms. The pseudo-elements style a *persistent* scrollbar,
but an **overlay** scrollbar (macOS's default unless "show scroll bars: always" is set) cannot be
reached by CSS at all — only `color-scheme` makes the browser draw its own chrome dark. That is
precisely why the page looked correct while the Settings overlay showed a white bar. The standard
`scrollbar-width` / `scrollbar-color` pair is then fenced behind a **Firefox-only `@supports` on
purpose**: Chromium **ignores every `::-webkit-scrollbar` rule** for any element whose
`scrollbar-color` is not `auto`, so setting both unconditionally silently discards the styling you
just wrote.
See: `frontend/index.html` (the inline theme block).

**A per-device stream picker must list only ACTIVE sources.** A source exists for every configured
endpoint whether or not a sender feeds it, so the unfiltered list showed Bluetooth, Spotify and
AirPlay long after the phone disconnected — "ghost sources". They were handed the full stream set on
the theory that a peer parked on an idle source must stay reachable; that theory was wrong about our
own code, because `viewClients` already reports such a peer as idle. Routing to an idle source is
legal and silent, which is exactly why the stale entries read as ghosts rather than as errors. The
top picker keeps the featured stream as well, so a source going idle cannot yank the selection out
from under the user.
See: `frontend/MeshApp.tsx` (`stableRoutable`), `frontend/components/StreamPickerButton.tsx`.

**Idle devices were always routable — only the GUI was missing.** `route_player` reclaims a player
that is in no unit's group via the listener URL from its own unit's self-report, and delegates when
the source lives on a peer. The router supported this from the start. The GUI offered only "Join
Stream", which requires a stream the current page is already on, so an **idle unit's page could route
nothing at all**. Every device row now carries the same picker; Join Stream stays as the one-click
case. History, not a rule.
See: `backend/scripts/mesh/router.py::route_player`.

**Two visualizer bugs that only appear on hardware.** The canvas must read frames **inside** its
render loop via a ref, not through React state — a per-frame `setState` cancels the render rAF and
the canvas goes blank. And `calculateBarHeights`, ported from Plum-Snapcast where it consumed a raw
linear WebAudio FFT, sliced our already-log-binned spectrum wrong and rendered nothing visible; it
was replaced with a direct pre-binned-spectrum→bars mapping. The port was a **rewire of the data
source**, not a rebuild, and both bugs live in the seam between the two.
See: `frontend/components/Visualizer.tsx`, `frontend/components/AmorphousBlob.tsx`.

---

## Mesh & routing

**DISCOVERY pre-connect was a source-reading error, refuted on hardware 2026-07-14.** The design
audit read `ConnectionReason.{DISCOVERY, PLAYBACK}` as a two-tier presence model: hold a player in a
lightweight idle connection, upgrade it when audio routes there. It cannot work. A `SendspinClient`
holds exactly **one** websocket (`attach_websocket` raises if already connected), so a playing player
cannot be warmed on a second server. Worse, `connection_reason` is reported only in `server/hello`,
which the client sees *after* it has already attached — so a player cannot even decline a DISCOVERY
dial while busy; it would have surrendered its current server to be told why it was called. Removed
in `de50035`.

It was also **unnecessary**, which is the part worth keeping. A roam fires no `stream_clear` or
`stream_end`, so the player's jitter buffer is never flushed and the DAC drains straight through the
reconnect: **~300 ms of buffer against a ~25-55 ms reconnect, ~6× headroom.** Instrumented with an
unconditional emitted-silence counter (`pad_ms`), the measurement across a cross-server roam is
`pad_ms` **unchanged** — zero emitted silence. Confirmed against real bursty content, not just a
tone: player-113 detached holding **435 ms of live AirPlay audio** and reattached with the buffer
intact. The earlier "~200 ms audible silence" estimate was simply wrong. The design consequence lives
in ARCHITECTURE §2-3; this is how it was settled.

**Adding a player to a live stream does not put it in that stream — and nothing anywhere says so.** A
stream's client set is fixed at `start_stream()`. A client that **connects** while a stream is live is
handed it during the handshake; one already connected and then added to the group is **not**. It sits
in the group, in the GUI, at the right volume, and silent, with nothing in any log. Measured on
`.7.204`: with `airplay-1` streaming, unrouting and re-routing the attached local player produced no
second `Stream started` on the client and a renderer buffer that never left **0 ms**.

The spike missed it because it measured re-route against an *idle-then-started* source and reported a
clean live re-route. Roaming hides it because a cross-server roam reconnects (`ANOTHER_SERVER`) and a
reconnect gets the stream for free — so only tier 1, the path advertised as seamless, was ever
affected. It is also why every manual workaround was "unjoin it and rejoin it": that forces the
reconnect. **Do not optimise `refresh_stream()` away because a roam test passes.** This was live for
months and looked like a dead speaker.
See: `backend/scripts/mesh/router.py::attach_player`, `SourceFeeder.refresh_stream`.

**Re-route must be `remove_client` then `add_client`, in that order.** A bare `add_client` calls
`old_group.stop()`, which kills the source the player is *leaving* for every other listener. Each
source group therefore keeps a stable anchor member, and `SourceFeeder` re-acquires its `PushStream`
on `StreamStoppedError` rather than dying.

**`connect_to_client(url)` is a NO-OP when a dial registration for that URL already exists.** A second
`adopt` of a speaker that went away — rebooted, or reclaimed by its own server — silently does nothing
and then times out reporting "never connected" about a device whose port is plainly open.
`adopt_foreign_client` cancels the existing dial first. Identify the speaker by its **registered
URL**, never by "a client id that was not in the set before": that heuristic only holds the first
time, and the GUI passes an mDNS name while the handshake id is a MAC.

**Releasing a foreign speaker takes four steps, and the first three each look sufficient.** Detach
from the group, cancel our dial, **close the live websocket**, forget the registry entry. Stopping
after any of the first three leaves the speaker ESTABLISHed to us and out of reach of its own server.
Only `SendspinConnection.disconnect()` actually hangs up, and 6.0.5 exposes it solely through a
private attribute. Hardware settled this; no amount of reading did.

**A server cannot read a player's volume, only command it.** `PlayerV1Role.set_volume()` only *sends*.
The server's own view moves solely on `client/state`, of which the client library emits exactly one —
at connect, carrying `initial_volume`. Skip the echo and every level in the mesh reads **100% forever**
while the audio is demonstrably quieter. **The failure looks like a GUI bug and is not one.** The
player must also persist the level to `/data/player_state.json` — not `settings.json`, which a
different process owns — so a restart does not reset the room.
See: `backend/scripts/sendspin_player.py::_publish_render_state`.

**A loopback player URL makes a roam fail silently.** A peer advertising its player as
`ws://127.0.0.1:8928` made cross-server reclaim dial *our own* loopback. The router now substitutes
the unit's beacon host, but rigs should still be configured with a LAN player URL — the same class of
error as the container entrypoint's `127.0.0.1` default, where peers reclaim a player **by the URL it
registers** and a loopback default advertises an endpoint no peer can reach.

**The metadata role stores ONE progress anchor and clients extrapolate from its timestamp.** A bare
`playback_speed` flip re-stamps a *stale* anchor and the timeline jumps, so play/pause must re-anchor
to the daemon's real position. Latent since Phase 2; a second source exposed it.
