/**
 * Sendspin data service — the engine seam for the Plum-Audio mesh GUI.
 *
 * Replaces Plum-Snapcast's snapcastService + snapcastDataService + federationService +
 * playbackService. Two data planes (see docs/FRONTEND-PORT.md):
 *   - Topology (which units/sources/players/groups exist) — REST poll of GET /api/mesh/view.
 *   - Now-playing (title/artist/album/art/position/transport) — ONE controller WS per SOURCE
 *     (a client sits in exactly one group, so it sees exactly one source). Merged client-side.
 *
 * Produces the engine-agnostic { servers, streams, clients } model the existing components consume,
 * so App.tsx wires to this instead of the Snapcast/federation services.
 */

import { Client, Server, Stream, Track } from '../types';
import { NowPlaying, SendspinControllerClient, currentPositionMs, ControllerCommand } from './sendspinControllerClient';

const MESH_API_PORT = 5001;
const MESH_API_BASE = import.meta.env.VITE_MESH_API_URL || '/api/mesh';
const POLL_INTERVAL_MS = 3000;

// ---- Mesh REST wire shapes (backend/scripts/mesh/model.py) ----
interface WirePlayer {
  player_id: string;
  name: string;
  connected: boolean;
  group_id: string | null;
  url: string | null;
}
interface WireSource {
  source_id: string;
  group_id: string;
  group_name: string | null;
  streaming: boolean;
  player_ids: string[];
}
interface WireUnit {
  unit_id: string;
  name: string;
  host: string | null;
  sources: WireSource[];
  players: WirePlayer[];
}
export interface MeshView {
  units: WireUnit[];
}

export interface Model {
  servers: Server[];
  streams: Stream[];
  clients: Client[];
}

/** Federated stream id — a source_id is only unique within its unit. */
export function streamId(unitId: string, sourceId: string): string {
  return `${unitId}::${sourceId}`;
}
export function parseStreamId(id: string): { unitId: string; sourceId: string } {
  const i = id.indexOf('::');
  return { unitId: id.slice(0, i), sourceId: id.slice(i + 2) };
}

/**
 * Pure: fold the mesh view + per-group now-playing into the component model. Exported for tests.
 * `npByGroup` / `offsetByGroup` are keyed by group_id (each unit's controller reports its group).
 */
export function mapViewToModel(
  view: MeshView,
  npByGroup: Map<string, NowPlaying>,
  offsetByGroup: Map<string, number>,
  nowMs = Date.now(),
): Model {
  const servers: Server[] = [];
  const streams: Stream[] = [];
  // group_id -> federated stream id, so a player's group_id resolves to its current stream
  // across ANY unit (a roamed player sits in a peer unit's source group).
  const groupToStream = new Map<string, string>();

  for (const unit of view.units) {
    servers.push({
      id: unit.unit_id,
      name: unit.name,
      host: unit.host ?? '',
      port: MESH_API_PORT,
      connected: true,
      isLocal: false,
    });
    for (const src of unit.sources) {
      const sid = streamId(unit.unit_id, src.source_id);
      groupToStream.set(src.group_id, sid);
      const np = npByGroup.get(src.group_id);
      const offset = offsetByGroup.get(src.group_id) ?? 0;
      const posMs = np ? currentPositionMs(np, offset, nowMs) : 0;
      const track: Track = {
        id: sid,
        title: np?.title ?? '',
        artist: np?.artist ?? '',
        album: np?.album ?? '',
        albumArtUrl: np?.artworkUrl ?? '',
        duration: np?.trackDurationMs ? np.trackDurationMs / 1000 : 0,
      };
      // A source pauses by dropping playback_speed to 0 on the metadata role (the group's
      // playback_state stays 'playing'). Treat speed 0 as paused so the transport button + bar
      // reflect it — this is the only pause signal AirPlay/Spotify give us out-of-band.
      const paused = np != null && np.playbackSpeed === 0;
      const isPlaying = np ? np.playbackState === 'playing' && !paused : src.streaming;
      streams.push({
        id: sid,
        serverId: unit.unit_id,
        serverName: unit.name,
        name: src.group_name || src.source_id,
        sourceDevice: src.source_id,
        currentTrack: track,
        isPlaying,
        progress: posMs / 1000,
        playback: {
          position: np?.trackProgressMs ?? 0,
          duration: np?.trackDurationMs ?? 0,
          interpolated_position: posMs,
          playback_status: np ? (paused ? 'paused' : np.playbackState) : 'unknown',
          is_stale: !np,
        },
        volume: np?.volume,
      });
    }
  }

  const clients: Client[] = [];
  for (const unit of view.units) {
    for (const p of unit.players) {
      clients.push({
        id: p.player_id,
        serverId: unit.unit_id,
        serverName: unit.name,
        name: p.name,
        currentStreamId: (p.group_id && groupToStream.get(p.group_id)) || null,
        volume: 100, // TODO(backend): per-player volume isn't in the snapshot yet
        connected: p.connected,
      });
    }
  }

  return { servers, streams, clients };
}

type Listener = (model: Model) => void;

export class SendspinDataService {
  // Federated stream id ("<unitId>::<sourceId>") -> controller client. ONE PER SOURCE, not per
  // unit: a Sendspin client holds a single websocket and therefore sits in a single group, so a
  // per-unit controller would only ever report the one source it happened to be grouped into
  // (metadata/artwork/transport for every other source on that unit would be invisible).
  private controllers = new Map<string, SendspinControllerClient>();
  private npByGroup = new Map<string, NowPlaying>();
  private offsetByGroup = new Map<string, number>();
  private unitHosts = new Map<string, string>(); // unitId -> host (for per-unit REST)
  private lastView: MeshView = { units: [] };
  private listeners = new Set<Listener>();
  private pollTimer: ReturnType<typeof setInterval> | null = null;
  private tickTimer: ReturnType<typeof setInterval> | null = null;

