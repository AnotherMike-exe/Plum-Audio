# Frontend Port — Plum-Snapcast GUI → Plum-Audio (Sendspin)

> Canonical reference for the Phase-3 GUI port. Source of truth for what to reuse, rewrite, and
> drop. Derived from a full read of the Plum-Snapcast `frontend/` (54 files, ~18k LOC).

## The seam (important)

The **leaf components are engine-agnostic** — they import only `types.ts` + `Icon`, and receive
`Server[] / Stream[] / Client[]` as props plus callback handlers. They port **as-is**.

The **real engine seam is `App.tsx`** (a ~2837-line orchestrator), not the service files. App wires
in three Snapcast/federation-specific services and carries ID-munging, a "none-stream" routing
hack, dual local-WS-vs-federation-REST branches, and ~5 anti-flap grace periods tuned to 5 s
polling. The port **collapses** all of that into one `sendspinDataService` + a live controller-WS
push model.

## Component-facing model (`types.ts`) — the contract to satisfy

```ts
Server { id; name; host; port; connected; isLocal }
Stream { id; serverId?; serverName?; name; sourceDevice; currentTrack: Track;
         isPlaying; progress /*sec*/; playback?: PlaybackData; volume? /*source vol 0-100*/ }
Client { id; serverId?; serverName?; name; currentStreamId: string|null; volume /*0-100*/; connected }
Track  { id; title; artist; album; albumArtUrl; duration /*sec*/ }
PlaybackData { position; duration; interpolated_position /*ms*/; playback_status; is_stale }
```

## New `services/sendspinDataService.ts` — replaces snapcast+snapcastData+federation+playback

Contract to implement:
```ts
getSnapshot(): Promise<{ servers; streams; clients }>          // GET /api/mesh/view
subscribe(cb): () => void                                      // controller-WS push
onMetadataUpdate / onPlaybackStateUpdate / onPositionUpdate    // controller-WS metadata roles
routeClient(playerId, sourceId)        // POST /api/mesh/route
unrouteClient(playerId)                // POST /api/mesh/unroute
setVolume(playerId, volume, muted?)    // POST /api/mesh/volume
controlStream(sourceId, 'play'|'pause'|'next'|'previous')      // controller WS
setStreamVolume(sourceId, volume)      // controller WS (source vol)
seekTo(sourceId, positionMs)           // controller WS (if supported)
getStreamCapabilities(sourceId)        // controller-WS field or per-source-type table
```

**`/api/mesh/view` → model mapping:**
- source → **Stream**: `source_id→id`, `group_name→name`, `streaming→isPlaying`; track/progress/vol from controller-WS metadata.
- player → **Client**: `player_id→id`, `name→name`, `connected→connected`; **`currentStreamId` = the `source_id` whose `group_id === player.group_id`** (else `null`). ⚠ THIS MAPPING IS THE CRUX — get it wrong and every device shows the wrong source.
- unit → **Server** (optional, if keeping the multi-unit grouping UI).

## Reuse / rewrite / drop

**Reuse as-is:** `types.ts` (trim federation fields), `index.tsx`, all leaf components
(`NowPlaying`, `PlayerControls`, `StreamSelector`, `SyncedDevices`, `ClientManager`,
`GroupVolumeControl`, `Switch`, `TabBar`, `Icon`, `CustomColorPicker`), `Settings.tsx` +
`settings/*` **except SnapcastTab**, `Visualizer`/`AudioVisualizer`/`AmorphousBlob` (minus the
reactive audio feed), all `utils/*`, `settingsService`, `calibrationService`,
`deviceSettingsService`, `hooks/useAudioSync`, `hooks/useAudioVisualizer`. Keep
`integrationsService` + `IntegrationsTab` **iff** Plum-Audio keeps `/api/integrations/*`.

**Rewrite:** new `sendspinDataService.ts`; `App.tsx` (strip dual paths / ID-munging / none-stream /
browser-audio / grace periods → re-point handlers at the data service).

**Drop / defer:** `snapcastService`, `snapcastDataService`, `federationService`, `playbackService`,
`snapStreamService` (~900 LOC browser audio), `hooks/useBrowserAudioClient`, `ServerManager.tsx`,
`settings/SnapcastTab.tsx`.

**Carry over verbatim:** the artwork data-URL corruption guard + `/coverart/` proxy rewrite
(`snapcastDataService.ts` ~lines 7–34, 155–186) into the new data service — Sendspin artwork needs
the same validation/proxying.

## Gaps vs the old federation API
- **No server CRUD** in mesh (units are discovery-only) → `ServerManager`/SnapcastTab have no backend. Drop or make read-only.
- **Source volume + playback commands** move REST→controller-WS (simplifies App).
- **`getStreamCapabilities`** has no mesh equivalent → per-source-type table or a controller-WS field, else play/pause/seek buttons misbehave.
- **`POST /api/mesh/source{,/stop}`** is NEW capability with no existing UI (future: a source start/stop control).

## First vertical slice (recommended order)
1. `sendspinDataService.getSnapshot()` → `GET /api/mesh/view`; map units→Stream[], players→Client[] (currentStreamId via group_id match).
2. Render read-only (`federationEnabled=false`, no browser audio) — proves the presentational tree reuses unchanged.
3. `subscribe()` to controller WS for live streaming/metadata/position push (kills polling flicker).
4. Commands: `onStreamChange`→route/unroute, `onVolumeChange`→volume. Players routable → slice done.
5. `onPlayPause`/`onSkip`/`onSeek`/`onSourceVolumeChange` over controller WS.
6. Later: reactive visualizer, integrations, calibration, `/api/mesh/source` UI.

## Risks
- **Routing model mismatch** (player↔source via group_id, not Snapcast group→stream) — the mapping is the crux.
- **Position/metadata single source of truth is the controller WS** — re-poll `/api/mesh/view` on WS reconnect; no `/api/playback` fallback.
- **`App.tsx` is a God component** with anti-flap timing tuned to polling — don't port the grace-period code verbatim; the WS push model makes most of it removable; budget for optimistic-update regressions.
- **Feature loss for v1**: "Listen in Browser" + audio-reactive visualizer go dark (Snapcast-protocol-specific) until Sendspin exposes a browser PCM stream; server-management UI has no backend. Confirm acceptable.

## Controller-WS unknowns to resolve before slice 3
The Sendspin **controller role** protocol (subscribe/metadata/playback/position/commands) needs a
source audit like the server-side one — what messages the controller WS sends/receives, and whether
`getStreamCapabilities`-equivalent info is available. Do this before wiring `subscribe()`.
