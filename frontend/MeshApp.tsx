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

import React, { useEffect, useMemo, useState } from 'react';
import { NowPlaying } from './components/NowPlaying';
import { PlayerControls } from './components/PlayerControls';
import { StreamSelector } from './components/StreamSelector';
import { SyncedDevices } from './components/SyncedDevices';
import { Model, SendspinDataService } from './services/sendspinDataService';
import { Stream } from './types';

const service = new SendspinDataService();

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

  // Pick a stream to feature: the selection, else the first playing source, else the first.
  const featured: Stream | undefined = useMemo(() => {
    if (selectedStreamId) return model.streams.find((s) => s.id === selectedStreamId);
    return model.streams.find((s) => s.isPlaying) ?? model.streams[0];
  }, [model.streams, selectedStreamId]);

  const caps = featured ? service.getStreamCapabilities(featured.id) : null;

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
          <PlayerControls
            stream={featured}
            volume={featured.volume ?? 100}
            onVolumeChange={(v) => service.setStreamVolume(featured.id, v)}
            sourceVolume={featured.volume}
            onSourceVolumeChange={(v) => service.setStreamVolume(featured.id, v)}
            onPlayPause={() => service.controlStream(featured.id, featured.isPlaying ? 'pause' : 'play')}
            onSkip={(dir) => service.controlStream(featured.id, dir === 'next' ? 'next' : 'previous')}
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
