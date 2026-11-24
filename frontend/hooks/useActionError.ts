/**
 * useActionError — one place for a user action's failure to go.
 *
 * Every mesh command (volume, route, adopt, release) is a promise that rejects when the unit refuses
 * or cannot be reached. The call sites used to fire them with a bare `void`: the rejection went
 * nowhere, and the user saw a slider that moved back, or a device that did not move, with no reason.
 * `guard` takes the promise, catches the rejection, and holds a sentence for ActionErrorToast.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

/** How long a message stays before it clears itself. Long enough to read one sentence twice. */
export const ACTION_ERROR_MS = 8_000;

/** "<what did not happen>: <why>" — the reason is the unit's own text when it sent one. */
export function describeActionError(action: string, err: unknown): string {
  const reason = err instanceof Error ? err.message.trim() : '';
  return reason ? `${action}: ${reason}` : `${action}.`;
}

export interface ActionErrorState {
  /** The current message, or null. */
  error: string | null;
  /** Show a failure. `action` says what did not happen, in the user's terms. */
  report: (action: string, err: unknown) => void;
  /** Catch `promise`'s rejection and report it. Never rejects, so the caller needs no `void`. */
  guard: (promise: Promise<unknown>, action: string) => void;
  dismiss: () => void;
}

export function useActionError(): ActionErrorState {
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const clearTimer = useCallback(() => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
  }, []);

  const dismiss = useCallback(() => {
    clearTimer();
    setError(null);
  }, [clearTimer]);

  const report = useCallback((action: string, err: unknown) => {
    clearTimer();
    setError(describeActionError(action, err));
    timer.current = setTimeout(() => {
      timer.current = null;
      setError(null);
    }, ACTION_ERROR_MS);
  }, [clearTimer]);

  const guard = useCallback((promise: Promise<unknown>, action: string) => {
    promise.catch((err) => report(action, err));
  }, [report]);

  useEffect(() => clearTimer, [clearTimer]);

  return { error, report, guard, dismiss };
}
