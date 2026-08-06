/**
 * Sendspin data service — the engine seam for the Plum-Audio mesh GUI.
 *
 * Replaces Plum-Snapcast's snapcastService + snapcastDataService + federationService +
 * playbackService, all four now deleted. Two data planes (see docs/ARCHITECTURE.md §11):
 *   - Topology (which units/sources/players/groups exist) — REST poll of GET /api/mesh/view.
 *   - Now-playing (title/artist/album/art/position/transport) — ONE controller WS per SOURCE
 *     (a client sits in exactly one group, so it sees exactly one source). Merged client-side.
 *
 * Produces the engine-agnostic { servers, streams, clients } model the existing components consume,
 * so App.tsx wires to this instead of the Snapcast/federation services.
 */

import { Client, Server, Stream, Track } from '../types';
import { NowPlaying, SendspinControllerClient, currentPositionMs, ControllerCommand, VizFrame } from './sendspinControllerClient';
import {ALBUM_ART_PLACEHOLDER} from './albumArtPlaceholder';

const MESH_API_PORT = 5001;
const MESH_API_BASE = import.meta.env.VITE_MESH_API_URL || '/api/mesh';
const POLL_INTERVAL_MS = 3000;
// How long a just-set volume is held over the polled value (see applyPendingVolumes). Comfortably
// longer than one poll, short enough that a level which never applied corrects itself visibly.
const VOLUME_HOLD_MS = 5000;
// Synthetic stream id for a foreign-server session our player renders (see snapshot()).
export const FOREIGN_PREFIX = 'foreign::';
// Where the learned speaker names live (see rememberPlayerNames). Survives a reload so a speaker
// that is idle when the page opens still reads the way it does when it is playing.
const NAME_MEMO_KEY = 'plum.speakerNames';
const CLIENT_NONCE_PREFIX = 'plum.ctrlNonce.';

// ---- Mesh REST wire shapes (backend/scripts/mesh/model.py) ----
interface WirePlayer {
  player_id: string;
  name: string;
  connected: boolean;
  group_id: string | null;
  url: string | null;
  volume?: number;    // as the PLAYER reports it (client/state), not as we last commanded it
  muted?: boolean;
}
interface WireSource {
  source_id: string;
  group_id: string;
  group_name: string | null;
  streaming: boolean;
  player_ids: string[];
  name?: string;      // endpoint device name ("Kitchen") — what the GUI labels the stream
  active?: boolean;   // a sender is using it right now (see SourceFeeder.is_active)
  // The volume ON THE SENDING DEVICE (phone's AirPlay/BT slider, Spotify Connect device volume).
  // Not an endpoint output level, and not part of the Sendspin protocol — see mesh/model.py.
  source_volume?: number | null;
  source_muted?: boolean | null;
  supports_source_volume?: boolean;
}
interface WireUnit {
  unit_id: string;
  name: string;
  host: string | null;
  sources: WireSource[];
  players: WirePlayer[];
  /** This unit's own speaker, as the player process itself reports it (mesh POST player-state).
   *  `players` can only list clients attached to THAT unit's server, so a speaker claimed by
   *  anyone else — a peer, Music Assistant — is absent there and would vanish from the GUI. */
  local_player?: {
    player_id: string;
    name?: string;
    url?: string;
    attached?: boolean;
    server_id?: string;
    server_name?: string;
    playback_state?: string;
    title?: string;
    artist?: string;
  } | null;
  /** Absent from a peer running an older image — treat that as "has one", never as playerless. */
  has_player?: boolean;
}
/** GET /api/mesh/neighbourhood — every Sendspin service mDNS can see on this segment. */
export interface Neighbourhood {
  players: Array<{ name: string; friendly_name: string; url: string; host: string; port: number; is_own: boolean }>;
  servers: Array<{ name: string; friendly_name: string; url: string; host: string; port: number; is_own: boolean }>;
}

export interface MeshView {
  units: WireUnit[];
  // Which unit answered. A unit serves its own GUI and that page must feature ITSELF, but the
  // view is identical from every unit — so identity can only come from the responder.
  local_unit_id?: string;
}

export interface Model {
  servers: Server[];
  streams: Stream[];
  clients: Client[];
  /** The unit serving this page, if known (mesh view's local_unit_id). */
  localUnitId?: string;
  /** This unit's own player ids — matched by listener host, so a roamed player stays "ours". */
  localPlayerIds: string[];
}