  start(): void {
    void this.poll();
    this.pollTimer = setInterval(() => void this.poll(), POLL_INTERVAL_MS);
    // Re-emit ~2x/s so extrapolated position advances even without new WS messages.
    this.tickTimer = setInterval(() => this.emit(), 500);
  }

  stop(): void {
    if (this.pollTimer) clearInterval(this.pollTimer);
    if (this.tickTimer) clearInterval(this.tickTimer);
    this.pollTimer = this.tickTimer = null;
    for (const c of this.controllers.values()) c.close();
    this.controllers.clear();
  }

  subscribe(cb: Listener): () => void {
    this.listeners.add(cb);
    cb(this.snapshot());
    return () => this.listeners.delete(cb);
  }

  snapshot(): Model {
    return mapViewToModel(this.lastView, this.npByGroup, this.offsetByGroup);
  }

  // -- commands -----------------------------------------------------------

  async routeClient(playerId: string, federatedStreamId: string): Promise<void> {
    const { unitId, sourceId } = parseStreamId(federatedStreamId);
    await this.post(unitId, '/route', { player_id: playerId, source_id: sourceId });
  }

  async unrouteClient(playerId: string, federatedStreamId?: string): Promise<void> {
    // Unroute needs the source the player is currently on; derive from the model if not given.
    const sid = federatedStreamId ?? this.snapshot().clients.find((c) => c.id === playerId)?.currentStreamId;
    if (!sid) return;
    const { unitId, sourceId } = parseStreamId(sid);
    await this.post(unitId, '/unroute', { player_id: playerId, source_id: sourceId });
  }

  async setVolume(playerId: string, volume: number, muted = false): Promise<void> {
    const unitId = this.playerUnit(playerId);
    if (!unitId) return;
    await this.post(unitId, '/volume', { player_id: playerId, volume, muted });
  }

  controlStream(federatedStreamId: string, command: ControllerCommand): void {
    const controller = this.controllers.get(federatedStreamId);
    if (!controller) return;
    controller.send(command);
    // Flip the transport button immediately; the AirPlay pause round-trip is multi-second.
    // applyOptimisticTransport emits through the onUpdate callback → onNowPlaying → npByGroup.
    if (command === 'play' || command === 'pause') controller.applyOptimisticTransport(command);
  }

  setStreamVolume(federatedStreamId: string, volume: number): void {
    this.controllers.get(federatedStreamId)?.send('volume', { volume });
  }

  getStreamCapabilities(federatedStreamId: string): {
    canPlay: boolean;
    canPause: boolean;
    canSeek: boolean;
    canGoNext: boolean;
    canGoPrevious: boolean;
  } {
    const { unitId, sourceId } = parseStreamId(federatedStreamId);
    const src = this.lastView.units
      .find((u) => u.unit_id === unitId)
      ?.sources.find((s) => s.source_id === sourceId);
    const cmds = (src && this.npByGroup.get(src.group_id)?.supportedCommands) || [];
    return {
      canPlay: cmds.includes('play'),
      canPause: cmds.includes('pause'),
      canSeek: false, // no seek in the Sendspin controller protocol
      canGoNext: cmds.includes('next'),
      canGoPrevious: cmds.includes('previous'),
    };
  }

  // -- internals ----------------------------------------------------------

  private async poll(): Promise<void> {
    try {
      const res = await fetch(`${MESH_API_BASE}/view`);
      if (!res.ok) return;
      const view: MeshView = await res.json();
      this.applyView(view);
    } catch {
      // transient — keep last view; the tick timer still re-emits.
    }
  }

  private applyView(view: MeshView): void {
    this.lastView = view;
    const seen = new Set<string>();
    for (const unit of view.units) {
      if (unit.host) this.unitHosts.set(unit.unit_id, unit.host);
      if (!unit.host) continue;
      for (const src of unit.sources) {
        const key = streamId(unit.unit_id, src.source_id);
        seen.add(key);
        if (this.controllers.has(key)) continue;
        // The "ctrl:<source_id>:" client id tells the unit which source group to join us to.
        const c = new SendspinControllerClient(unit.unit_id, unit.host, (_uid, np) => this.onNowPlaying(key, np), {
          clientId: `ctrl:${src.source_id}:${Math.floor(performance.now())}`,
        });
        this.controllers.set(key, c);
        c.connect();
      }
    }
    // Drop controllers for sources (or whole units) that vanished from discovery.
    for (const [key, c] of this.controllers) {
      if (!seen.has(key)) {
        c.close();
        this.controllers.delete(key);
      }
    }
    this.emit();
  }

  private onNowPlaying(streamKey: string, np: NowPlaying): void {
    if (np.groupId) {
      this.npByGroup.set(np.groupId, np);
      const offset = this.controllers.get(streamKey)?.clockOffsetUs ?? 0;
      this.offsetByGroup.set(np.groupId, offset);
    }
    this.emit();
  }

  private playerUnit(playerId: string): string | undefined {
    for (const u of this.lastView.units) if (u.players.some((p) => p.player_id === playerId)) return u.unit_id;
    return undefined;
  }

  private async post(unitId: string, path: string, body: unknown): Promise<void> {
    const host = this.unitHosts.get(unitId);
    // Prefer per-unit host (works cross-unit + open CORS); fall back to the proxied base.
    const base = host ? `http://${host}:${MESH_API_PORT}/api/mesh` : MESH_API_BASE;
    const res = await fetch(`${base}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`mesh ${path} failed: ${res.status}`);
    void this.poll(); // reflect the change quickly
  }

  private emit(): void {
    const model = this.snapshot();
    for (const cb of this.listeners) cb(model);
  }
}
