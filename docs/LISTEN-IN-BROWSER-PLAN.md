# Listen in Browser — Implementation Plan

> **Status:** frontend feature IMPLEMENTED (2026-07-25) on `feature/phase3-sources-gui`; live-smoke
> validated against the R&D rig (unit Pi4-02 @ 192.0.2.10): clicking Listen connects the tab as
> `browser:<id>` to `ws://…:8927/sendspin`, it appears as "Browser Audio" in the device list, the
> button toggles to "Stop Listening", and Stop cleanly removes it + restores the button. The idle-start
> path (unit on "none" → browser idle) is confirmed. **Still to do:** (a) hardware test with a source
> actually playing — route the browser to it and confirm audible FLAC + per-tab volume + route-to-none
> teardown; (b) **§5 retirement of the Snapcast browser stack** (deferred until after the audio test).
> Spike de-risked the wire path end-to-end (PCM + FLAC transcode, resample, clean disconnect) — see
> memory `listen-in-browser-spike`.
>
> **Goal:** a "Listen in Browser" button in a unit's GUI turns *that browser tab* into a Sendspin
> player. It joins the source the triggering unit is currently playing (or starts idle if the unit
> is on "none"), then behaves as a first-class routable Sendspin client visible to the whole mesh —
> individually routable and volume-controllable like any speaker. Refresh/close, an explicit Stop, or
> routing it to "none" tears the feed down and restores the button. Name shown on the network:
> **"Browser Audio"**.

---

## 1. Architecture decision

A browser tab dials **in** to `ws://<unit-host>:8927/sendspin` and negotiates the `player@v1` role via
`@sendspin/sendspin-js`. This is the **inverse** of the native player (`sendspin_player.py`), which the
server dials *out* to on :8928. The dial-in topology is natively supported by aiosendspin's
`on_client_connect`, and it makes the tab a first-class endpoint for free:

- **Listing:** `PlumSendspinServer.snapshot()` builds `players[]` by iterating the server's live client
  registry (`self.server.clients`), filtering out `src:` anchors, disconnected clients, and non-player
  roles (`sendspin_server.py:601-648`). A connected `player@v1` client appears automatically in the
  next `/api/mesh/view` poll — **no discovery/mDNS involved**.
- **Routing / unroute / volume:** the existing `/api/mesh/route|unroute|volume` endpoints
  (`mesh/api.py:236/247/255`) → `Router` → `attach_player` / `detach_player` / `set_player_volume`
  work unchanged. Per-player volume is render-side gain on the client's `PlayerV1Role`
  (`sendspin_server.py:461`).
- **Removal:** on WS drop `client.is_connected` flips false and `snapshot()` excludes it within one
  poll (~2-3 s). A clean `disconnect('shutdown'|'user_request')` triggers immediate registry cleanup;
  an unclean drop is purged after aiosendspin's 30 s grace. **No Plum-side removal code needed.**

**Consequence: the backend requires no changes for listing, routing, volume, or removal.** The feature
is implemented almost entirely in the frontend, plus retiring the dead Snapcast browser stack.

### Where the audio comes from (codec)
The server transcodes the group's PCM per player to whatever the browser advertises. Recommended
default `codecs: ['flac', 'pcm']` — FLAC is lossless, ~half the bandwidth of PCM, cheaper to encode on
a Pi than Opus, and proven in the spike. (Opus is available but adds per-client encode CPU; skip it for
v1.) The unit already ships PyAV/libopus/flac, so FLAC/Opus encode is available.

### Sync mode
`correctionMode: 'quality-local'` — no pitch/resample artifacts, tolerant of loose sync (hard-resync
only past ~600 ms drift). A browser is a casual/second-location listen, not lip-synced to the room
speakers. (A user-adjustable sync-delay control can be added later via `player.setSyncDelay`.)

---

## 2. Key design decisions

| Decision | Choice | Why |
|---|---|---|
| **Player library** | `@sendspin/sendspin-js` v3.2.x, lazy-imported | Official first-party browser player; the porting map's "sendspin-js". Lazy import keeps the opus WASM out of the initial bundle — loads only on first click. |
| **Client id** | fresh `browser:<random>` per page load | Each tab = one client; a **refresh makes a new client** (old one goodbyes on `pagehide`), matching "refresh kills the feed". Do NOT persist in sessionStorage — that would survive refresh. |
| **Display name** | `clientName: 'Browser Audio'` | User preference: generic, not the device name. (Optional short suffix to disambiguate multiple tabs — deferred.) |
| **Auto-join source** | frontend routes to the unit's *featured* stream after connect | "Matches the source of the endpoint it was triggered from." The GUI knows its featured stream id; if it's null (unit idle/none), leave the browser idle → "starts on none" too. Keeps backend untouched and avoids a boot-time auto-group race. |
| **Kill on "none"** | tab watches its own client row; a non-null→null `currentStreamId` transition stops the player | Satisfies "setting it to none kills it, button returns" and works even when *another* GUI on the network routes it to none. Starting idle (null from the start) does not self-trigger. |
| **Kill on refresh/close** | `pagehide`/`beforeunload` → `player.disconnect('shutdown')` | Prompt clean goodbye → immediate server-side removal + network notify. Also stops the SDK's auto-reconnect. Backstop: 30 s grace + `is_connected` filter if the handler doesn't fire. |
| **Backend auto-place** | none (frontend orchestrates) | Alternative would be a `browser:` branch in `_maybe_group_controller` (`sendspin_server.py:430`), but frontend orchestration gives exact "featured source, incl. none" semantics with zero backend change. |

