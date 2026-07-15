/**
 * Sendspin controller-role WebSocket client (one per unit).
 *
 * The GUI's live now-playing + transport channel. Connects to a unit's Sendspin server
 * (ws://<host>:8927) as a controller/metadata/artwork receiver — NOT a player, so it never pulls
 * the audio stream. See docs/FRONTEND-PORT.md for the audited protocol.
 *
 * Per connection the server exposes ONE group's state; with one source-group per unit that group
 * is the unit's active source. Metadata/controller state ride `server/state` (as diffs), transport
 * state rides `group/update`, album art arrives as binary frames. Position is not pushed — we
 * extrapolate it client-side. There is no seek command in the protocol.
 */

export type PlaybackState = 'playing' | 'paused' | 'stopped' | 'unknown';

/** Every MediaCommand the controller protocol defines. No `seek`. */
export type ControllerCommand =
  | 'play' | 'pause' | 'stop' | 'next' | 'previous'
  | 'volume' | 'mute'
  | 'repeat_off' | 'repeat_one' | 'repeat_all'
  | 'shuffle' | 'unshuffle' | 'switch';

/** Merged now-playing state for a unit's current group. */
export interface NowPlaying {
  groupId: string | null;
  groupName: string | null;
  playbackState: PlaybackState;
  title?: string;
  artist?: string;
  album?: string;
  artworkUrl?: string; // object URL (binary art) or the metadata artwork_url text pointer
  // Position extrapolation inputs (from metadata.progress):
  trackProgressMs?: number; // progress at `timestampUs`
  trackDurationMs?: number; // 0 = live/unknown
  playbackSpeed?: number; // x1000; 0 = paused
  timestampUs?: number; // server µs when trackProgressMs is valid
  // Controller state:
  volume?: number; // source (group) volume 0-100
  muted?: boolean;
  supportedCommands: ControllerCommand[];
}

function emptyNowPlaying(): NowPlaying {
  return { groupId: null, groupName: null, playbackState: 'unknown', supportedCommands: [] };
}

/**
 * Extrapolated current track position in ms. `serverClockOffsetUs` maps local→server µs (captured
 * from the last timestamped message). Halts when paused (playbackSpeed 0) or not playing.
 */
export function currentPositionMs(np: NowPlaying, serverClockOffsetUs: number, nowMs = Date.now()): number {
  if (np.trackProgressMs == null || np.timestampUs == null) return 0;
  const speed = np.playbackSpeed ?? 1000;
  if (speed === 0 || np.playbackState !== 'playing') return np.trackProgressMs;
  const nowServerUs = nowMs * 1000 + serverClockOffsetUs;
  const elapsedMs = ((nowServerUs - np.timestampUs) * speed) / 1_000_000;
  const pos = np.trackProgressMs + elapsedMs;
  if (np.trackDurationMs && np.trackDurationMs > 0) return Math.max(0, Math.min(pos, np.trackDurationMs));
  return Math.max(0, pos);
}

const ARTWORK_CHANNEL_0 = 8; // binary message types 8..11 = artwork channels 0..3
const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 15000;

export class SendspinControllerClient {
  readonly unitId: string;
  private url: string;
  private clientId: string;
  private onUpdate: (unitId: string, np: NowPlaying) => void;

  private ws: WebSocket | null = null;
  private np: NowPlaying = emptyNowPlaying();
  private artworkObjectUrl: string | null = null;
  private serverClockOffsetUs = 0; // serverUs - localUs, from the last timestamped message
  private closed = false;
  private reconnectAttempts = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(
    unitId: string,
    host: string,
    onUpdate: (unitId: string, np: NowPlaying) => void,
    opts: { port?: number; clientId?: string } = {},
  ) {
    this.unitId = unitId;
    this.url = `ws://${host}:${opts.port ?? 8927}/sendspin`;
    this.clientId = opts.clientId ?? `plum-web-${unitId}-${Math.floor(performance.now())}`;
    this.onUpdate = onUpdate;
  }

  get nowPlaying(): NowPlaying {
    return this.np;
  }

  get clockOffsetUs(): number {
    return this.serverClockOffsetUs;
  }

  connect(): void {
    this.closed = false;
    this.open();
  }

  close(): void {
    this.closed = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    this.revokeArtwork();
    if (this.ws) {
      this.ws.onclose = null;
      this.ws.close();
      this.ws = null;
    }
  }

