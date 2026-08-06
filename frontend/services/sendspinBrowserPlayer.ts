/**
 * Browser Sendspin player — turns THIS tab into a `player@v1` client of a unit's Sendspin server.
 *
 * The audio counterpart to sendspinControllerClient.ts: the controller pulls now-playing/metadata,
 * this pulls (and plays) the actual audio via the official @sendspin/sendspin-js player. It dials IN
 * to ws://<host>:8927/sendspin, so the tab shows up in that unit's mesh snapshot as a routable
 * endpoint ("Browser Audio") — routing/volume/removal all ride the existing /api/mesh/* path, no
 * special backend handling (see docs/ARCHITECTURE.md §11).
 *
 * Dialing IN is the inverse of the native player, which servers dial. A tab has no listener socket,
 * so cross-server roam does not apply to it.
 *
 * Codec: PCM FIRST, and that ordering is a sync decision, not a bandwidth one. The server caches
 * outgoing audio per TRANSFORM KEY (codec + rate + depth + channels + frame size), and a client
 * joining a live stream is only handed the live timeline if it shares a key with what is already
 * flowing. The unit's own player takes PCM, so a browser asking for FLAC became the first user of a
 * second key: instead of joining at the playhead it got a fresh transcode fed by a REPLAY of cached
 * PCM, and played out from the start of that replay — landing a variable 0.5-2s behind the speaker
 * depending on how much was cached when Listen was pressed. Sharing the key removes the replay.
 * FLAC stays as a fallback for a link that cannot carry 1.4 Mbit/s; on a LAN, PCM is what the
 * hardware player already receives. Opus is deliberately not advertised, which also keeps the
 * opus-encdec WASM off the wire. The spec orders supported_formats by preference, first wins.
 * Sync: `sync` — the tab tracks the group clock like any other player. This was `quality-local`
 * (loose sync, no pitch/resample artifacts) on the assumption a browser is a casual second-room
 * listen; on hardware that assumption was wrong. People listen in the browser while standing in the
 * same room as the speaker, where `quality-local` drifts ~1 s behind and reads as broken sync rather
 * than as cleaner audio. `sync` trades a possible resample artifact for staying with the room.
 */

import type { Codec, GoodbyeReason, SendspinPlayer } from '@sendspin/sendspin-js';

// Lazily loaded ONCE and cached. Importing the module does NOT pull the opus WASM — that is a nested
// dynamic import inside the decoder, gated on Opus actually being selected (we use flac/pcm). Warming
// this early (see useBrowserPlayer) means the click handler's start() resolves in a microtask, so
// AudioContext.unlock() still runs inside the user-gesture activation window.
let modPromise: Promise<typeof import('@sendspin/sendspin-js')> | null = null;
export function warmBrowserPlayerModule(): Promise<typeof import('@sendspin/sendspin-js')> {
  return (modPromise ??= import('@sendspin/sendspin-js'));
}

const SERVER_PORT = 8927; // per-unit Sendspin server; the browser connects here as a new client

export class SendspinBrowserPlayer {
  /** The Sendspin client_id this tab presents. `browser:` namespaces it away from src:/ctrl:. A fresh
   *  id per instance means a page refresh is a genuinely new client (the old one goodbyes on pagehide). */
  readonly playerId: string;
  private readonly host: string;
  private readonly name: string;
  private readonly codecs: Codec[];
  private readonly onReconnected?: () => void;
  private player: SendspinPlayer | null = null;
  private onUnload: (() => void) | null = null;

  constructor(host: string, opts: { name?: string; codecs?: Codec[]; onReconnected?: () => void } = {}) {
    this.host = host;
    this.name = opts.name ?? 'Browser Audio';
    this.codecs = opts.codecs ?? ['pcm', 'flac'];
    this.onReconnected = opts.onReconnected;
    this.playerId = 'browser:' + Math.random().toString(36).slice(2, 8);
  }

  /**
   * Construct the player and connect. MUST be awaited from within the click handler's gesture chain
   * (unlock() resumes the AudioContext, which browsers only allow under user activation).
   */
  async start(): Promise<void> {
    const { SendspinPlayer: Player } = await warmBrowserPlayerModule();
    this.player = new Player({
      playerId: this.playerId,
      baseUrl: `http://${this.host}:${SERVER_PORT}`, // -> ws://<host>:8927/sendspin
      clientName: this.name,
      codecs: this.codecs,
      correctionMode: 'sync',
      // Persistence left at the SDK default (localStorage). It caches this machine's MEASURED
      // output latency, which is a property of the device and its output path, so re-measuring it
      // per tab was throwing away the one number that most affects how closely we land on the room.
      // (It also persists server-commanded static delays; nothing here sends those.)
      // The SDK auto-reconnects on an unexpected WS drop, but the reconnected client lands in a
      // fresh idle group (the old one was cleaned up after the server's ~30s grace). Surface the
      // event so the app can re-route us back onto our source. See MeshApp's browser reconciler.
      reconnect: { onReconnected: () => this.onReconnected?.() },
    });
    // Tab close/refresh sends no goodbye on its own and the SDK would try to reconnect — send a clean
    // shutdown so the server drops us immediately instead of holding a 30s reconnect grace.
    this.onUnload = () => this.stop('shutdown');
    window.addEventListener('pagehide', this.onUnload);
    window.addEventListener('beforeunload', this.onUnload);
    await this.player.unlock();
    await this.player.connect();
  }

  stop(reason: GoodbyeReason = 'user_request'): void {
    if (this.onUnload) {
      window.removeEventListener('pagehide', this.onUnload);
      window.removeEventListener('beforeunload', this.onUnload);
      this.onUnload = null;
    }
    try {
      this.player?.disconnect(reason);
    } catch {
      /* already gone */
    }
    this.player = null;
  }

  get isConnected(): boolean {
    return !!this.player?.isConnected;
  }
}
