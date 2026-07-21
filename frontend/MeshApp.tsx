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
import { Client, Settings as SettingsType, Stream } from './types';
import { Model, SendspinDataService } from './services/sendspinDataService';
import { settingsService } from './services/settingsService';
import { useThemeSettings } from './hooks/useThemeSettings';

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
    a.onPlayPause === b.onPlayPause &&
    a.onSkip === b.onSkip &&
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
  const [settings, setSettings] = useState<SettingsType>(settingsService.getMergedSettings());

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

  useThemeSettings(settings);

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

  // Left card = us and whoever is synced with us. Right card = everyone else — never ourselves.
  // Both comparisons guard on featuredId being set: when we're idle it is null, and so is every
  // idle peer's stream, so an unguarded equality would read "synced with us" and swallow them.
  const syncedClients: Client[] = useMemo(
    () => stableClients.filter((c) => c.isLocal || (featuredId != null && c.currentStreamId === featuredId)),
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
  const onSourceVolume = useCallback((v: number) => {
    const f = featuredRef.current;
    if (f) service.setStreamVolume(f.id, v);
  }, []);
  const onClientVolume = useCallback((clientId: string, v: number) => service.setVolume(clientId, v), []);

  /** Route this unit — and everyone currently grouped with it — onto the chosen source. */
  const onSelectStream = useCallback((streamId: string | null) => {
    setSelectedStreamId(streamId);
    const m = modelRef.current;
    const mine = m.clients.filter((c) => c.isLocal);
    const current = mine.find((c) => c.currentStreamId)?.currentStreamId ?? null;
    // Whoever shares our group moves with us; a solo unit just moves itself.
    const group = current ? m.clients.filter((c) => c.currentStreamId === current) : mine;
    const targets = group.length ? group : mine;
    for (const c of targets) {
      if (streamId) void service.routeClient(c.id, streamId);
      else void service.unrouteClient(c.id);
    }
  }, []);
  const onClientStreamChange = useCallback((clientId: string, streamId: string | null) => {
    if (streamId) void service.routeClient(clientId, streamId);
    else void service.unrouteClient(clientId);
  }, []);
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
                <NowPlaying stream={featured} canSeek={false} />
                <MemoPlayerControls
                  stream={featured}
                  volume={featured.volume ?? 100}
                  onVolumeChange={onSourceVolume}
                  sourceVolume={featured.volume}
                  onSourceVolumeChange={onSourceVolume}
                  onPlayPause={onPlayPause}
                  onSkip={onSkip}
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
    </div>
  );
}
