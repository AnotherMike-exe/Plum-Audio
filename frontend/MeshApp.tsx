/**
 * MeshApp — the Phase-3 ported GUI shell, wired to SendspinDataService.
 *
 * Reproduces the Plum-Snapcast App.tsx layout (two-column card grid: now-playing on the left,
 * other streams/devices on the right, settings in the footer), adapted to the mesh model.
 *
 * THIS PAGE IS A UNIT. Each unit serves its own GUI, so the left card is always THIS unit — its
 * name, its player, and the source that player is on — and the right card is everyone else. The
 * unit never lists itself among "other" devices. `localUnitId`/`localPlayerIds` come from the mesh
 * view (the responder tells us who it is); a player stays "ours" by its listener host even after it
 * roams onto a peer's group.
 *
 * The source picker is a CONTROL, not a view filter: choosing a source routes this unit's player
 * there — and with it every player currently sharing its group, so a synced room moves together.
 * Only sources a sender is actually using are listed (plus whatever we're on); idle ones stay
 * routable and reappear the moment audio flows. Browser-audio, federation server-CRUD, and the
 * reactive visualizer are dropped for v1 (see docs/FRONTEND-PORT.md).
 *
 * All interactive sub-trees are memoized with identity-stable array/handler props so the 500ms
 * position tick (which re-renders to advance the progress bar) can't reconcile their DOM mid-click.
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { NowPlaying } from './components/NowPlaying';
import { PlayerControls } from './components/PlayerControls';
import { StreamSelector } from './components/StreamSelector';
import { SyncedDevices } from './components/SyncedDevices';
import { ClientManager } from './components/ClientManager';
import { Icon } from './components/Icon';
import { Settings } from './components/Settings';
import { Visualizer } from './components/Visualizer';
import { Client, Settings as SettingsType, Stream } from './types';
import { Model, SendspinDataService, FOREIGN_PREFIX, parseStreamId } from './services/sendspinDataService';
import type { ControllerCommand } from './services/sendspinControllerClient';
import { settingsService } from './services/settingsService';
import { useThemeSettings } from './hooks/useThemeSettings';
import { useBrowserPlayer } from './hooks/useBrowserPlayer';

const service = new SendspinDataService();
const EMPTY: Model = { servers: [], streams: [], clients: [], localPlayerIds: [] };
const clampVol = (v: number) => Math.max(0, Math.min(100, v));

// Memoized interactive controls: the parent re-renders ~2x/s (position tick) + on every WS message,
// x2 under StrictMode. Reconciling a control's DOM mid-click silently drops the click, so give each
// a stable DOM by memoizing on the fields it actually shows (paired with identity-stable array +
// useCallback handler props below).
const MemoPlayerControls = React.memo(
  PlayerControls,
  (a, b) =>
    a.stream.id === b.stream.id &&
    a.stream.isPlaying === b.stream.isPlaying &&
    a.stream.volume === b.stream.volume &&
    a.volume === b.volume &&
    a.sourceVolume === b.sourceVolume &&
    a.canShuffle === b.canShuffle &&
    a.canRepeat === b.canRepeat &&
    a.shuffle === b.shuffle &&
    a.repeat === b.repeat &&
    a.onPlayPause === b.onPlayPause &&
    a.onSkip === b.onSkip &&
    a.onToggleShuffle === b.onToggleShuffle &&
    a.onCycleRepeat === b.onCycleRepeat &&
    a.onVolumeChange === b.onVolumeChange &&
    a.onSourceVolumeChange === b.onSourceVolumeChange,
);
const MemoStreamSelector = React.memo(StreamSelector);
const MemoSyncedDevices = React.memo(SyncedDevices);
const MemoClientManager = React.memo(ClientManager);

export default function MeshApp(): React.ReactElement {
  const [model, setModel] = useState<Model>(EMPTY);
  const [selectedStreamId, setSelectedStreamId] = useState<string | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [visualizerOpen, setVisualizerOpen] = useState(false);
  const [settings, setSettings] = useState<SettingsType>(settingsService.getMergedSettings());
  const browser = useBrowserPlayer();

  useEffect(() => {
    service.start();
    const unsub = service.subscribe(setModel);
    return () => {
      unsub();
      service.stop();
    };
  }, []);

  // Settings: fetch from the config API on mount, then track changes.
  useEffect(() => {
    settingsService.init().then(setSettings).catch((err) => {
      console.error('[Settings] init failed:', err);
    });
    const unsub = settingsService.subscribe(setSettings);
    return () => unsub();
  }, []);

  // Feed the featured track's artwork to the theme hook so album-art colors can drive the UI.
  // (featured is computed below; a ref-free read is fine — the effect keys on the URL string.)

  const onSettingsChange = useCallback((next: SettingsType) => {
    settingsService.updateSettings(next);
  }, []);

  // This unit, and the player(s) it owns.
  const localUnit = useMemo(
    () => model.servers.find((s) => s.id === model.localUnitId),
    [model.servers, model.localUnitId],
  );
  const myPlayers = useMemo(
    () => model.clients.filter((c) => c.isLocal),
    [model.clients],
  );
  // Where this unit is playing from right now — the authority for what the left card shows.
  const myStreamId = myPlayers.find((c) => c.currentStreamId)?.currentStreamId ?? null;

  // A route takes a moment to land (a cross-server reclaim reconnects the player), so hold the
  // user's choice until the mesh view catches up; then the view wins again.
  useEffect(() => {
    if (selectedStreamId && selectedStreamId === myStreamId) setSelectedStreamId(null);
  }, [selectedStreamId, myStreamId]);

  // Our stream only counts as "on" while a sender is using it: when the source goes idle (writer
  // closed, or long silence) the card falls back to the holding state, as the Snapcast build did.
  // The player STAYS routed there — the moment audio flows again this lights straight back up.
  const myStream = useMemo(
    () => model.streams.find((s) => s.id === myStreamId),
    [model.streams, myStreamId],
  );
  const featuredId = selectedStreamId ?? (myStream?.active ? myStream.id : null);
  const featured: Stream | undefined = useMemo(
    () => model.streams.find((s) => s.id === featuredId),
    [model.streams, featuredId],
  );

  // The local unit's single active source, if exactly one is streaming. Used as the browser player's
  // join target when the unit's OWN player is idle (auto-switch hasn't grabbed the source), so
  // "Listen in Browser" still joins what the unit is actually playing rather than starting idle.
  const localActiveTarget = useMemo(() => {
    const act = model.streams.filter((s) => s.active && s.serverId === model.localUnitId);
    return act.length === 1 ? act[0].id : null;
  }, [model.streams, model.localUnitId]);

  // Theme: also drives album-art colors from the featured track's artwork; returns the extracted
  // palette so the visualizer can share it.
  const albumArtColors = useThemeSettings(settings, featured?.currentTrack.albumArtUrl ?? null);

  // The PICKER lists only sources a sender is actually using (plus whatever we're on, so a stream
  // going idle can't yank itself out from under the selection). Device lists still get the full
  // set — a peer parked on an idle source must stay visible and reachable.
  const listedStreams = useMemo(
    () => model.streams.filter((s) => s.active || s.id === featuredId),
    [model.streams, featuredId],
  );

  // What each device READS AS. A player parked on a source nobody is streaming to isn't "on
  // AirPlay" in any sense the user cares about — it's idle, exactly like the left card's holding
  // state, so show it that way. Routing is untouched underneath (the player stays attached and
  // relights the moment audio flows); only the label changes. Controls read the RAW model below,
  // so moving a group still moves everyone actually in it.
  const activeStreamIds = useMemo(
    () => new Set(model.streams.filter((s) => s.active).map((s) => s.id)),
    [model.streams],
  );
  const viewClients: Client[] = useMemo(
    () =>
      model.clients.map((c) =>
        c.currentStreamId && !activeStreamIds.has(c.currentStreamId) ? { ...c, currentStreamId: null } : c,
      ),
    [model.clients, activeStreamIds],
  );

  // Latest featured/model for stable handlers that must read current values without re-binding.
  const featuredRef = useRef(featured);
  featuredRef.current = featured;
  const featuredIdRef = useRef(featuredId);
  featuredIdRef.current = featuredId;
  const localActiveTargetRef = useRef(localActiveTarget);
  localActiveTargetRef.current = localActiveTarget;
  const modelRef = useRef(model);
  modelRef.current = model;

  // Identity-stable arrays: keep the same reference across position ticks so the memoized device
  // lists only re-render when a client/stream field they show actually changes.
  const streamsSig = model.streams.map((s) => `${s.id}|${s.name}|${s.isPlaying}|${s.volume ?? ''}|${s.active}`).join(';');
  const clientsSig = viewClients
    .map((c) => `${c.id}|${c.name}|${c.currentStreamId ?? ''}|${c.volume}|${c.connected}`)
    .join(';');
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const stableStreams = useMemo(() => model.streams, [streamsSig]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const stablePickable = useMemo(() => listedStreams, [listedStreams.map((s) => `${s.id}|${s.name}`).join(';')]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const stableClients = useMemo(() => viewClients, [clientsSig]);

  // "Synced Devices" = OTHER units playing our featured stream — never this unit itself. The top
  // "Select a Source" bar and the main now-playing card already are this unit's own endpoint;
  // listing it again here (with a redundant volume row) is what the original design deliberately
  // omitted. So exclude isLocal: when we're solo the list is empty (nothing else is synced) and the
  // panel hides itself; when a peer joins, only the peer appears. Guard on featuredId so idle peers
  // (currentStreamId null, like our own idle featuredId) aren't mistaken for "synced with us".
  const syncedClients: Client[] = useMemo(
    () => stableClients.filter((c) => !c.isLocal && featuredId != null && c.currentStreamId === featuredId),
    [stableClients, featuredId],
  );
  const otherClients: Client[] = useMemo(
    () => stableClients.filter((c) => !c.isLocal && (featuredId == null || c.currentStreamId !== featuredId)),
    [stableClients, featuredId],
  );

  // -- stable handlers (identity never changes; read latest via refs) --
  const onPlayPause = useCallback(() => {
    const f = featuredRef.current;
    if (f) service.controlStream(f.id, f.isPlaying ? 'pause' : 'play');
  }, []);
  const onSkip = useCallback((dir: 'next' | 'prev') => {
    const f = featuredRef.current;
    if (f) service.controlStream(f.id, dir === 'next' ? 'next' : 'previous');
  }, []);
  const onToggleShuffle = useCallback(() => {
    const f = featuredRef.current;
    if (f) service.controlStream(f.id, f.shuffle ? 'unshuffle' : 'shuffle');
  }, []);
  // Cycle off -> all -> one -> off (the Spotify order: repeat context, then repeat track).
  const onCycleRepeat = useCallback(() => {
    const f = featuredRef.current;
    if (!f) return;
    const next: ControllerCommand =
      f.repeat == null || f.repeat === 'off' ? 'repeat_all' : f.repeat === 'all' ? 'repeat_one' : 'repeat_off';
    service.controlStream(f.id, next);
  }, []);
  const onSourceVolume = useCallback((v: number) => {
    const f = featuredRef.current;
    if (f) service.setStreamVolume(f.id, v);
  }, []);
  const onClientVolume = useCallback((clientId: string, v: number) => service.setVolume(clientId, v), []);

  // Native visualizer frames for the featured source; read on rAF by the visualizer.
  const getSpectrum = useCallback(() => (featuredId ? service.getVizFrame(featuredId) : null), [featuredId]);

  /** Put one speaker on a stream, whichever kind of speaker it is.
   *  - ours, in the mesh          -> the router (intra-server re-group or cross-server reclaim)
   *  - ours, held by a foreign server -> dial its own URL back; the router can't see it to reclaim
   *  - not ours at all            -> adopt (dial) / release (hang up)
   */
  const moveClient = useCallback((client: Client, streamId: string | null) => {
    if (streamId && streamId.startsWith(FOREIGN_PREFIX)) return; // foreign session: nothing to route
    const url = client.url;
    if (streamId && url && (client.isForeign || client.foreignServer)) {
      void service.adoptSpeaker(url, streamId);
      return;
    }
    if (!streamId && client.isForeign && client.currentStreamId) {
      void service.releaseSpeaker(client.id, client.currentStreamId, url);
      return;
    }
    if (streamId) void service.routeClient(client.id, streamId);
    else void service.unrouteClient(client.id);
  }, []);

  /** Route THIS unit's own endpoint onto the chosen source. Never touches whoever else happens to
   *  share the current group (e.g. a peer we're following, or one following us) — that shared
   *  group is a consequence of routing decisions, not something the top selector should undo for
   *  everyone in it. One player per unit, so `mine` is always at most one client. */
  const onSelectStream = useCallback((streamId: string | null) => {
    setSelectedStreamId(streamId);
    const mine = modelRef.current.clients.filter((c) => c.isLocal);
    for (const c of mine) moveClient(c, streamId);
  }, [moveClient]);
  const onClientStreamChange = useCallback((clientId: string, streamId: string | null) => {
    const client = modelRef.current.clients.find((c) => c.id === clientId);
    if (client) moveClient(client, streamId);
  }, [moveClient]);

  // -- Listen in Browser: this tab becomes a Sendspin player of the unit that serves it -------------
  const {
    active: browserActive,
    playerId: browserPlayerId,
    reconnectSeq: browserReconnectSeq,
    start: browserStart,
    stop: browserStop,
  } = browser;

  // The source this tab's player should stay on. Set at Listen; followed to wherever the user
  // manually re-routes it; null once torn down or started idle. Refs so the reconciler reads latest
  // without re-subscribing. `routed` = have we ever seen it land on target; `handledReconnect` = the
  // last reconnectSeq we've reconciled (to tell a reconnect-idle apart from a user route-to-none).
  const browserTargetRef = useRef<string | null>(null);
  const browserRoutedRef = useRef(false);
  const browserHandledReconnectRef = useRef(0);

  const onStartBrowserAudio = useCallback(() => {
    const m = modelRef.current;
    // Join whatever the unit is playing: the featured stream, or — if the unit's own player is idle
    // while a source streams — that single active source. Idle unit (no active source) => start idle.
    const target = featuredIdRef.current ?? localActiveTargetRef.current;
    const validTarget = target && !target.startsWith(FOREIGN_PREFIX) ? target : null;

    // Dial the unit that OWNS the target stream, NOT the one serving this page. A browser player is
    // a plain client of one server and has no listening URL of its own, so it can never be reclaimed
    // across servers the way a hardware player is — it can only ever join a group on the server it
    // connected to. Dialling the local unit while listening to a peer's stream is what left the
    // player visible-but-unroutable, with "Join Stream" doing nothing from either GUI.
    const ownerId = validTarget ? parseStreamId(validTarget).unitId : m.localUnitId;
    const host =
      m.servers.find((s) => s.id === ownerId)?.host ||
      m.servers.find((s) => s.id === m.localUnitId)?.host ||
      window.location.hostname;

    browserTargetRef.current = validTarget;
    browserRoutedRef.current = false;
    browserHandledReconnectRef.current = 0;
    void browserStart(host).catch(() => {
      browserTargetRef.current = null;
    });
  }, [browserStart]);

  const onStopBrowserAudio = useCallback(() => {
    browserTargetRef.current = null;
    browserStop();
  }, [browserStop]);

  // Browser-player reconciler — runs each view tick while the tab is a player. Four jobs:
  //   1. Initial route: the player can't join until it shows up in the 2s-polled view (route_player
  //      looks it up there; routing sooner 400s "unknown player"), so we route once our row appears.
  //   2. Reconnect recovery: the SDK auto-reconnects on a dropped WS, but the reconnected client
  //      lands in a fresh idle group — re-route it back onto its target (keyed on reconnectSeq).
  //   3. Follow a manual re-route: if the user moves it onto another source, adopt that as the target.
  //   4. Route-to-none teardown: if it was on a source and goes idle with NO reconnect, the user
  //      unrouted it (from this gui or any other) — stop the tab's player.
  // Reads RAW model.clients (true group membership), not viewClients (which nulls idle-but-routed
  // streams — a source merely going quiet must not look like an unroute).
  useEffect(() => {
    if (!browserActive || !browserPlayerId) return;
    const row = model.clients.find((c) => c.id === browserPlayerId);
    if (!row) return; // not in the view yet (or mid-reconnect) — wait
    const cur = row.currentStreamId;
    const target = browserTargetRef.current;
    const reconnected = browserReconnectSeq !== browserHandledReconnectRef.current;

    if (cur && !cur.startsWith(FOREIGN_PREFIX) && cur !== target) {
      browserTargetRef.current = cur; // (3) user moved it — follow, so reconnects restore THAT
      browserRoutedRef.current = true;
      browserHandledReconnectRef.current = browserReconnectSeq;
      return;
    }
    if (cur && cur === target) {
      browserRoutedRef.current = true; // landed / still on target
      browserHandledReconnectRef.current = browserReconnectSeq;
      return;
    }
    // cur is null → idle
    if (!target) return; // (started idle — unit had nothing active)
    if (!browserRoutedRef.current || reconnected) {
      void service.routeClient(browserPlayerId, target).catch(() => {
        /* view lag — retry next tick */
      }); // (1) initial or (2) reconnect recovery
      return;
    }
    browserTargetRef.current = null; // (4) unrouted by the user → tear down
    browserStop();
  }, [model.clients, browserActive, browserPlayerId, browserReconnectSeq, browserStop]);
  const adjustStreamVolume = useCallback((streamId: string, dir: 'up' | 'down') => {
    const s = modelRef.current.streams.find((x) => x.id === streamId);
    service.setStreamVolume(streamId, clampVol((s?.volume ?? 100) + (dir === 'up' ? 5 : -5)));
  }, []);
  const muteStream = useCallback((streamId: string) => service.controlStream(streamId, 'mute'), []);
  const syncedGroupVolumeAdjust = useCallback((dir: 'up' | 'down') => {
    const f = featuredRef.current;
    if (f) adjustStreamVolume(f.id, dir);
  }, [adjustStreamVolume]);
  const syncedGroupMute = useCallback(() => {
    const f = featuredRef.current;
    if (f) muteStream(f.id);
  }, [muteStream]);

  // Always this unit's own name — every unit serves its own page.
  const serverName = localUnit?.name || settings.deviceName || 'Plum Audio';
  const connected = model.servers.length > 0;
  const shouldShowControls = !!featured;

  // Transport capabilities from the source's advertised Sendspin controller commands. Repeat/shuffle
  // show only for sources that support them (Spotify) and stay hidden otherwise (AirPlay).
  const featCmds = featured?.supportedCommands ?? [];
  const canShuffle = featCmds.includes('shuffle') || featCmds.includes('unshuffle');
  const canRepeat = featCmds.some((c) => c.startsWith('repeat'));

  return (
    <div className="min-h-screen bg-[var(--bg-primary)] text-[var(--text-primary)] font-sans p-4 md:p-8 flex flex-col">
      {!connected && (
        <div className="w-full max-w-7xl mx-auto mb-4 p-4 bg-red-600/20 border border-red-600/30 rounded-lg">
          <div className="flex items-center">
            <Icon name="triangle-exclamation" className="text-red-400 mr-2" />
            <span className="text-red-400">Connecting to the mesh…</span>
          </div>
        </div>
      )}

      <div className="w-full max-w-7xl mx-auto flex-grow grid grid-cols-1 lg:grid-cols-3 gap-8">
        {/* Left: now-playing + controls + this source's devices */}
        <div className="lg:col-span-2 bg-[var(--bg-secondary)] p-6 rounded-2xl shadow-2xl flex flex-col">
          <div className="border-b border-[var(--border-color)] pb-4">
            <div className="flex items-center justify-between mb-4">
              <h1 className="text-xl font-semibold text-[var(--accent-color)]">{serverName}</h1>
              <div className="flex items-center text-sm text-[var(--text-muted)]">
                <Icon name="tower-broadcast" className="mr-2" />
                <span>{connected ? 'Connected' : 'Offline'}</span>
              </div>
            </div>
            <MemoStreamSelector
              streams={stablePickable}
              currentStreamId={featuredId}
              onSelectStream={onSelectStream}
              federationEnabled={false}
            />
          </div>

          {shouldShowControls && featured ? (
            <div className="flex-grow flex flex-col">
              <div className="space-y-6">
                <NowPlaying stream={featured} canSeek={false} onAlbumArtClick={() => setVisualizerOpen(true)} />
                <MemoPlayerControls
                  stream={featured}
                  volume={featured.volume ?? 100}
                  onVolumeChange={onSourceVolume}
                  sourceVolume={featured.volume}
                  onSourceVolumeChange={onSourceVolume}
                  onPlayPause={onPlayPause}
                  onSkip={onSkip}
                  canShuffle={canShuffle}
                  canRepeat={canRepeat}
                  shuffle={featured.shuffle}
                  repeat={featured.repeat}
                  onToggleShuffle={onToggleShuffle}
                  onCycleRepeat={onCycleRepeat}
                />
              </div>
              <MemoSyncedDevices
                clients={syncedClients}
                streams={stableStreams}
                onVolumeChange={onClientVolume}
                onStreamChange={onClientStreamChange}
                onGroupVolumeAdjust={syncedGroupVolumeAdjust}
                onGroupMute={syncedGroupMute}
              />
            </div>
          ) : (
            <div className="flex-grow flex flex-col items-center justify-center rounded-lg p-8 h-full min-h-[300px]">
              <Icon name="music" className="text-6xl text-[var(--text-muted)] mb-4" />
              <h2 className="text-2xl font-semibold text-[var(--text-primary)]">Nothing Playing</h2>
              <p className="text-[var(--text-secondary)] mt-2">
                Start playing to an endpoint, or choose an active source above.
              </p>
              {/* Our own speaker only needs a row here when some OTHER Sendspin server has claimed
                  it — that's information the top selector can't show (it has nothing to select),
                  and without this the speaker would silently disappear from its own unit's page.
                  A plainly idle speaker has nothing to add beyond what "Select a Source" above
                  already says with nothing chosen, so it's left out — the top bar is the one
                  place that controls this unit's own endpoint. */}
              {myPlayers.some((p) => p.foreignServer) && (
                <div className="mt-8 w-full max-w-md space-y-2">
                  {myPlayers.filter((p) => p.foreignServer).map((p) => (
                    <div key={p.id} className="flex items-center justify-between gap-3 p-3 rounded-lg bg-[var(--bg-tertiary)]">
                      <span className="font-semibold truncate">{p.name}</span>
                      <span className="text-sm text-[var(--text-secondary)] truncate text-right">
                        <Icon name="tower-broadcast" className="mr-2 text-[var(--text-muted)]" />
                        {p.foreignServer!.name}
                        {p.foreignServer!.title ? ` · ${p.foreignServer!.title}` : ''}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>

        {/* Right: other sources & devices */}
        <div className="space-y-8">
          <div className="bg-[var(--bg-secondary)] p-6 rounded-2xl shadow-2xl">
            <h2 className="text-2xl font-bold text-[var(--accent-color)] border-b border-[var(--border-color)] pb-4 mb-4">
              Other Streams &amp; Devices
            </h2>
            <MemoClientManager
              clients={otherClients}
              streams={stableStreams}
              myClientStreamId={featuredId}
              onVolumeChange={onClientVolume}
              onStreamChange={onClientStreamChange}
              onGroupVolumeAdjust={adjustStreamVolume}
              onGroupMute={muteStream}
              onStartBrowserAudio={onStartBrowserAudio}
              onStopBrowserAudio={onStopBrowserAudio}
              browserAudioActive={browserActive}
              browserSyncDelayMs={browser.syncDelayMs}
              onBrowserSyncDelayChange={browser.setSyncDelayMs}
              federationEnabled={false}
            />
          </div>
        </div>
      </div>

      <footer className="w-full max-w-7xl mx-auto grid grid-cols-3 items-center text-[var(--text-muted)] mt-12 text-sm">
        <div />
        <p className="text-center">Plum Audio — Mesh</p>
        <div className="flex justify-end gap-2">
          <button
            onClick={() => setVisualizerOpen(true)}
            className="p-2 rounded-full hover:bg-[var(--bg-secondary)] transition-colors"
            aria-label="Open Visualizer"
            disabled={!featured}
          >
            <Icon name="waveform" className="text-lg" />
          </button>
          <button
            onClick={() => setSettingsOpen(true)}
            className="p-2 rounded-full hover:bg-[var(--bg-secondary)] transition-colors"
            aria-label="Open Settings"
          >
            <Icon name="gear" className="text-lg" />
          </button>
        </div>
      </footer>

      {settingsOpen && (
        <Settings
          settings={settings}
          onSettingsChange={onSettingsChange}
          onClose={() => setSettingsOpen(false)}
        />
      )}

      <Visualizer
        isOpen={visualizerOpen}
        onClose={() => setVisualizerOpen(false)}
        stream={featured ?? null}
        streams={stableStreams}
        settings={settings}
        getSpectrum={getSpectrum}
        extractedAlbumArtColors={albumArtColors}
        currentVolume={featured?.volume ?? 100}
        onPlayPause={onPlayPause}
        onSkip={(dir) => onSkip(dir === 'next' ? 'next' : 'prev')}
        onVolumeChange={onSourceVolume}
        onStreamChange={onSelectStream}
        onOpenSettings={() => { setVisualizerOpen(false); setSettingsOpen(true); }}
        onOpenVisualizerSettings={() => { setVisualizerOpen(false); setSettingsOpen(true); }}
        browserAudioMuted={false}
        onStartBrowserAudio={() => {}}
        onToggleBrowserAudioMute={() => {}}
      />
    </div>
  );
}
