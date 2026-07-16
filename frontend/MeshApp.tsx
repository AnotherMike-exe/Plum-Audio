/**
 * MeshApp — the Phase-3 ported GUI shell, wired to SendspinDataService.
 *
 * Reproduces the Plum-Snapcast App.tsx layout (two-column card grid: now-playing on the left,
 * other streams/devices on the right, settings in the footer) but adapted to the mesh CONTROLLER
 * model — there is no browser-as-player "myClient", so the left card features the SELECTED source
 * and the device lists are the roamable players grouped by which source they're on. Browser-audio,
 * federation server-CRUD, and the reactive visualizer are dropped for v1 (see docs/FRONTEND-PORT.md);
 * the settings overlay + visualizer port in later slices (the gear opens a placeholder for now).
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
import { Client, Stream } from './types';
import { Model, SendspinDataService } from './services/sendspinDataService';

const service = new SendspinDataService();
const EMPTY: Model = { servers: [], streams: [], clients: [] };
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

function SettingsPlaceholder({ onClose }: { onClose: () => void }): React.ReactElement {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div
        className="w-full max-w-md bg-[var(--bg-secondary)] rounded-2xl shadow-2xl p-6"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-[var(--border-color)] pb-4 mb-4">
          <h2 className="text-xl font-semibold text-[var(--accent-color)]">Settings</h2>
          <button onClick={onClose} className="p-2 rounded-full hover:bg-[var(--bg-secondary-hover)]" aria-label="Close">
            <Icon name="xmark" />
          </button>
        </div>
        <p className="text-[var(--text-secondary)]">Settings (integrations, audio, visualizer) port in the next slice.</p>
      </div>
    </div>
  );
}

export default function MeshApp(): React.ReactElement {
  const [model, setModel] = useState<Model>(EMPTY);
  const [selectedStreamId, setSelectedStreamId] = useState<string | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);

  useEffect(() => {
    service.start();
    const unsub = service.subscribe(setModel);
    return () => {
      unsub();
      service.stop();
    };
  }, []);

  // Best default source: real now-playing (title/art) beats merely `streaming`, beats the first.
  const bestDefault = (streams: Stream[]): Stream | undefined =>
    streams.find((s) => s.currentTrack.title || s.currentTrack.albumArtUrl) ??
    streams.find((s) => s.isPlaying) ??
    streams[0];

  // Sticky, metadata-aware featured selection — re-pick only when nothing is selected or the
  // selection vanished, never on a pause (which flips isPlaying).
  useEffect(() => {
    if (selectedStreamId && model.streams.some((s) => s.id === selectedStreamId)) return;
    const pick = bestDefault(model.streams);
    setSelectedStreamId(pick ? pick.id : null);
  }, [model.streams, selectedStreamId]);

  const featured: Stream | undefined = useMemo(
    () => model.streams.find((s) => s.id === selectedStreamId) ?? bestDefault(model.streams),
    [model.streams, selectedStreamId],
  );

  // Latest featured/model for stable handlers that must read current values without re-binding.
  const featuredRef = useRef(featured);
  featuredRef.current = featured;
  const modelRef = useRef(model);
  modelRef.current = model;

  // Identity-stable arrays: keep the same reference across position ticks so the memoized device
  // lists only re-render when a client/stream field they show actually changes.
  const streamsSig = model.streams.map((s) => `${s.id}|${s.name}|${s.isPlaying}|${s.volume ?? ''}`).join(';');
  const clientsSig = model.clients
    .map((c) => `${c.id}|${c.name}|${c.currentStreamId ?? ''}|${c.volume}|${c.connected}`)
    .join(';');
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const stableStreams = useMemo(() => model.streams, [streamsSig]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const stableClients = useMemo(() => model.clients, [clientsSig]);

  const featuredId = featured?.id ?? null;
  const syncedClients: Client[] = useMemo(
    () => stableClients.filter((c) => c.currentStreamId === featuredId),
    [stableClients, featuredId],
  );
  const otherClients: Client[] = useMemo(
    () => stableClients.filter((c) => c.currentStreamId !== featuredId),
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

  const serverName = featured?.serverName || 'Plum Audio';
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
              streams={stableStreams}
              currentStreamId={featuredId}
              onSelectStream={setSelectedStreamId}
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
              <h2 className="text-2xl font-semibold text-[var(--text-primary)]">No Source Selected</h2>
              <p className="text-[var(--text-secondary)] mt-2">Choose a source to begin.</p>
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

      {settingsOpen && <SettingsPlaceholder onClose={() => setSettingsOpen(false)} />}
    </div>
  );
}
