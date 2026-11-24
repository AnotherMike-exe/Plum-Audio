import React, { useEffect, useRef, useState } from 'react';
import type { Client } from '../types';
import { Icon } from './Icon';

export type PairMethod = 'pairing_psk' | 'dynamic_pin' | 'static_pin';

interface PairDeviceDialogProps {
  client: Client;
  /** Begin an attempt. Resolves once the unit has ACCEPTED it — pairing then runs in the background. */
  onStart: (method: PairMethod, token?: string) => Promise<{ ok: boolean; message?: string }>;
  /** Answer a PIN prompt. `ok:false` with a message is a wrong PIN or a dead attempt, not a crash. */
  onSubmitPin: (pin: string) => Promise<{ ok: boolean; message?: string }>;
  /** Poll the attempt's outcome. Pairing completes on the unit, not in this dialog. */
  onPoll: () => Promise<{ state: string; error?: string }>;
  onCancel: () => void;
  onDone: () => void;
}

const POLL_MS = 1000;
const FOCUSABLE = 'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [href], [tabindex]:not([tabindex="-1"])';

const focusableIn = (root: HTMLElement): HTMLElement[] => Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE));

/**
 * Pair one Sendspin device.
 *
 * The flow is not request/response, and the dialog is shaped around that: `pair` only *starts* an
 * attempt, because the exchange includes a PAKE round and a wait on a human reading a PIN off a
 * speaker. So this starts, then polls, and the terminal state arrives from the unit.
 *
 * Three methods, in the order an operator is likely to want them:
 *   - **Dynamic PIN** — the device shows a PIN, you type it here. Works for anything with a display.
 *   - **PIN entry** (static) — the device has a fixed 8-digit PIN printed on it or in its own UI.
 *   - **Token** — the device shows a `SP:` token or QR; paste it. No interaction after that.
 */
