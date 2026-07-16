/**
 * MeshApp — Phase-3 first vertical slice of the ported GUI.
 *
 * Drives the reused, engine-agnostic Plum-Snapcast components (StreamSelector / NowPlaying /
 * PlayerControls / SyncedDevices) straight from SendspinDataService — proving the data layer backs
 * the real UI end-to-end against a live mesh (/api/mesh/view + one controller WS per unit).
 *
 * Deliberately minimal: no Settings / visualizer / calibration / browser-audio yet (those port on
 * top of this once the data path is confirmed in a browser). The full App.tsx rewrite replaces
 * this; keeping it separate lets `npm run dev` show a working slice without the 2837-line surgery.
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { NowPlaying } from './components/NowPlaying';
import { PlayerControls } from './components/PlayerControls';
import { StreamSelector } from './components/StreamSelector';
import { SyncedDevices } from './components/SyncedDevices';
import { Model, SendspinDataService } from './services/sendspinDataService';
import { Stream } from './types';

const service = new SendspinDataService();

// Memoized transport controls: the parent re-renders ~2x/s (position tick) + on every WS message,
// but the buttons only depend on id/isPlaying/volume. Skipping their reconciliation on every tick
// keeps the button DOM stable — otherwise a click landing mid-reconcile is silently dropped.
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

const EMPTY: Model = { servers: [], streams: [], clients: [] };

export default function MeshApp(): React.ReactElement {
  const [model, setModel] = useState<Model>(EMPTY);
  const [selectedStreamId, setSelectedStreamId] = useState<string | null>(null);

  useEffect(() => {
    service.start();
    const unsub = service.subscribe(setModel);
    return () => {
      unsub();
      service.stop();
    };
  }, []);

  // Best default source: one with real now-playing (title/art) beats one that's merely `streaming`,
  // which beats the first source. A paused source still has metadata, so it stays a strong pick.
  const bestDefault = (streams: Stream[]): Stream | undefined =>
    streams.find((s) => s.currentTrack.title || s.currentTrack.albumArtUrl) ??
    streams.find((s) => s.isPlaying) ??
    streams[0];

  // Keep the featured selection STICKY. Auto-pick only when nothing is selected or the selected
  // source has vanished — never on a pause. Otherwise pausing (which flips isPlaying to false)
  // would bounce the view to another unit's idle AirPlay source, dropping art/progress and
  // leaving the transport buttons pointed at the wrong stream.
  useEffect(() => {
    if (selectedStreamId && model.streams.some((s) => s.id === selectedStreamId)) return;
    const pick = bestDefault(model.streams);
    setSelectedStreamId(pick ? pick.id : null);
  }, [model.streams, selectedStreamId]);

  const featured: Stream | undefined = useMemo(
    () => model.streams.find((s) => s.id === selectedStreamId) ?? bestDefault(model.streams),
    [model.streams, selectedStreamId],
  );

  const caps = featured ? service.getStreamCapabilities(featured.id) : null;

  // Stable handlers (identity never changes) reading the latest featured via a ref, so the memoized
  // controls don't re-render when only the position tick fires.
  const featuredRef = useRef(featured);
  featuredRef.current = featured;
  const onPlayPause = useCallback(() => {
    const f = featuredRef.current;
    if (f) service.controlStream(f.id, f.isPlaying ? 'pause' : 'play');
  }, []);
  const onSkip = useCallback((dir: 'next' | 'prev') => {
    const f = featuredRef.current;
    if (f) service.controlStream(f.id, dir === 'next' ? 'next' : 'previous');
  }, []);
  const onHwVolume = useCallback((v: number) => {
    const f = featuredRef.current;
    if (f) service.setStreamVolume(f.id, v);
  }, []);
  const onSrcVolume = useCallback((v: number) => {
    const f = featuredRef.current;
    if (f) service.setStreamVolume(f.id, v);
  }, []);

  return (
    <div style={{ maxWidth: 720, margin: '0 auto', padding: 16, fontFamily: 'system-ui, sans-serif' }}>
      <h1 style={{ fontSize: 20, marginBottom: 4 }}>Plum Audio — Mesh</h1>
      <p style={{ opacity: 0.6, marginTop: 0, fontSize: 13 }}>
        {model.servers.length} unit(s) · {model.streams.length} source(s) · {model.clients.length} player(s)
      </p>

      {featured && (
        <section style={{ margin: '16px 0' }}>
          <StreamSelector
            streams={model.streams}
            currentStreamId={featured.id}
            onSelectStream={setSelectedStreamId}
            federationEnabled={model.servers.length > 1}
          />
          <NowPlaying stream={featured} canSeek={caps?.canSeek ?? false} />
          <MemoPlayerControls
            stream={featured}
            volume={featured.volume ?? 100}
            onVolumeChange={onHwVolume}
            sourceVolume={featured.volume}
            onSourceVolumeChange={onSrcVolume}
            onPlayPause={onPlayPause}
            onSkip={onSkip}
          />
        </section>
      )}

      <section style={{ marginTop: 24 }}>
        <h2 style={{ fontSize: 15, opacity: 0.7 }}>Players</h2>
        <SyncedDevices
          clients={model.clients}
          streams={model.streams}
          onVolumeChange={(clientId, v) => void service.setVolume(clientId, v)}
          onStreamChange={(clientId, streamId) =>
            streamId ? void service.routeClient(clientId, streamId) : void service.unrouteClient(clientId)
          }
          onGroupVolumeAdjust={(dir) => featured && service.setStreamVolume(featured.id, (featured.volume ?? 100) + (dir === 'up' ? 5 : -5))}
          onGroupMute={() => featured && service.controlStream(featured.id, 'mute')}
        />
      </section>
    </div>
  );
}
