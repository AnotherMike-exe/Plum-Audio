/**
 * useBrowserPlayer — React state around a single SendspinBrowserPlayer (the "Listen in Browser" tab).
 *
 * Replaces the Snapcast-era useBrowserAudioClient. Exposes the tab's Sendspin client id so the caller
 * can find its own row in the mesh view and drive the "route to none => tear down" behaviour.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { SendspinBrowserPlayer, warmBrowserPlayerModule } from '../services/sendspinBrowserPlayer';

export interface BrowserPlayerState {
  active: boolean;
  starting: boolean;
  playerId: string | null;
  /** Increments each time the SDK auto-reconnects after an unexpected WS drop. The reconnected
   *  client lands idle, so the app watches this to re-route the tab back onto its source. */
  reconnectSeq: number;
  /** Begin playing in this tab from `host`'s Sendspin server. Resolves with the tab's Sendspin
   *  client id once connected. Call directly from the button onClick (its gesture unlocks the
   *  AudioContext). */
  start: (host: string) => Promise<string | null>;
  /** Tear the tab's player down (explicit Stop, or routed to none). */
  stop: () => void;
}

export function useBrowserPlayer(): BrowserPlayerState {
  const ref = useRef<SendspinBrowserPlayer | null>(null);
  const [active, setActive] = useState(false);
  const [starting, setStarting] = useState(false);
  const [playerId, setPlayerId] = useState<string | null>(null);
  const [reconnectSeq, setReconnectSeq] = useState(0);

  // Warm the module once so the first click's dynamic import is already resolved (keeps unlock() in
  // the gesture window). Does not load the opus WASM (we use flac/pcm).
  useEffect(() => {
    void warmBrowserPlayerModule();
  }, []);

  const stop = useCallback(() => {
    ref.current?.stop('user_request');
    ref.current = null;
    setActive(false);
    setStarting(false);
    setPlayerId(null);
    setReconnectSeq(0);
  }, []);

  const start = useCallback(async (host: string): Promise<string | null> => {
    if (ref.current) return ref.current.playerId; // already listening in this tab
    const p = new SendspinBrowserPlayer(host, { onReconnected: () => setReconnectSeq((n) => n + 1) });
    ref.current = p;
    setPlayerId(p.playerId); // expose the id up-front so the caller can watch its own row
    setStarting(true);
    try {
      await p.start();
      setActive(true);
      return p.playerId;
    } catch (err) {
      console.error('[BrowserPlayer] start failed:', err);
      p.stop('user_request');
      ref.current = null;
      setActive(false);
      setPlayerId(null);
      throw err;
    } finally {
      setStarting(false);
    }
  }, []);

  // Tear down if the component using the hook unmounts.
  useEffect(() => () => ref.current?.stop('shutdown'), []);

  return { active, starting, playerId, reconnectSeq, start, stop };
}