/** Federated stream id — a source_id is only unique within its unit. */
export function streamId(unitId: string, sourceId: string): string {
  return `${unitId}::${sourceId}`;
}
/** Host part of a ws:// URL, or null. */
export function hostOf(url: string): string | null {
  const m = /^\w+:\/\/([^/:]+)/.exec(url);
  return m ? m[1] : null;
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
  neighbourhood?: Neighbourhood | null,
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
      // `!== false`, not `=== true`: a peer on an older image omits the field entirely, and reading
      // that as playerless would make every existing unit look like an ingest-only node.
      hasPlayer: unit.has_player !== false,
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
        albumArtUrl: np?.artworkUrl || ALBUM_ART_PLACEHOLDER,
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
        name: src.name || src.group_name || src.source_id,
        sourceDevice: src.source_id,
        active: !!src.active,
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
        // `volume` is the GROUP volume the controller role reports (the average of this source's
        // endpoints, maintained by the library) — our own output. `sourceVolume` is the sender's.
        volume: np?.volume,
        muted: np?.muted,
        sourceVolume: src.source_volume ?? undefined,
        sourceMuted: src.source_muted ?? undefined,
        supportsSourceVolume: !!src.supports_source_volume,
        supportedCommands: np?.supportedCommands ?? [],
        repeat: np?.repeat,
        shuffle: np?.shuffle,
      });
    }
  }

  // "Ours" is decided by the player's own listener host, NOT by which unit currently reports it:
  // a roamed player is listed by the peer that reclaimed it but is still this unit's speaker.
  const localHost = view.units.find((u) => u.unit_id === view.local_unit_id)?.host ?? null;
  const localPlayerIds: string[] = [];

  const clients: Client[] = [];
  for (const unit of view.units) {
    for (const p of unit.players) {
      const isLocal = !!localHost && !!p.url && hostOf(p.url) === localHost;
      if (isLocal) localPlayerIds.push(p.player_id);
      clients.push({
        id: p.player_id,
        serverId: unit.unit_id,
        serverName: unit.name,
        name: p.name,
        currentStreamId: (p.group_id && groupToStream.get(p.group_id)) || null,
        volume: p.volume ?? 100,
        muted: p.muted ?? false,
        connected: p.connected,
        isLocal,
        url: p.url ?? undefined,
      });
    }
  }

  // Fold in each unit's self-reported speaker. Two jobs: keep a speaker visible when the server
  // it left can no longer see it, and name the server that took it.
  const unitIds = new Set(view.units.map((u) => u.unit_id));
  for (const unit of view.units) {
    const lp = unit.local_player;
    if (!lp?.player_id) continue;
    const claimedByOutsider = !!lp.attached && !!lp.server_id && !unitIds.has(lp.server_id);
    const foreignServer = claimedByOutsider
      ? { name: lp.server_name || lp.server_id!, title: lp.title, artist: lp.artist }
      : undefined;
    const existing = clients.find((c) => c.id === lp.player_id);
    if (existing) {
      if (foreignServer) existing.foreignServer = foreignServer;
      continue;
    }
    // Not listed by any unit: it is attached elsewhere (or nowhere). Synthesize it from the report.
    const isLocal = !!localHost && !!lp.url && hostOf(lp.url) === localHost;
    if (isLocal) localPlayerIds.push(lp.player_id);
    clients.push({
      id: lp.player_id,
      serverId: unit.unit_id,
      serverName: unit.name,
      name: lp.name || lp.player_id,
      currentStreamId: null,
      volume: 100, // unknown: a speaker attached elsewhere reports its level to THAT server, not us
      connected: !!lp.attached,
      isLocal,
      foreignServer,
      url: lp.url,  // how we dial it back: the router can't reclaim what it cannot see
    });
  }

  // Speakers the PROTOCOL can see but the mesh cannot: a third-party Sendspin player (a Home
  // Assistant Voice PE, an ESP32 speaker) advertises itself over mDNS and belongs to no unit's
  // snapshot, so without this it never appears as somewhere you could send audio. Matched by URL,
  // because a foreign speaker's mDNS instance name and its Sendspin client_id are different
  // strings (the Voice PE advertises "home-assistant-voice-…" and connects as its MAC).
  const knownUrls = new Set(
    view.units.flatMap((u) => [...u.players.map((p) => p.url), u.local_player?.url]).filter(Boolean) as string[],
  );
  // An ADOPTED foreign speaker is in a unit's group like any other player, so flag it by URL —
  // release needs to know to hang up rather than just re-group it.
  //
  // `neighbourhood.is_own` only means "not literally this unit's own player" — a PEER unit's own
  // player also comes back `is_own: false` (it's a candidate render endpoint from that unit's point
  // of view too), so it must not be treated as foreign here. A player is only genuinely foreign if
  // its host isn't one of our own mesh units', regardless of adoption state.
  const unitHosts = new Set(view.units.map((u) => u.host).filter(Boolean) as string[]);
  const foreignUrls = new Set(
    (neighbourhood?.players ?? [])
      .filter((p) => !p.is_own && !unitHosts.has(hostOf(p.url) ?? ''))
      .map((p) => p.url),
  );
  for (const c of clients) {
    if (c.url && foreignUrls.has(c.url)) c.isForeign = true;
  }

  for (const entry of neighbourhood?.players ?? []) {
    if (entry.is_own || knownUrls.has(entry.url)) continue;
    clients.push({
      id: entry.url, // stable, and the thing adopt/release actually needs
      name: entry.friendly_name || entry.name,
      currentStreamId: null,
      volume: 100,
      connected: true,
      isForeign: true,
      url: entry.url,
    });
  }

  return { servers, streams, clients, localUnitId: view.local_unit_id, localPlayerIds };
}