### Known limitation (document, don't fix in v1)
A browser has no listener socket, so **cross-server reclaim/roam does not apply** — the tab is pinned to
the unit it connected to. It can be routed among *that unit's* sources; routing it to another unit's
source (which needs a dial-able URL, `router.py:109` path-2) will no-op. To listen to another unit,
open Listen-in-Browser from *that* unit's GUI. The client-row source picker should therefore offer only
the home unit's sources for browser clients (nice-to-have; see §4.4).

---

## 3. Backend changes

**None required** for the core feature. Confirmed unchanged seams:
`mesh/api.py` (`_route`/`_unroute`/`_volume`), `mesh/router.py` (`route_player`/`unroute_player`/
`set_volume`), `sendspin_server.py` (`attach_player`/`detach_player`/`set_player_volume`/`snapshot`).

Optional future refinements (not in v1):
- Auto-place browser players on connect via a `browser:` id branch in `_maybe_group_controller`.
- Mark browser players in the snapshot (`PlayerState` flag, `model.py:21`) so the GUI can special-case
  routing scope and cross-unit greying.

---

## 4. Frontend changes (live entry is `MeshApp.tsx`, **not** `App.tsx`)

### 4.1 New: dependency
`frontend/package.json` — add `"@sendspin/sendspin-js": "^3.2.1"`. Lazy `import()` it inside the
service so the opus WASM isn't in the main chunk.