export const PairDeviceDialog: React.FC<PairDeviceDialogProps> = ({
  client, onStart, onSubmitPin, onPoll, onCancel, onDone,
}) => {
  const [method, setMethod] = useState<PairMethod>('dynamic_pin');
  const [token, setToken] = useState('');
  const [pin, setPin] = useState('');
  const [phase, setPhase] = useState<'choose' | 'running' | 'pin' | 'done' | 'failed'>('choose');
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const panelRef = useRef<HTMLDivElement | null>(null);

  // While an attempt runs, a stray click outside the panel must not abandon it: cancel tells the unit
  // to drop the attempt, and the device then needs the whole exchange again. Cancel stays one
  // deliberate act away — the Cancel button, or Escape.
  const attemptLive = phase === 'running' || phase === 'pin';
  // Once paired there is nothing to cancel, so every way out is "done".
  const dismiss = phase === 'done' ? onDone : onCancel;

  // While an attempt runs, the unit is the source of truth for what is happening — including
  // "awaiting_pin", which is how we learn the device has DISPLAYED a PIN and it is worth asking for.
  useEffect(() => {
    if (phase !== 'running' && phase !== 'pin') return;
    // `cancelled` drops a response that lands after this effect ended (dialog closed, phase moved,
    // outcome known). `inFlight` stops a slow unit from having two polls open at once: with both
    // open, the slower answer landed last, and a late "failed" replaced a "paired" on screen.
    let cancelled = false;
    let inFlight = false;
    const stop = () => {
      cancelled = true;
      window.clearInterval(timer);
    };
    const tick = async () => {
      if (inFlight || cancelled) return;
      inFlight = true;
      let state: { state: string; error?: string };
      try {
        state = await onPoll();
      } catch {
        return; // a failed poll is not an outcome — the next tick asks again
      } finally {
        inFlight = false;
      }
      if (cancelled) return;
      if (state.state === 'awaiting_pin') setPhase('pin');
      else if (state.state === 'paired') { stop(); setPhase('done'); }
      else if (state.state === 'failed' || state.state === 'cancelled') {
        stop();
        setMessage(state.error ?? 'Pairing did not complete.');
        setPhase('failed');
      }
    };
    const timer = window.setInterval(() => void tick(), POLL_MS);
    return stop;
  }, [phase, onPoll]);

  // Focus: into the dialog when it opens, back to whatever opened it when it closes.
  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const panel = panelRef.current;
    if (panel && !panel.contains(document.activeElement)) (focusableIn(panel)[0] ?? panel).focus();
    return () => opener?.focus();
  }, []);

  // A phase change replaces the controls (Cancel/Pair become Done), and focus on a removed element
  // falls to <body> — outside the dialog. Put it back.
  useEffect(() => {
    const panel = panelRef.current;
    if (panel && !panel.contains(document.activeElement)) (focusableIn(panel)[0] ?? panel).focus();
  }, [phase]);

  // Escape closes; Tab stays inside. On the document, so both still work when focus was lost.
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        dismiss();
        return;
      }
      const panel = panelRef.current;
      if (e.key !== 'Tab' || !panel) return;
      const items = focusableIn(panel);
      const first = items[0] ?? panel;
      const last = items[items.length - 1] ?? panel;
      const active = document.activeElement;
      if (!panel.contains(active)) {
        e.preventDefault();
        (e.shiftKey ? last : first).focus();
      } else if (e.shiftKey && (active === first || active === panel)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [dismiss]);

  const start = async () => {
    setBusy(true);
    setMessage(null);
    const res = await onStart(method, method === 'pairing_psk' ? token.trim() : undefined);
    setBusy(false);
    if (!res.ok) {
      setMessage(res.message ?? 'Could not start pairing.');
      setPhase('failed');
      return;
    }
    setPhase('running');
  };

  const sendPin = async () => {
    setBusy(true);
    setMessage(null);
    const res = await onSubmitPin(pin.trim());
    setBusy(false);
    if (!res.ok) {
      // Deliberately kept distinct from a wrong PIN by the message the unit sends: "nothing is
      // waiting for a PIN" means the attempt timed out, and retyping will never work.
      setMessage(res.message ?? 'That PIN was not accepted.');
      setPin('');
    }
  };

  const canStart = method !== 'pairing_psk' || token.trim().length > 0;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-xs"
      onClick={attemptLive ? undefined : dismiss}
      role="dialog"
      aria-modal="true"
      aria-labelledby="pair-dialog-title"
    >
      <div
        ref={panelRef}
        tabIndex={-1}
        className="relative w-[380px] bg-(--bg-secondary) rounded-2xl shadow-2xl border border-(--border-color) p-6 outline-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 id="pair-dialog-title" className="text-lg font-semibold text-(--text-primary) mb-1">
          Pair {client.name}
        </h3>
        <p className="text-xs text-(--text-secondary) mb-4">
          This speaker is encrypted and has not been paired with this unit, so it cannot play yet.
        </p>

        {phase === 'choose' && (
          <div className="space-y-4">
            <div className="space-y-2">
              {([
                ['dynamic_pin', 'The device shows a PIN', 'It displays a code; you type it here.'],
                ['static_pin', 'The device has a fixed PIN', 'An 8-digit code printed on it or in its own settings.'],
                ['pairing_psk', 'Paste a pairing token', 'A code starting SP:, shown as text or a QR.'],
              ] as const).map(([value, label, hint]) => (
                <label
                  key={value}
                  className={`flex gap-3 p-2 rounded-lg cursor-pointer border ${
                    method === value
                      ? 'border-(--accent-color) bg-(--bg-tertiary)'
                      : 'border-transparent hover:bg-(--bg-tertiary-hover)'
                  }`}
                >
                  <input
                    type="radio"
                    name="pair-method"
                    className="mt-1"
                    checked={method === value}
                    onChange={() => setMethod(value)}
                  />
                  <span>
                    <span className="block text-sm font-semibold text-(--text-primary)">{label}</span>
                    <span className="block text-xs text-(--text-secondary)">{hint}</span>
                  </span>
                </label>
              ))}
            </div>

            {method === 'pairing_psk' && (
              <input
                type="text"
                value={token}
                onChange={(e) => setToken(e.target.value)}
                placeholder="SP:..."
                aria-label="Pairing token"
                className="w-full px-3 py-2 rounded-lg bg-(--bg-tertiary) text-(--text-primary) border border-(--border-color) font-mono text-sm"
              />
            )}
          </div>
        )}

        {phase === 'running' && (
          <p className="text-sm text-(--text-secondary) py-6 text-center">
            Pairing with {client.name}…
            <span className="block text-xs mt-1">You may need to confirm on the device.</span>
          </p>
        )}

        {phase === 'pin' && (
          <div className="space-y-2 py-2">
            <label htmlFor="pair-pin" className="block text-sm text-(--text-primary)">
              Enter the PIN {client.name} is showing
            </label>
            <input
              id="pair-pin"
              type="text"
              inputMode="numeric"
              autoFocus
              value={pin}
              onChange={(e) => setPin(e.target.value.replace(/\D/g, ''))}
              onKeyDown={(e) => { if (e.key === 'Enter' && pin.trim()) void sendPin(); }}
              className="w-full px-3 py-2 rounded-lg bg-(--bg-tertiary) text-(--text-primary) border border-(--border-color) font-mono text-lg tracking-widest text-center"
            />
          </div>
        )}

        {phase === 'done' && (
          <p className="text-sm text-(--text-primary) py-6 text-center">
            <Icon name="plus" className="mr-2 text-(--accent-color)" />
            {client.name} is paired and can now play.
          </p>
        )}

        {message && <p className="text-xs text-red-400 mt-3">{message}</p>}

        <div className="flex gap-3 pt-5">
          {phase === 'done' ? (
            <button
              onClick={onDone}
              className="flex-1 px-4 py-2 bg-(--accent-color) accent-button-text rounded-lg hover:bg-(--accent-color-hover) transition-colors"
            >
              Done
            </button>
          ) : (
            <>
              <button
                onClick={onCancel}
                className="flex-1 px-4 py-2 bg-(--bg-tertiary) text-(--text-primary) rounded-lg hover:bg-(--bg-tertiary-hover) transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={() => (phase === 'pin' ? void sendPin() : void start())}
                disabled={busy || (phase === 'choose' && !canStart) || (phase === 'pin' && !pin.trim())}
                className="flex-1 px-4 py-2 bg-(--accent-color) accent-button-text rounded-lg hover:bg-(--accent-color-hover) transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {phase === 'pin' ? 'Submit PIN' : phase === 'failed' ? 'Try again' : 'Pair'}
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  );
};