  /** Send a controller command. `volume` required for 'volume', `mute` for 'mute'. */
  send(command: ControllerCommand, args: { volume?: number; mute?: boolean } = {}): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    const controller: Record<string, unknown> = { command };
    if (command === 'volume') controller.volume = Math.max(0, Math.min(100, args.volume ?? 0));
    if (command === 'mute') controller.mute = !!args.mute;
    this.ws.send(JSON.stringify({ type: 'client/command', payload: { controller } }));
  }

  // -- internals ----------------------------------------------------------

  private open(): void {
    const ws = new WebSocket(this.url);
    ws.binaryType = 'arraybuffer';
    this.ws = ws;
    ws.onopen = () => {
      this.reconnectAttempts = 0;
      ws.send(
        JSON.stringify({
          type: 'client/hello',
          payload: {
            client_id: this.clientId,
            name: 'Plum Web GUI',
            version: 1,
            supported_roles: ['controller@v1', 'metadata@v1', 'artwork@v1'],
            'artwork@v1_support': {
              channels: [{ source: 'album', format: 'jpeg', media_width: 512, media_height: 512 }],
            },
          },
        }),
      );
    };
    ws.onmessage = (ev) => this.onMessage(ev);
    ws.onclose = () => this.scheduleReconnect();
    ws.onerror = () => ws.close();
  }

  private scheduleReconnect(): void {
    if (this.closed) return;
    // On a drop we don't know the current state; reset to unknown so the UI doesn't show stale.
    this.np = { ...emptyNowPlaying(), groupId: this.np.groupId, groupName: this.np.groupName };
    this.emit();
    const delay = Math.min(RECONNECT_BASE_MS * 2 ** this.reconnectAttempts, RECONNECT_MAX_MS);
    this.reconnectAttempts++;
    this.reconnectTimer = setTimeout(() => this.open(), delay);
  }

  private onMessage(ev: MessageEvent): void {
    if (typeof ev.data !== 'string') {
      this.onBinary(ev.data as ArrayBuffer);
      return;
    }
    let msg: { type?: string; payload?: any };
    try {
      msg = JSON.parse(ev.data);
    } catch {
      return;
    }
    switch (msg.type) {
      case 'server/state':
        this.onServerState(msg.payload);
        break;
      case 'group/update':
        this.onGroupUpdate(msg.payload);
        break;
      case 'stream/end':
      case 'stream/clear':
        this.revokeArtwork();
        this.np = { ...this.np, artworkUrl: undefined };
        this.emit();
        break;
      // stream/start (artwork channel config) needs no action — we already requested our size.
    }
  }

  private onServerState(payload: any): void {
    if (!payload) return;
    let changed = false;
    const md = payload.metadata;
    if (md) {
      // Metadata is a diff: omitted = unchanged, explicit null = cleared.
      if ('timestamp' in md) this.captureClockOffset(md.timestamp);
      if ('title' in md) this.np.title = md.title ?? undefined;
      if ('artist' in md) this.np.artist = md.artist ?? undefined;
      if ('album' in md) this.np.album = md.album ?? undefined;
      if ('artwork_url' in md && !this.artworkObjectUrl) this.np.artworkUrl = md.artwork_url ?? undefined;
      if (md.progress) {
        this.np.trackProgressMs = md.progress.track_progress;
        this.np.trackDurationMs = md.progress.track_duration;
        this.np.playbackSpeed = md.progress.playback_speed;
        this.np.timestampUs = md.timestamp;
      }
      changed = true;
    }
    const ctrl = payload.controller;
    if (ctrl) {
      if ('supported_commands' in ctrl) this.np.supportedCommands = ctrl.supported_commands ?? [];
      if ('volume' in ctrl) this.np.volume = ctrl.volume;
      if ('muted' in ctrl) this.np.muted = ctrl.muted;
      changed = true;
    }
    if (changed) this.emit();
  }

  private onGroupUpdate(payload: any): void {
    if (!payload) return;
    if ('group_id' in payload) this.np.groupId = payload.group_id ?? null;
    if ('group_name' in payload) this.np.groupName = payload.group_name ?? null;
    if ('playback_state' in payload) this.np.playbackState = payload.playback_state ?? 'unknown';
    this.emit();
  }

  private onBinary(buf: ArrayBuffer): void {
    if (buf.byteLength < 9) return;
    const view = new DataView(buf);
    const msgType = view.getUint8(0);
    const channel = msgType - ARTWORK_CHANNEL_0;
    if (channel !== 0) return; // we only requested channel 0 (album, 512x512)
    const imageBytes = buf.slice(9);
    this.revokeArtwork();
    if (imageBytes.byteLength === 0) {
      this.np.artworkUrl = undefined; // empty payload = cleared
    } else {
      const blob = new Blob([imageBytes], { type: 'image/jpeg' });
      this.artworkObjectUrl = URL.createObjectURL(blob);
      this.np.artworkUrl = this.artworkObjectUrl;
    }
    this.emit();
  }

  private captureClockOffset(serverTimestampUs: number): void {
    if (typeof serverTimestampUs === 'number') {
      this.serverClockOffsetUs = serverTimestampUs - Date.now() * 1000;
    }
  }

  private revokeArtwork(): void {
    if (this.artworkObjectUrl) {
      URL.revokeObjectURL(this.artworkObjectUrl);
      this.artworkObjectUrl = null;
    }
  }

  private emit(): void {
    this.onUpdate(this.unitId, { ...this.np });
  }
}
