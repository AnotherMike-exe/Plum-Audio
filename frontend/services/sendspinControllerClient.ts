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

/**
 * Clock sync filter — the controller half of the spec's REQUIRED time synchronization.
 *
 * Every Sendspin client "must continuously send client/time"; the server replies with server/time
 * carrying four timestamps, and the client derives a server↔local clock offset. All progress
 * timestamps ride the server's monotonic clock, so without this our extrapolated position drifts,
 * and a strict server can treat a controller that never syncs as not operational.
 *
 * Strategy: best-of-window. Each round-trip yields (offset, delay); the sample with the LOWEST
 * one-way delay in the recent window is the most trustworthy (it suffered the least queuing), so
 * its offset wins — the classic NTP "minimum filter". `error` is that best delay, which the send
 * loop uses to back off once the clock is settled.
 */
export class TimeFilter {
  private samples: Array<{ offsetUs: number; delayUs: number }> = [];
  private static readonly WINDOW = 8;
  private best: { offsetUs: number; delayUs: number } | null = null;

  /** Feed one round-trip. Formulas match aiosendspin's _handle_server_time exactly. */
  update(offsetUs: number, delayUs: number): void {
    this.samples.push({ offsetUs, delayUs });
    if (this.samples.length > TimeFilter.WINDOW) this.samples.shift();
    this.best = this.samples.reduce((a, b) => (b.delayUs < a.delayUs ? b : a));
  }

  get synchronized(): boolean {
    return this.samples.length >= 3;
  }
  /** server_us − local_us, in microseconds. */
  get offsetUs(): number {
    return this.best?.offsetUs ?? 0;
  }
  /** Best (lowest) one-way delay seen — a proxy for remaining uncertainty. */
  get errorUs(): number {
    return this.best?.delayUs ?? Number.POSITIVE_INFINITY;
  }
}

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
// How long to hold an optimistic play/pause before letting server state win again. Covers the
// AirPlay round-trip (buffered audio keeps draining for ~5s, so `pend` lands late); if the command
// never takes (e.g. the source ignored it), reality reasserts after this window.
const OPTIMISTIC_HOLD_MS = 8000;

export class SendspinControllerClient {
  readonly unitId: string;
  private url: string;
  private clientId: string;
  private onUpdate: (unitId: string, np: NowPlaying) => void;