// -- learned speaker names ---------------------------------------------------------------------
//
// A speaker has TWO names and the GUI sees a different one depending on whether it is playing.
// Attached, it is in its server's `players` and carries the name it declared at the Sendspin
// handshake ("Home Assistant Voice PE - 01"). Idle, no server holds it, so the only trace of it is
// its mDNS advertisement — and a third-party device typically publishes no `name` TXT key, leaving
// the bare instance name ("home-assistant-voice-a1b2c3"). The row therefore renamed itself every
// time it joined or left a group, which reads as two devices rather than one.
//
// So: remember the protocol name against the speaker's listener URL — the one identifier both
// views share (the client id does NOT: mDNS names it by instance, the handshake by MAC) — and use
// it wherever that URL turns up. Persisted, because "idle at page load" is the common case and a
// memo that only fills in after the speaker has played once would still flip on the first reload.

function loadNameMemo(): [string, string][] {
  try {
    const raw = window.localStorage?.getItem(NAME_MEMO_KEY);
    const parsed = raw ? JSON.parse(raw) : null;
    return Array.isArray(parsed) ? parsed.filter((e) => Array.isArray(e) && e.length === 2) : [];
  } catch {
    return []; // private mode, quota, corrupt entry — names just fall back to mDNS
  }
}

function saveNameMemo(memo: Map<string, string>): void {
  try {
    window.localStorage?.setItem(NAME_MEMO_KEY, JSON.stringify([...memo]));
  } catch {
    // non-fatal: the memo still works for this page's lifetime
  }
}

