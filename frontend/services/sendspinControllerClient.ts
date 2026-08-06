/**
 * Sendspin controller-role WebSocket client (one per unit).
 *
 * The GUI's live now-playing + transport channel. Connects to a unit's Sendspin server
 * (ws://<host>:8927) as a controller/metadata/artwork receiver — NOT a player, so it never pulls
 * the audio stream. See docs/SENDSPIN-CONTROLLER-PROTOCOL.md for the audited wire format.
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

/**
 * Latest visualizer frame for a source, from the native Sendspin visualizer@v1 role.
 * `spectrum` is 0-255 per display bin (scaled from the wire's uint16), the shape the ported
 * Visualizer component already consumes; `loudness` is 0-1. `at` is a local-clock ms stamp used to
 * fade the bars to rest when frames stop (the source went idle). Server computes the DSP — see
 * docs/ARCHITECTURE.md.
 */
export interface VizFrame {
  spectrum: Uint8Array;
  loudness: number;
  at: number;
}

// Requested spectrum shape: the server bins its FFT to exactly this many log-spaced display bins
// over the audible band. 256 gives the GUI headroom to downsample to any bar count (32-256);
// 256 * 2 bytes * ~30 fps is trivial bandwidth, and only while a source is actually streaming.
export const VIZ_BINS = 256;
const VIZ_F_MIN = 40;
const VIZ_F_MAX = 16000;
// Binary visualizer message types (aiosendspin BinaryMessageType).
const MSG_LOUDNESS = 16;
const MSG_SPECTRUM = 19;

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
  repeat?: 'off' | 'one' | 'all'; // group repeat mode (undefined = source doesn't report it)
  shuffle?: boolean; // group shuffle state
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
// How long a socket must SURVIVE before its attempt counter is forgiven. Resetting on `open`
// instead — which is what this did — means a connection that dies shortly after connecting resets
// the counter every cycle, so the backoff is pinned at RECONNECT_BASE_MS forever and the
// exponential growth can never engage. Measured on the rig 2026-08-06: six controller sockets
// opening and closing every ~3 s indefinitely, which reads in the GUI as the visualizer dropping to
// zero and recovering on a fixed cadence. Comfortably longer than a healthy roam reconnect
// (sub-second) and than the handshake, so a genuinely good connection still clears it promptly.
const RECONNECT_STABLE_MS = 10000;

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
  private helloSeen = false; // gate: nothing but client/hello goes on the wire before server/hello
  private closed = false;
  private reconnectAttempts = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  // Fires RECONNECT_STABLE_MS after a socket opens; only then is the attempt counter forgiven.
  private stableTimer: ReturnType<typeof setTimeout> | null = null;
  // Commands issued while the WS is mid-reconnect: queue and flush once (re)connected + grouped, so
  // a click during a brief socket blip isn't silently lost. Each carries a timestamp for TTL expiry.
  private pendingCommands: Array<{ msg: string; at: number }> = [];
  // Latest visualizer frame + an optional subscriber. Off by default (the role is only negotiated
  // when someone actually wants the visualizer) so idle controllers don't pull spectrum they'll
  // never render.
  private viz: VizFrame | null = null;
  private onViz: ((frame: VizFrame) => void) | null = null;
  private wantViz: boolean;

  constructor(
    unitId: string,
    host: string,
    onUpdate: (unitId: string, np: NowPlaying) => void,
    opts: { port?: number; clientId?: string; visualizer?: boolean } = {},
  ) {
    this.unitId = unitId;
    this.url = `ws://${host}:${opts.port ?? 8927}/sendspin`;
    this.clientId = opts.clientId ?? `plum-web-${unitId}-${Math.floor(performance.now())}`;
    this.onUpdate = onUpdate;
    this.wantViz = !!opts.visualizer;
  }

  get nowPlaying(): NowPlaying {
    return this.np;
  }

  /** The most recent visualizer frame, or null if none has arrived (idle / role not negotiated). */
  get vizFrame(): VizFrame | null {
    return this.viz;
  }

  /** Subscribe to visualizer frames as they arrive (in addition to reading vizFrame). */
  setVizListener(cb: ((frame: VizFrame) => void) | null): void {
    this.onViz = cb;
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
    if (this.stableTimer) clearTimeout(this.stableTimer);
    if (this.timeTimer) clearTimeout(this.timeTimer);
    this.reconnectTimer = this.timeTimer = this.stableTimer = null;
    this.viz = null;
    this.onViz = null;
    this.revokeArtwork();
    if (this.ws) {
      this.ws.onclose = null;
      // Say goodbye before dropping the socket. Servers must tolerate an abrupt close anyway, but
      // an explicit client/goodbye lets one release our slot immediately instead of holding a
      // reconnect grace — and the browser player already does this, so the pattern was understood,
      // just not applied here.
      if (this.ws.readyState === WebSocket.OPEN) {
        try {
          this.ws.send(JSON.stringify({ type: 'client/goodbye', payload: { reason: 'user_request' } }));
        } catch {
          // A send on a socket closing underneath us is not worth reporting — we are leaving.
        }
      }
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
  /**
   * The handshake is complete. Only now may anything else go on the wire.
   *
   * The spec requires client/state "immediately after receiving server/hello", so it is sent here
   * unconditionally. It used to be sent only from onServerTime, and only once the clock had
   * settled (>=3 samples) — which meant a server that never answered client/time, or answered
   * twice, got NO client/state at all, a required message simply skipped. The gating instinct was
   * right and is preserved in the VALUE: we open as 'synchronized' by declaration and correct
   * ourselves if the clock never settles, rather than withholding the message.
   */
  private onServerHello(): void {
    if (this.helloSeen) return;
    this.helloSeen = true;
    this.sendState('synchronized');
    this.stateSent = true;
    this.sendTime(); // continuous clock sync starts now, not before
  }

  private onServerTime(p: { client_transmitted: number; server_received: number; server_transmitted: number }): void {
    const now = this.nowUs();
    const offset = ((p.server_received - p.client_transmitted) + (p.server_transmitted - now)) / 2;
    const delay = ((now - p.client_transmitted) - (p.server_transmitted - p.server_received)) / 2;
    this.clock.update(Math.round(offset), Math.round(delay));
    if (this.clock.synchronized) {
      this.serverClockOffsetUs = this.clock.offsetUs; // authoritative once we have real samples
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
      // NOT reset here — see RECONNECT_STABLE_MS. A socket that opens and immediately dies would
      // otherwise retry at a flat 1s forever.
      if (this.stableTimer) clearTimeout(this.stableTimer);
      this.stableTimer = setTimeout(() => { this.reconnectAttempts = 0; }, RECONNECT_STABLE_MS);
      this.clock = new TimeFilter(); // a new socket is a new clock relationship
      this.stateSent = false;
      this.helloSeen = false;
      ws.send(
        JSON.stringify({
          type: 'client/hello',
          payload: {
            client_id: this.clientId,
            name: 'Plum Web GUI',
            version: 1,
            // Optional identity a server/controller can display for this client.
            device_info: { product_name: 'Plum Web GUI', manufacturer: 'Plum Solutions' },
            supported_roles: [
              'controller@v1',
              'metadata@v1',
              'artwork@v1',
              ...(this.wantViz ? ['visualizer@v1'] : []),
            ],
            'artwork@v1_support': {
              channels: [{ source: 'album', format: 'jpeg', media_width: 512, media_height: 512 }],
            },
            // The server auto-computes the spectrum to exactly this shape from the source audio;
            // 64 log-spaced bins over the audible band render well and keep frames small.
            ...(this.wantViz
              ? {
                  'visualizer@v1_support': {
                    buffer_capacity: 65536,
                    rate_max: 30,
                    types: ['spectrum', 'loudness'],
                    spectrum: { n_disp_bins: VIZ_BINS, scale: 'log', f_min: VIZ_F_MIN, f_max: VIZ_F_MAX },
                  },
                }
              : {}),
          },
        }),
      );
      // NOTHING else goes on the wire until server/hello lands. The spec is explicit: "The first
      // message must always be a client/hello... Before this handshake is complete, no other
      // messages should be sent." We used to call sendTime() synchronously right here, pushing
      // client/time ahead of server/hello. aiosendspin servers tolerate it, which is why it never
      // showed up on the rig; a stricter server may drop the connection. See onServerHello.
      // Flush any commands queued during the reconnect, once the server has had time to re-group us
      // into the source group (a command sent while still in our solo group would go nowhere).
      if (this.pendingCommands.length > 0) setTimeout(() => this.flushPending(), 600);
    };
    ws.onmessage = (ev) => this.onMessage(ev);
    ws.onclose = () => this.scheduleReconnect();
    ws.onerror = () => ws.close();
  }

  private scheduleReconnect(): void {
    if (this.stableTimer) { clearTimeout(this.stableTimer); this.stableTimer = null; }
    if (this.closed) return;
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
   * Optimistically reflect a GUI-initiated transport action for the click-to-render gap before the
   * next real `server/state` arrives (normally well under a second — one WS round trip). Only
   * touches playback speed/state — never title/art — so a no-op command self-corrects on the next
   * update rather than blanking anything.
   *
   * This is a one-shot local flip, not a hold: the very next real update wins outright, even if it
   * disagrees (AirPlay's pause round-trip — MPRIS → shairport → buffer drain → `pend` → metadata
   * diff — can take several seconds, so the button may visibly revert while that drains). That is
   * deliberate. A second Sendspin controller client on ANOTHER unit's GUI, watching the same group,
   * has no optimistic flip of its own and reflects the real broadcast immediately — if this client
   * suppressed disagreeing updates instead of yielding to them, the two GUIs would show conflicting
   * transport state and diverging timelines for as long as the suppression lasted. See
   * frontend/MeshApp.tsx's follow feature, which routinely puts two GUIs on one group.
   */
  applyOptimisticTransport(command: ControllerCommand): void {
    if (command === 'pause') {
      this.np.playbackSpeed = 0;
    } else if (command === 'play') {
      this.np.playbackSpeed = 1000;
      this.np.playbackState = 'playing';
      // Re-anchor so extrapolation resumes from the frozen position, not a stale timestamp.
      if (this.np.trackProgressMs != null) this.np.timestampUs = Date.now() * 1000 + this.serverClockOffsetUs;
    } else {
      return;
    }
    this.emit();
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
      case 'server/hello':
        this.onServerHello();
        break;
      case 'server/state':
        this.onServerState(msg.payload);
        break;
      case 'group/update':
        this.onGroupUpdate(msg.payload);
        break;
      case 'stream/end':
      case 'stream/clear':
        // Audio stopped/flushed (pause, idle, a roam) — but the TRACK hasn't changed, so keep its
        // artwork. Revoking it here blanks the cover on every pause/idle/switch until the next PICT
        // (which only arrives on a track change), the reported "art disappears on switch". Art is
        // replaced on the next PICT and cleared on an explicit empty PICT; only the visualizer
        // falls to rest here, since no audio is flowing.
        this.viz = null;
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
        // Always the real broadcast, unconditionally — see applyOptimisticTransport's docstring on
        // why this must never be held/suppressed in favor of a local guess.
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
      if ('repeat' in ctrl) this.np.repeat = ctrl.repeat ?? undefined;
      if ('shuffle' in ctrl) this.np.shuffle = ctrl.shuffle ?? undefined;
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

  // Every binary frame is [type:1][ts:8 BE][payload]. `type` selects the role: 8-11 artwork
  // channels, 16 loudness, 19 spectrum.
  private onBinary(buf: ArrayBuffer): void {
    if (buf.byteLength < 9) return;
    const msgType = new DataView(buf).getUint8(0);
    if (msgType === MSG_SPECTRUM || msgType === MSG_LOUDNESS) {
      this.onVizBinary(msgType, buf);
      return;
    }
    const channel = msgType - ARTWORK_CHANNEL_0;
    if (channel !== 0) return; // we only requested artwork channel 0 (album, 512x512)
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

  private onVizBinary(msgType: number, buf: ArrayBuffer): void {
    const view = new DataView(buf);
    const frame: VizFrame = this.viz
      ? { ...this.viz, at: Date.now() }
      : { spectrum: new Uint8Array(VIZ_BINS), loudness: 0, at: Date.now() };
    if (msgType === MSG_SPECTRUM) {
      // uint16 BE per display bin → 0-255 for the component (high byte is the visible range).
      const bins = (buf.byteLength - 9) >> 1;
      const spectrum = new Uint8Array(bins);
      for (let i = 0; i < bins; i++) spectrum[i] = view.getUint16(9 + i * 2, false) >> 8;
      frame.spectrum = spectrum;
    } else {
      frame.loudness = view.getUint16(9, false) / 65535; // 0-1
    }
    this.viz = frame;
    this.onViz?.(frame);
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
