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

**Server-side codec override: written, then reverted (`0d7c6ab`).** The spec says a client's
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

**Card numbers move; the ALSA card *name* does not.** The HiFiBerry on `.100.21` was card 2, then 1,
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
3. *Our own obexd starts after the bind* — measured at **10 s on `.2.11`**. Losing that race is
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
forever. On `.100.21` that was **178 restarts and a new dbus-daemon every 9.5 s**. The real damage was
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
match by URL.

The memo was first built in the GUI and **persisted to localStorage** — right instinct ("idle at page
load" is the common case, so an in-memory memo would flip on the first reload), wrong place. Being
per-browser and per-origin, it was blank until *that* tab had watched *that* speaker attach, so a
fresh browser's first idle sighting still showed the technical name, and two units' GUIs could
disagree. Reported from the rig on 2026-08-05 and briefly mistaken for a regression: the memo was
working, it had simply never seen the name.

The name belongs to the speaker, not to whoever is looking. It now lives on the unit
(`speaker_names.py`, beside `player_state.json`), learned by the server whenever a speaker is
attached and served from `/api/mesh/neighbourhood` — so one observation by anything serves every
browser, and the GUI needed no change because it already prefers `friendly_name`.
See: `backend/scripts/speaker_names.py`, `frontend/services/sendspinDataService.ts` (~L300).

**Avahi resolves once per interface *and* family.** One player arrives as loopback, link-local v6,
`docker0` and the real LAN address. Addresses are merged per instance and ranked — a `docker0`-only
advertisement is what made spotifyd unreachable earlier. Also, `ServiceBrowserNew` announces before
D-Bus signal handlers can attach, so cached entries were silently missed until we moved to Server2's
`ServiceBrowserPrepare` / `Start` pair.
See: `backend/scripts/mesh/avahi.py`, `backend/scripts/mesh/neighbourhood.py`.

---

## GUI

**An unproxied `/api/` path answers 200 with index.html, so the GUI fails as "still loading"
(2026-09-08).** `about_api.py` shipped with a Flask route, a service, a typed response and two
callers, and never had an nginx `location`. The request therefore fell through to `location /`, whose
`try_files ... /index.html` is what makes the SPA's client-side routing work — so `response.ok` was
**true**, `response.json()` threw on the HTML, and both the About panel and the page footer sat in
their empty state permanently. It reads exactly like a slow or broken backend, and `curl` on `:5002`
answers perfectly, which sends you looking at the API. **Any new API prefix needs a block in
`backend/nginx/plum-audio.conf`** — check there first when a panel will not populate, and test the
proxied port, not the origin.

**CI published every image with the Dockerfile's placeholder version.** `docker/build.sh` derives
`PLUM_APP_VERSION` from `git describe` and passes it as a build-arg, but neither `dev.yml` nor
`release.yml` did — so the ARG defaults won and every `:dev` AND every tagged release reported
`0.0.0-dev` / `gitDescribe: unknown`. Doubly hidden: nginx was swallowing the endpoint anyway (above),
and `actions/checkout` is shallow by default, so even adding the build-arg without `fetch-depth: 0`
would have stamped "unknown". A release is the tag; a dev build is the last tag plus `-dev`.

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
tone: player-211 detached holding **435 ms of live AirPlay audio** and reattached with the buffer
intact. The earlier "~200 ms audible silence" estimate was simply wrong. The design consequence lives
in ARCHITECTURE §2-3; this is how it was settled.

**Adding a player to a live stream does not put it in that stream — and nothing anywhere says so.** A
stream's client set is fixed at `start_stream()`. A client that **connects** while a stream is live is
handed it during the handshake; one already connected and then added to the group is **not**. It sits
in the group, in the GUI, at the right volume, and silent, with nothing in any log. Measured on
`.100.21`: with `airplay-1` streaming, unrouting and re-routing the attached local player produced no
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

**Releasing the idle player handed our speakers to Music Assistant permanently (2026-09-07).**
`b1c1fe0` shipped half a policy. It made `_go_idle` and `detach_player` release the unit's own
player, so a foreign server could finally claim it, on the stated premise that "routing, follow and
`autoSwitch.localActivity` all reach an unattached player through `mesh.router`'s idle-speaker
fallback". The premise never held on a VLAN with MA on it: **MA re-dials a released speaker within
seconds and then parks a silent websocket on it indefinitely**, so the player is never *unattached*
again. Measured on `.7.200` (fresh deploy, `XLR-Pro`): one MA connection held **87,111 s — 24 h —
with `audio_flowing=False`**, and when it finally let go a *second* MA instance on the same host
took the speaker back **17 s later**.

`follow._player_status` read that as `(False, None)` — "busy, but nothing we can route onto" — which
is right for a *leader* (there is no `source_id` of ours to follow) and wrong for our own player,
because `idle` is also what gates `localActivity` at `follow.py:204`. So the setting was on, the
source went active, and nothing happened: `[airplay-1] active: sender feeding us` followed 300 s
later by `[airplay-1] idle: no audio for 300s (... detached 0 player(s))`, with **no `follow:` line
in any of four rotated logs**. It looked like a broken toggle and was a disarmed trigger.

Nothing was missing from the mechanism. `Router.route_player` already wins the speaker back:
`view.find_player` misses (MA is not one of our units), so it takes the idle-speaker fallback, reads
the URL from our own `local_player`, and calls `reclaim_remote_player(stage_pairing=True)` —
`GoodbyeReason.ANOTHER_SERVER`, proven live on `.7.200` at 2026-09-06 19:02. A manual GUI route
therefore always worked, which is exactly why this read as a settings bug.

The fix is the gate, not the mechanism: the foreign branch returns `(not lp["playing"], None)`.
`playing` is the player's audio-flow truth (driven by `stream_start`/`stream_end` with a 1.5 s stall
net) and deliberately **not** the foreign server's `playback_state`, which MA reports unreliably to a
member player. Two properties are load-bearing and both have tests:

- while MA really is feeding the speaker, `playing` is True and we leave it alone — that is what
  lets a user push an MA stream to this endpoint mid-AirPlay and keep it, with no further input;
- if MA takes the speaker while our source is already streaming, there is no rising edge, so we do
  not grab it back. Without that, two servers would trade the one websocket a client allows.

Both limits are accepted, not overlooked: a **paused** MA reads as parked, so a fresh AirPlay
connection does take the room; and a speaker lost mid-AirPlay stays lost until the next connection.

## Connection lifecycle & identity (2026-08)

Moved here from `docs/CLAUDE.md` on 2026-08-13 when that file was trimmed back toward its own
~280-line budget. The rules these produced are still stated there; the evidence is here.

**The immortal dialer: `disconnect_from_client()` does not stop the dial.** Found 2026-08-10 bringing
up an Esparagus HiFi board and a FutureProof Satellite1 on unit-7204 — routing a source at either
played for a few seconds, dropped back to idle, and got *worse* on retry. The cancel is swallowed:
`SendspinConnection._handle_client` awaited the message loop as a separate task and
`_run_message_loop` caught `CancelledError` and returned normally, so the dialer saw a clean session
end, backed off ~1 s and reconnected. A session lasting ≥10 s reset the backoff, so against a real
speaker it never reached the ceiling that would end it. Meanwhile the caller's next
`connect_to_client(url)` had already installed its own task, and the doomed task's `finally` popped
**the new task's** registry entry — so the next call missed the "already dialling" guard and opened a
third. Measured with a fake speaker counting sockets, six disconnect/reconnect pairs 3 s apart:
1, 2, 3, 4, 5, 6 live websockets, registry still reporting one client. A Sendspin client holds
exactly ONE websocket, so they fought over it: audio for a few seconds, then `close_code=1006`,
forever. That is the whole 19:05–19:39 window in that unit's server log.
**9.1.0 fixes the swallow** (the message loop's cancel now propagates via `_connection_done`), but
`disconnect_from_client` is still synchronous and still clobbers `_connection_tasks[url]`, so
`_stop_dialing` stays.

> **Correction, 2026-08-24.** "A client holds exactly ONE websocket" is true of 6.0.5 and is what
> the account above was written against. Under 9.1.0 connections OVERLAP: measured on the rig, our
> player's session 306 stayed open while sessions 307-311 opened and closed, because 9.x brings an
> incoming connection up provisionally and then arbitrates by activity rank rather than refusing it
> outright. The lesson is unaffected — two dialers still fight, and one holder still wins — but do
> not use "it holds one websocket" to predict that a dial will be REFUSED. It may be accepted and
> then lose the arbitration, which is a different failure with a different signature. OPEN-ITEMS #23.

**The orphaned eviction timer.** Found the same night, with DEBUG on, chasing "reroute a speaker, it
plays for a few seconds, then drops back to idle". `SendspinClient._schedule_cleanup` assigns
`_cleanup_handle` **without cancelling whatever was already there**, so scheduling twice orphans the
first timer — nothing references it, `attach_connection`'s "cancel pending cleanup" can never reach
it, and it fires anyway. `_do_cleanup`'s only other guard is `if self._connected` on the object that
owns the timer, which does not protect a connection that came back on a *different* object, and it
then evicts whichever client currently holds that id. Two schedules is the normal shape of a release:
the teardown carries no goodbye reason (→ 30 s delayed) and the `USER_REQUEST` goodbye ~250 ms later
adds an immediate one on top.

```
20:53:32,209  Scheduling delayed cleanup in 30s (reason: None)
20:53:32,507  Received client/hello           <- rerouted, reconnected
20:53:32,571  attached player 98:A3:16:D0:9E:E8   <- playing
20:54:02,210  Cleaning up client from registry
20:54:02,211  removing 98:A3:16:D0:9E:E8 from group   <- evicted mid-playback
```

Exactly 30 s after the **unroute**, not the reroute — which is why it read as a random 15–60 s
dropout that scaled with how quickly the speaker was re-routed. That session logged 3 cancelled
cleanups against 13 executed ones. **Still unfixed in 9.1.0, and the window is now 180 s**, so an
orphan has six times longer to outlive a re-route.

**Stop the stream before changing membership, not after — but know what that is and is not.**
`attach_player` stops the stream, changes membership, then re-acquires. The other order (add, then
refresh) puts `stream/start` → `stream/end` → `stream/start` on the wire inside ~110 ms, because
`add_client` runs the library's late-join and `refresh_stream` then replaces that stream. The single
start is strictly less churn and costs nothing, so it stays — but it is **belt-and-braces, not a
proven fix**. A sendspin-cpp read says that sequence can jam `pending_start_` permanently, yet a
Voice PE cross-routed mid-stream on the OLD ordering (2026-08-10 ~20:45) did not wedge, and the
Esparagus wedge it was written for is at least as well explained by the two bugs above, which were
live at the time. **Do not cite it as the cause without the A/B.** Separately and firmly: an ESP32
client that *does* wedge is unrecoverable over the protocol — the full ladder (detach, detach+settle,
release, release+settle; `_resources/spike/unwedge_probe.py`) was run on hardware and none of it
worked. Only a power cycle clears it.

**"None" became a true none, reversing an earlier design.** The group and its anchor persist when a
source goes idle, which used to mean attached players stayed attached and silently resumed when the
sender came back. That auto-resume was the bug: on unit-7204, 2026-08-10, both endpoints stayed
attached, the sender returned two minutes later, and audio resumed with no re-route — a room playing
because of something a user did before lunch. Going idle now detaches every player-role client
uniformly, with no exceptions for our own player, a roamed peer, or an adopted foreign speaker, and
nothing auto-resumes one except `autoSwitch.localActivity` (own player, rising edge) or `follow`.

**Every default name must be unique per unit, and a clash must never block a deploy.**
`DEFAULT_SETTINGS` is written to `settings.json` on the first read, so any literal in it outranks the
environment permanently *and identically on every unit* — which is how two freshly imaged units both
came up as "Plum Sendspin" offering a "Plum Audio" AirPlay receiver, with nothing on the LAN to tell
them apart. Unit name and all three endpoint names now derive from `PLUM_UNIT_NAME`, falling back to
`unit_identity.default_device_name()`, which appends a stable per-unit token. The token is the Pi's
**SoC serial**, not a default-route MAC: a MAC moves when a unit is put on `wlan0` instead of `eth0`,
silently renaming it. No token readable → bare name, because unknown beats invented. `deploy.sh`
appends the same token to whatever `units.conf` duplicates and **warns rather than refuses** — a
cosmetic slip must not become a rig that will not deploy.