// The spec asks that a client_id "remain persistent across reconnections so servers can associate
// clients with previous sessions". A performance.now() nonce survives a WS reconnect (it is fixed at
// construction) but not a page reload, so every refresh minted a fresh identity — and against a
// foreign server such as Music Assistant that accretes a dead client record per reload. Persisted
// per source, the same way and for the same reason as the speaker-name memo above.
function stableClientNonce(sourceId: string): string {
  const key = `${CLIENT_NONCE_PREFIX}${sourceId}`;
  try {
    const existing = window.localStorage?.getItem(key);
    if (existing) return existing;
    const minted = Math.floor(Math.random() * 1e9).toString(36);
    window.localStorage?.setItem(key, minted);
    return minted;
  } catch {
    // Private mode or quota: fall back to a per-load value. Worse for the server's bookkeeping,
    // but never a reason to fail to connect.
    return Math.floor(performance.now()).toString(36);
  }
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
  private neighbourhood: Neighbourhood | null = null;
  // Foreign-server consume relay (see MeshApi._consume): our player, as a group member of a foreign
  // server (Music Assistant), forwards that group's controller state + visualizer frames here, and
  // we send transport commands back. Lets the GUI observe + control + visualize foreign playback.
  private consumeWs: WebSocket | null = null;
  private consumeCtrl: {
    commands: string[]; volume: number | null; muted: boolean | null;
    playing: boolean | null;   // authoritative play/pause (audio actually flowing at the player)
    state: string | null;      // group playback_state ('playing' | 'stopped'); 'stopped' = feed ended
    progress: number | null;   // spec position valid at receipt (ms), computed on the server clock
    duration: number | null;   // track duration (ms; 0/unknown for live)
    speed: number | null;      // playback_speed x1000 (spec extrapolation rate; 0/absent = unknown)
    repeat: 'off' | 'one' | 'all' | null; // group repeat mode (foreign source, e.g. MA)
    shuffle: boolean | null;   // group shuffle state
    at: number;                // Date.now() at receipt — anchor for onward extrapolation
  } | null = null;
  private consumeCtrlOptimisticUntil = 0;  // ignore relayed `playing` briefly after a local toggle
  private consumeViz: VizFrame | null = null;
  private consumeArt: string | null = null;  // album art data URL from the foreign server
  private listeners = new Set<Listener>();
  private pollTimer: ReturnType<typeof setInterval> | null = null;
  private tickTimer: ReturnType<typeof setInterval> | null = null;
  // Volume the user just set, held over the polled value until the round trip completes (set →
  // player applies → player reports → our next /view poll). Without it a drag fights the poll.
  // Player volume only; group volume needs none — the controller WS echoes it back immediately.
  private pendingVolume = new Map<string, { volume: number; muted: boolean; at: number }>();
  private pendingSourceVolume = new Map<string, { volume: number; at: number }>();
  // listener URL -> the name the speaker gave over the PROTOCOL (see rememberPlayerNames).
  private nameByUrl = new Map<string, string>(loadNameMemo());

  start(): void {
    void this.poll();
    this.pollTimer = setInterval(() => void this.poll(), POLL_INTERVAL_MS);
    // Re-emit ~2x/s so extrapolated position advances even without new WS messages.
    this.tickTimer = setInterval(() => this.emit(), 500);
    this.openConsume();
  }

  stop(): void {
    if (this.pollTimer) clearInterval(this.pollTimer);
    if (this.tickTimer) clearInterval(this.tickTimer);
    this.pollTimer = this.tickTimer = null;
    for (const c of this.controllers.values()) c.close();
    this.controllers.clear();
    this.consumeWs?.close();
    this.consumeWs = null;
  }

  private openConsume(): void {
    // ws(s):// same-origin /api/mesh/consume (nginx proxies it). In dev, VITE_MESH_API_URL is a full
    // http URL → swap the scheme. Auto-reconnects; the relay is optional, the mesh renders without it.
    let url: string;
    if (MESH_API_BASE.startsWith('http')) url = MESH_API_BASE.replace(/^http/, 'ws') + '/consume';
    else url = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + MESH_API_BASE + '/consume';
    let ws: WebSocket;
    try {
      ws = new WebSocket(url);
    } catch {
      return;
    }
    this.consumeWs = ws;
    ws.onmessage = (ev) => {
      let m: any;
      try { m = JSON.parse(ev.data); } catch { return; }
      if (m.t === 'ctrl') {
        const prev = this.consumeCtrl;
        // Within the optimistic window after a local toggle, keep our intended `playing` so a stale
        // in-flight frame doesn't flip the button back; everything else takes the relayed values.
        const keepOptimistic = Date.now() < this.consumeCtrlOptimisticUntil;
        this.consumeCtrl = {
          commands: m.commands || [],
          volume: m.volume ?? null,
          muted: m.muted ?? null,
          playing: keepOptimistic ? (prev?.playing ?? null) : (typeof m.playing === 'boolean' ? m.playing : null),
          state: typeof m.state === 'string' ? m.state : null,
          progress: typeof m.progress === 'number' ? m.progress : null,
          duration: typeof m.duration === 'number' ? m.duration : null,
          speed: typeof m.speed === 'number' ? m.speed : null,
          repeat: m.repeat === 'off' || m.repeat === 'one' || m.repeat === 'all' ? m.repeat : null,
          shuffle: typeof m.shuffle === 'boolean' ? m.shuffle : null,
          at: Date.now(),
        };
        this.emit();
      } else if (m.t === 'viz' && Array.isArray(m.s)) {
        this.consumeViz = { spectrum: Uint8Array.from(m.s), loudness: m.l ?? 0, at: Date.now() };
      } else if (m.t === 'art') {
        this.consumeArt = m.d ?? null;
        this.emit();
      }
    };
    ws.onclose = () => {
      this.consumeCtrl = null;
      this.consumeViz = null;
      this.consumeArt = null;
      if (this.pollTimer) setTimeout(() => this.openConsume(), 2000); // still running → reconnect
    };
    ws.onerror = () => ws.close();
  }

  /** Send a transport command to the foreign server our player is a member of (via the relay). */
  private sendForeignCommand(command: string, args: { volume?: number; mute?: boolean } = {}): void {
    // Optimistically flip the button so it responds instantly; the relay's audio-flow truth (~1s
    // round-trip through MA) reconciles it. The window makes a stale in-flight frame yield to us.
    if ((command === 'play' || command === 'pause') && this.consumeCtrl) {
      this.consumeCtrl.playing = command === 'play';
      this.consumeCtrlOptimisticUntil = Date.now() + 1500;
      this.emit();
    }
    if (this.consumeWs?.readyState === WebSocket.OPEN) {
      this.consumeWs.send(JSON.stringify({ t: 'cmd', command, ...args }));
    }
  }

  subscribe(cb: Listener): () => void {
    this.listeners.add(cb);
    cb(this.snapshot());
    return () => this.listeners.delete(cb);
  }

  /**
   * Overlay a just-set level on the polled model until the real value comes back (or we give up).
   *
   * Held for at most VOLUME_HOLD_MS, and dropped the moment the poll agrees — so a level that
   * failed to apply (offline speaker, source with no absolute volume) snaps back to the truth
   * rather than lying indefinitely.
   */
  private applyPendingVolumes(model: Model): void {
    const now = Date.now();
    for (const [playerId, pending] of this.pendingVolume) {
      const client = model.clients.find((c) => c.id === playerId);
      if (!client || now - pending.at > VOLUME_HOLD_MS || client.volume === pending.volume) {
        this.pendingVolume.delete(playerId);
        continue;
      }
      client.volume = pending.volume;
      client.muted = pending.muted;
    }
    for (const [sid, pending] of this.pendingSourceVolume) {
      const stream = model.streams.find((s) => s.id === sid);
      if (!stream || now - pending.at > VOLUME_HOLD_MS || stream.sourceVolume === pending.volume) {
        this.pendingSourceVolume.delete(sid);
        continue;
      }
      stream.sourceVolume = pending.volume;
    }
  }

  /** Learn each speaker's name, keyed by its listener URL. */
  private rememberPlayerNames(view: MeshView): void {
    let changed = false;
    const learn = (url: string | null | undefined, name: string | null | undefined) => {
      if (url && name && this.nameByUrl.get(url) !== name) {
        this.nameByUrl.set(url, name);
        changed = true;
      }
    };

    // `players` carries the name the speaker declared at the Sendspin HANDSHAKE, which is fixed at
    // connect: a unit renamed while attached keeps announcing its old name to its server until the
    // audio process restarts, deliberately (restarting it to apply a rename would drop playback).
    for (const unit of view.units) {
      for (const p of unit.players) learn(p.url, p.name);
    }

    // ...so the unit's OWN self-report wins, and is applied second. It is driven from settings.json
    // via unit_identity and therefore reflects a rename within a poll. Without this the memo pinned
    // the pre-rename name — and because the memo is persisted, a peer's GUI kept showing the old
    // name across a page refresh, which is exactly how this was reported from the rig 2026-08-05.
    for (const unit of view.units) {
      learn(unit.local_player?.url, unit.local_player?.name);
    }

    if (changed) saveNameMemo(this.nameByUrl);
  }

  /** Give every speaker we have ever seen attached its protocol name, whatever state it is in. */
  private applyStableNames(model: Model): void {
    for (const c of model.clients) {
      const known = c.url ? this.nameByUrl.get(c.url) : undefined;
      if (known) c.name = known;
    }
  }

  snapshot(): Model {
    const model = mapViewToModel(this.lastView, this.npByGroup, this.offsetByGroup, Date.now(), this.neighbourhood);
    this.applyStableNames(model);
    this.applyPendingVolumes(model);
    // Our player playing a FOREIGN server (MA) is not a stream in the mesh view — synthesize one so
    // now-playing, transport and the visualizer all light up on it. Its metadata rides the player's
    // self-report (foreignServer); controls + spectrum ride the consume relay.
    const fc = model.clients.find((c) => c.isLocal && c.foreignServer);
    const cc = this.consumeCtrl;
    // Feed ended: the player is still a member of MA's group but MA has STOPPED (group
    // playback_state 'stopped', per spec, and no audio flowing) — distinct from a pause, which keeps
    // state 'playing' with speed 0. Drop the synthesized stream so the GUI returns to the idle/none
    // state instead of lingering on the last track. (A full detach clears foreignServer upstream and
    // is handled already; this covers stopped-but-still-attached.)
    const feedEnded = cc != null && cc.playing === false && cc.state === 'stopped';
    if (fc?.foreignServer && !feedEnded) {
      const sid = `${FOREIGN_PREFIX}${fc.id}`;
      // Play/pause: the player's audio-flow truth (Music Assistant's own state/speed fields proved
      // unreliable to a group member — it reports speed 0 and even state 'stopped' mid-playback).
      const playing = cc?.playing != null ? cc.playing : fc.foreignServer.title != null;
      // Position: the relay already carries the spec position valid at receipt (computed on the
      // server clock the player shares). Extrapolate onward at the spec playback_speed; when audio
      // is flowing but the server under-reports speed (0/absent), fall back to 1x so the bar still
      // advances. Halted when paused.
      const durMs = cc?.duration ?? 0;
      let posMs = cc?.progress ?? 0;
      if (cc?.progress != null && playing) {
        const rate = cc.speed && cc.speed > 0 ? cc.speed / 1000 : 1;
        posMs = cc.progress + (Date.now() - cc.at) * rate;
      }
      if (durMs > 0) posMs = Math.max(0, Math.min(posMs, durMs));
      model.streams.push({
        id: sid,
        serverName: fc.foreignServer.name,
        name: fc.foreignServer.name,
        sourceDevice: 'foreign',
        active: true,
        currentTrack: {
          id: sid,
          title: fc.foreignServer.title ?? '',
          artist: fc.foreignServer.artist ?? '',
          album: '',
          albumArtUrl: this.consumeArt || ALBUM_ART_PLACEHOLDER,
          duration: durMs / 1000,
        },
        isPlaying: playing,
        progress: posMs / 1000,
        volume: this.consumeCtrl?.volume ?? undefined,
        supportedCommands: cc?.commands ?? [],
        repeat: cc?.repeat ?? undefined,
        shuffle: cc?.shuffle ?? undefined,
      });
      fc.currentStreamId = sid; // makes it the featured stream for the local page
    }
    return model;
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

  /** Per-endpoint output level (Sendspin player role, via the mesh REST surface). */
  async setVolume(playerId: string, volume: number, muted = false): Promise<void> {
    const unitId = this.playerUnit(playerId);
    if (!unitId) return;
    // Hold what the user just chose until the poll catches up: the round trip is player → server
    // → next /view poll, so without this a drag fights the last polled value and the knob jumps.
    this.pendingVolume.set(playerId, { volume, muted, at: Date.now() });
    await this.post(unitId, '/volume', { player_id: playerId, volume, muted });
  }

  /**
   * The volume ON THE SENDING DEVICE for a source — the phone's AirPlay/Bluetooth slider, the
   * Spotify Connect device volume. Nothing to do with our own output gain: the two stack, and only
   * this one is visible on the sender's screen. Not part of Sendspin, hence our own endpoint.
   */
  async setSourceVolume(federatedStreamId: string, volume: number, muted?: boolean): Promise<void> {
    if (federatedStreamId.startsWith(FOREIGN_PREFIX)) return; // a foreign server owns its own sender
    const { unitId, sourceId } = parseStreamId(federatedStreamId);
    this.pendingSourceVolume.set(federatedStreamId, { volume, at: Date.now() });
    await this.post(unitId, '/source-volume', { source_id: sourceId, volume, muted });
  }

  controlStream(federatedStreamId: string, command: ControllerCommand): void {
    if (federatedStreamId.startsWith(FOREIGN_PREFIX)) {
      this.sendForeignCommand(command);
      return;
    }
    const controller = this.controllers.get(federatedStreamId);
    if (!controller) return;
    controller.send(command);
    // Flip the transport button immediately; the AirPlay pause round-trip is multi-second.
    // applyOptimisticTransport emits through the onUpdate callback → onNowPlaying → npByGroup.
    if (command === 'play' || command === 'pause') controller.applyOptimisticTransport(command);
  }

  /**
   * GROUP volume for a source — every endpoint rendering it, over the protocol.
   *
   * The controller `volume` command is not ours to interpret: aiosendspin's controller group role
   * redistributes it across the group's players preserving their relative levels, then republishes
   * the average as the group volume. So this is one command, not a per-client fan-out, and it works
   * identically against a foreign server (Music Assistant) via the consume relay.
   */
  setStreamVolume(federatedStreamId: string, volume: number): void {
    if (federatedStreamId.startsWith(FOREIGN_PREFIX)) {
      this.sendForeignCommand('volume', { volume });
      return;
    }
    this.controllers.get(federatedStreamId)?.send('volume', { volume });
  }

  /** Group mute for a source (all its endpoints), same protocol path as setStreamVolume. */
  setStreamMute(federatedStreamId: string, mute: boolean): void {
    if (federatedStreamId.startsWith(FOREIGN_PREFIX)) {
      this.sendForeignCommand('mute', { mute });
      return;
    }
    this.controllers.get(federatedStreamId)?.send('mute', { mute });
  }

  getStreamCapabilities(federatedStreamId: string): {
    canPlay: boolean;
    canPause: boolean;
    canSeek: boolean;
    canGoNext: boolean;
    canGoPrevious: boolean;
    canRepeat: boolean;
    canShuffle: boolean;
  } {
    const caps = (cmds: string[]) => ({
      canPlay: cmds.includes('play'),
      canPause: cmds.includes('pause'),
      canSeek: false, // no seek in the Sendspin controller protocol
      canGoNext: cmds.includes('next'),
      canGoPrevious: cmds.includes('previous'),
      // A source advertises the repeat set as three commands (off/one/all) and shuffle as
      // shuffle+unshuffle; presence of any one is enough to show the control.
      canRepeat: cmds.some((c) => c.startsWith('repeat')),
      canShuffle: cmds.includes('shuffle') || cmds.includes('unshuffle'),
    });
    if (federatedStreamId.startsWith(FOREIGN_PREFIX)) {
      return caps(this.consumeCtrl?.commands ?? []);
    }
    const { unitId, sourceId } = parseStreamId(federatedStreamId);
    const src = this.lastView.units
      .find((u) => u.unit_id === unitId)
      ?.sources.find((s) => s.source_id === sourceId);
    return caps((src && this.npByGroup.get(src.group_id)?.supportedCommands) || []);
  }

  /**
   * Latest native visualizer frame for a stream (source), or null if none/idle. Read by the
   * visualizer via requestAnimationFrame rather than pushed through React state — the frames arrive
   * ~30×/s and would otherwise trigger a full re-render each time.
   */
  getVizFrame(federatedStreamId: string): VizFrame | null {
    if (federatedStreamId.startsWith(FOREIGN_PREFIX)) return this.consumeViz;
    return this.controllers.get(federatedStreamId)?.vizFrame ?? null;
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
    try {
      const res = await fetch(`${MESH_API_BASE}/neighbourhood`);
      if (res.ok) this.neighbourhood = await res.json();
    } catch {
      // discovery is optional; the mesh still renders without it.
    }
  }

  /** Pull a foreign Sendspin speaker onto one of our sources (it leaves its own server). */
  async adoptSpeaker(url: string, federatedStreamId: string): Promise<void> {
    const { unitId, sourceId } = parseStreamId(federatedStreamId);
    await this.post(unitId, "/adopt", { url, source_id: sourceId });
  }

  /** Hand a foreign speaker back to whatever server it came from. */
  async releaseSpeaker(playerId: string, federatedStreamId: string, url?: string): Promise<void> {
    const { unitId, sourceId } = parseStreamId(federatedStreamId);
    await this.post(unitId, "/release", { player_id: playerId, source_id: sourceId, url });
  }

  private applyView(view: MeshView): void {
    this.lastView = view;
    this.rememberPlayerNames(view);
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
          clientId: `ctrl:${src.source_id}:${stableClientNonce(src.source_id)}`,
          // Negotiate the native visualizer role on every source controller. The server only sends
          // spectrum/loudness while that source is actually streaming, so an idle source costs
          // nothing — and whichever source the user opens the visualizer on already has its frames.
          visualizer: true,
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