  private ws: WebSocket | null = null;
  private np: NowPlaying = emptyNowPlaying();
  private artworkObjectUrl: string | null = null;
  // Clock sync: the filter is authoritative once synchronized; before then we fall back to the
  // rough metadata-timestamp offset so progress still moves at connect. `serverClockOffsetUs` is
  // the value the data service reads (server_us − local_us).
  private clock = new TimeFilter();
  private serverClockOffsetUs = 0;
  private timeTimer: ReturnType<typeof setTimeout> | null = null;
  private stateSent = false; // client/state{synchronized} is sent once per connection
  private closed = false;
  private reconnectAttempts = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  // Optimistic transport: after a GUI play/pause, hold this speed until the server confirms it (a
  // matching speed arrives) or OPTIMISTIC_HOLD_MS elapses. null = no action pending.
  private optimisticSpeed: number | null = null;
  private optimisticUntil = 0;
  // Commands issued while the WS is mid-reconnect: queue and flush once (re)connected + grouped, so
  // a click during a brief socket blip isn't silently lost. Each carries a timestamp for TTL expiry.
  private pendingCommands: Array<{ msg: string; at: number }> = [];

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
    if (this.timeTimer) clearTimeout(this.timeTimer);
    this.reconnectTimer = this.timeTimer = null;
    this.revokeArtwork();
    if (this.ws) {
      this.ws.onclose = null;
      this.ws.close();
      this.ws = null;
    }
  }

  private nowUs(): number {
    return Date.now() * 1000;
  }

  /** Send client/time. Restarts the adaptive loop: fast until synced, then backs off. */
  private sendTime(): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.ws.send(JSON.stringify({ type: 'client/time', payload: { client_transmitted: this.nowUs() } }));
    // Match aiosendspin's cadence: 200 ms until the clock settles, then ease off to 3 s.
    const err = this.clock.errorUs;
    const nextMs = !this.clock.synchronized ? 200 : err < 1000 ? 3000 : err < 5000 ? 500 : 200;
    if (this.timeTimer) clearTimeout(this.timeTimer);
    this.timeTimer = setTimeout(() => this.sendTime(), nextMs);
  }

  /** server/time reply → NTP offset/delay → filter (formulas verbatim from the library). */
  private onServerTime(p: { client_transmitted: number; server_received: number; server_transmitted: number }): void {
    const now = this.nowUs();
    const offset = ((p.server_received - p.client_transmitted) + (p.server_transmitted - now)) / 2;
    const delay = ((now - p.client_transmitted) - (p.server_transmitted - p.server_received)) / 2;
    this.clock.update(Math.round(offset), Math.round(delay));
    if (this.clock.synchronized) {
      this.serverClockOffsetUs = this.clock.offsetUs; // authoritative once we have real samples
      if (!this.stateSent) {
        this.sendState('synchronized');
        this.stateSent = true;
      }
    }
  }

  /** client/state — REQUIRED. We are a controller/metadata client, so no player payload. */
  private sendState(state: 'synchronized' | 'error' | 'external_source'): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: 'client/state', payload: { state } }));
    }
  }

  /** Send a controller command. `volume` required for 'volume', `mute` for 'mute'. */
  send(command: ControllerCommand, args: { volume?: number; mute?: boolean } = {}): void {
    const controller: Record<string, unknown> = { command };
    if (command === 'volume') controller.volume = Math.max(0, Math.min(100, args.volume ?? 0));
    if (command === 'mute') controller.mute = !!args.mute;
    const msg = JSON.stringify({ type: 'client/command', payload: { controller } });
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(msg);
      return;
    }
    // WS mid-reconnect (or briefly closed): queue instead of dropping the click, and kick a
    // reconnect if the socket is fully closed. flushPending() sends it once (re)connected + grouped.
    this.pendingCommands.push({ msg, at: Date.now() });
    while (this.pendingCommands.length > 8) this.pendingCommands.shift();
    const state = this.ws ? this.ws.readyState : WebSocket.CLOSED;
    if (!this.closed && (state === WebSocket.CLOSED || this.ws === null)) {
      if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
      this.open();
    }
  }

  private flushPending(): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN || this.pendingCommands.length === 0) return;
    const now = Date.now();
    const cmds = this.pendingCommands;
    this.pendingCommands = [];
    for (const c of cmds) {
      if (now - c.at <= 5000) this.ws.send(c.msg); // drop commands older than 5s (stale intent)
    }
  }

  // -- internals ----------------------------------------------------------

  private open(): void {
    const ws = new WebSocket(this.url);
    ws.binaryType = 'arraybuffer';
    this.ws = ws;
    ws.onopen = () => {
      this.reconnectAttempts = 0;
      this.clock = new TimeFilter(); // a new socket is a new clock relationship
      this.stateSent = false;
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
      // Begin continuous clock sync immediately (spec: clients send client/time continuously).
      this.sendTime();
      // Flush any commands queued during the reconnect, once the server has had time to re-group us
      // into the source group (a command sent while still in our solo group would go nowhere).
      if (this.pendingCommands.length > 0) setTimeout(() => this.flushPending(), 600);
    };
    ws.onmessage = (ev) => this.onMessage(ev);
    ws.onclose = () => this.scheduleReconnect();
    ws.onerror = () => ws.close();
  }

  private scheduleReconnect(): void {
    if (this.closed) return;
    this.clearOptimistic(); // don't hold a stale transport guess across a drop
    // Keep the last-known now-playing through brief blips — a healthy reconnect (server restart,
    // roam) is sub-second and the reconnected socket re-emits full state, so blanking immediately
    // makes the GUI flash "disconnected" and drop art/progress mid-pause. Only reset to unknown
    // after several failed attempts, when the connection is genuinely gone.
    if (this.reconnectAttempts >= 3) {
      this.np = { ...emptyNowPlaying(), groupId: this.np.groupId, groupName: this.np.groupName };
    }
    this.emit();
    const delay = Math.min(RECONNECT_BASE_MS * 2 ** this.reconnectAttempts, RECONNECT_MAX_MS);
    this.reconnectAttempts++;
    this.reconnectTimer = setTimeout(() => this.open(), delay);
  }

  /**
   * Optimistically reflect a GUI-initiated transport action before the source round-trips.
   * AirPlay's pause round-trip (MPRIS → shairport → buffer drain → `pend` → metadata diff) takes
   * several seconds; reflecting speed locally flips the button instantly. The next real progress
   * diff refines the anchor. Only touches playback speed/state — never title/art — so a no-op
   * command self-corrects on the next update rather than blanking anything.
   */
  applyOptimisticTransport(command: ControllerCommand): void {
    if (command === 'pause') {
      this.np.playbackSpeed = 0;
      this.optimisticSpeed = 0;
    } else if (command === 'play') {
      this.np.playbackSpeed = 1000;
      this.np.playbackState = 'playing';
      // Re-anchor so extrapolation resumes from the frozen position, not a stale timestamp.
      if (this.np.trackProgressMs != null) this.np.timestampUs = Date.now() * 1000 + this.serverClockOffsetUs;
      this.optimisticSpeed = 1000;
    } else {
      return;
    }
    this.optimisticUntil = Date.now() + OPTIMISTIC_HOLD_MS;
    this.emit();
  }

  private clearOptimistic(): void {
    this.optimisticSpeed = null;
    this.optimisticUntil = 0;
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
      case 'server/time':
        this.onServerTime(msg.payload);
        break;
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
        const serverSpeed = md.progress.playback_speed;
        const holding = this.optimisticSpeed != null && this.optimisticUntil > Date.now();
        if (holding && serverSpeed !== this.optimisticSpeed) {
          // GUI action hasn't round-tripped to the source yet — hold the button where the user put
          // it and ignore in-flight frames from the pre-action state (AirPlay keeps sending
          // speed=1000 for ~5s after a pause). Freeze the position for a held pause; let it advance
          // for a held play.
          this.np.playbackSpeed = this.optimisticSpeed as number;
          if (this.optimisticSpeed !== 0) {
            this.np.trackProgressMs = md.progress.track_progress;
            this.np.trackDurationMs = md.progress.track_duration;
            this.np.timestampUs = md.timestamp;
          }
        } else {
          if (holding) this.clearOptimistic(); // server confirmed the action
          this.np.trackProgressMs = md.progress.track_progress;
          this.np.trackDurationMs = md.progress.track_duration;
          this.np.playbackSpeed = serverSpeed;
          this.np.timestampUs = md.timestamp;
        }
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
    // Fallback only. Treating a metadata anchor timestamp as "server-now" ignores transmission
    // delay and is crude, but it lets progress move before the first few client/time round-trips
    // land (and if a non-conformant server never answers client/time at all). Once the filter is
    // synchronized it owns serverClockOffsetUs and this is ignored.
    if (typeof serverTimestampUs === 'number' && !this.clock.synchronized) {
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