### 4.2 New: `frontend/services/sendspinBrowserPlayer.ts`
Thin wrapper around `SendspinPlayer`. Responsibilities:
- `constructor(host: string, opts?)` → build `playerId = 'browser:' + rand()`, store `host`.
- `async start()` — lazy `import('@sendspin/sendspin-js')`, construct
  `new SendspinPlayer({ playerId, baseUrl: 'http://'+host+':8927', clientName: 'Browser Audio',
  codecs: ['flac','pcm'], correctionMode: 'quality-local', onStateChange })`, then `await unlock()`
  (must be called from the click handler's synchronous gesture chain — see 4.3), `await connect()`.
- `stop(reason: 'user_request'|'shutdown' = 'user_request')` — `player.disconnect(reason)`.
- Getters: `playerId`, `isConnected`, `isPlaying`, `currentFormat`.
- Registers a one-shot `pagehide` listener that calls `stop('shutdown')`.
- Mirrors the existing `sendspinControllerClient.ts` URL convention (`ws://<host>:8927/sendspin`).

### 4.3 New: `frontend/hooks/useBrowserPlayer.ts` (replaces `useBrowserAudioClient`)
React state wrapper exposing `{ active, starting, playerId, start(host), stop() }`.
- `start(host)` must run `player.unlock()` **synchronously within the user click** (autoplay gesture),
  then `connect()`. So the hook's `start` is called directly from the button `onClick`, not behind an
  `await` that loses the gesture. (Spike confirmed a real click satisfies unlock.)
- Tracks `active` from connect/disconnect; exposes `playerId` so `MeshApp` can find its own client row.

### 4.4 Edit: `frontend/MeshApp.tsx`
This is the orchestration seam (component tree + data service already here).
- Instantiate the hook: `const browser = useBrowserPlayer();`
- Compute target host: `const browserHost = localUnit?.host ?? window.location.hostname;`
  (`localUnit` from `model.localUnitId`, `MeshApp.tsx:102-105`).
- **Wire the button** into `<MemoClientManager>` (`MeshApp.tsx:379-388`), which currently passes no
  browser props (so the button in `ClientManager.tsx:193-201,278-286` is hidden today):
  - `onStartBrowserAudio={onStartBrowserAudio}`
  - `browserAudioActive={browser.active}`
- `onStartBrowserAudio` handler:
  1. `browser.start(browserHost)` (unlock+connect).
  2. After connect resolves, if `featuredId` is non-null, `service.routeClient(browser.playerId, featuredId)`
     — join the source the unit is currently playing. If `featuredId` is null, do nothing (start idle
     = "none").
- **Kill-on-none watcher** (effect on `model`): find `model.clients.find(c => c.id === browser.playerId)`.
  - If we previously observed it with a non-null `currentStreamId` and it is now null (or the row is
    gone), call `browser.stop()` and clear state. Guard so an initial idle start doesn't self-trigger.
- **Stop affordance:** routing the browser client to "none" from its own row (via
  `moveClient`/`unrouteClient`, `MeshApp.tsx:230-243`) already flips `currentStreamId` to null → the
  watcher tears it down. No extra button needed, but optionally re-label the row or the empty-state
  button to "Stop listening" when `browser.active`.
- (Optional) restrict the browser client row's source picker to the home unit's sources — pass a flag
  or filter `streams` for that row (addresses the cross-unit limitation UX).
- Remove the inert `browserAudio*` stubs passed to `<Visualizer>` (`MeshApp.tsx:438-440`) once
  `Visualizer.tsx` is cleaned (4.6).

### 4.5 Edit: `frontend/components/ClientManager.tsx`
Button already exists (`:193-201`, `:278-286`, gated `onStartBrowserAudio && !browserAudioActive`).
- No structural change needed — it lights up once `MeshApp` passes the props.
- Reconcile the legacy synthetic `none-*` stream id it uses for its "None" control (`:47-59`) with
  MeshApp's `unrouteClient(null)` path so routing a browser client to None goes through
  `moveClient`/`unrouteClient` (which the kill watcher keys on). Prefer emitting `streamId=null` to
  `onStreamChange` for None rather than a synthetic `none-*` id.

### 4.6 Edit: `frontend/components/Visualizer.tsx`
Strip the dead `browserAudioMuted`, `browserAudioSnapStream`, `onStartBrowserAudio`,
`onToggleBrowserAudioMute` props (`:21,41` + App.tsx-only call sites). The live visualizer already runs
off the native `visualizer@v1` role via `service.getVizFrame()`.

---

## 5. Retire the Snapcast browser stack

Delete (browser-audio-specific dead Snapcast code — App.tsx is already unmounted; `index.tsx` renders
`MeshApp`):
- `frontend/services/snapStreamService.ts` (Snapcast Web-Audio player, port 1780 `/stream`).
- `frontend/hooks/useBrowserAudioClient.ts`.
- `frontend/hooks/useAudioVisualizer.ts` (legacy analyser path bound to `SnapStream`; MeshApp uses
  `getVizFrame()` instead — confirm no other importer before deleting).
- `frontend/tests/unit/hooks/useBrowserAudioClient.test.ts`.

Strip browser-audio wiring from the dead `frontend/App.tsx` (import `:19`, `:126`, `:344-395`,
`:471-543`, `:665-723`, `:1604` [1780 art URL], `:1825/1978/2021/2071/2104/2133`, `:2724-2737`,
`:2784-2811`). Full removal of App.tsx's remaining Snapcast data plane
(`snapcastService.ts`/`snapcastDataService.ts`/`federationService.ts`/`ServerManager.tsx` port 1780) is
**out of scope** here — that belongs to the broader App.tsx→MeshApp port. Scope this change to the
browser-audio path only.

---

## 6. Testing

1. **Dev rig (RPi):** deploy branch; from unit A's GUI while A plays AirPlay, click Listen in Browser →
   audio plays in the tab; "Browser Audio" appears in the client list on A's *and* B's GUI.
2. Route the browser client to a different source on A → audio follows. Adjust its volume → independent
   render gain.
3. Route it to "none" (from A and from B) → feed stops, button returns.
4. Refresh the tab → old client disappears within ~3 s, a new one appears if re-clicked. Close the tab →
   disappears.
5. Start with unit A on "none" → browser starts idle (no audio, listed idle). Then play a source on A
   and route the browser to it.
6. Codec: confirm desktop Chrome negotiates FLAC (server log `_preferred_codec=FLAC`), Safari falls back
   to PCM/Opus. Watch Pi CPU with 2-3 browser clients on FLAC.
7. Cross-unit: from B's GUI, try routing A's browser client to a B source → confirm graceful no-op (and
   ideally the picker doesn't offer it).

Headless probe artifacts from the spike (`_resources/spike/` equivalents:
`server_harness.py` + `js/`) can be adapted for a Tier-1 protocol check.

---

## 7. Sequencing

1. Add dependency + `sendspinBrowserPlayer.ts` + `useBrowserPlayer.ts` (no UI yet; unit-test the wrapper).
2. Wire `MeshApp` → `ClientManager` button + auto-join + kill-on-none watcher.
3. Manual hardware test (dev rig) per §6.
4. Retire the Snapcast browser code (§5) once the new path is validated.
5. Update `docs/ARCHITECTURE.md` (Phase-3 item 12 "browser audio") and `docs/SPEC-CONFORMANCE.md`
   (player role now has a browser client), and flip the FRONTEND-PORT.md "browser audio goes dark" note.
