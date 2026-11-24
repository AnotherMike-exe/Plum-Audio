/**
 * Exponential backoff for one repeated request, with a cap and an in-flight guard.
 *
 * Written for MeshApp's browser-player reconciler. That effect runs on every model emit — twice a
 * second — and it re-sent a failing route on every run, for as long as the route failed. The caller
 * asks `ready()` before each attempt and reports the outcome; this class only keeps the time.
 *
 * `begin()` gives a token for the attempt, and the caller gives it back with the outcome. `reset()`
 * makes every older token stale: an outcome that lands after it belongs to a request the caller no
 * longer cares about, and must not move the clock or the count of the new one.
 *
 * Only `reset()` clears the count. A request the unit ACCEPTED is not the result — the caller sees
 * the result on a later poll, and calls `reset()` then. Until it does, an accepted request waits
 * and counts the same as a failed one. It did clear the count before, and a route that was accepted
 * but never took effect was then sent again each second, for ever, with no error.
 *
 * `now` is a parameter everywhere so a test can drive the clock.
 */
export class RetryBackoff {
  private consecutiveFailures = 0;
  private nextAt = 0;
  private inFlight = false;
  private generation = 0;

  constructor(
    private readonly baseMs = 1_000,
    private readonly maxMs = 30_000,
  ) {}

  /** May an attempt start now? False while one is in flight, and until the wait is over. */
  ready(now = Date.now()): boolean {
    return !this.inFlight && now >= this.nextAt;
  }

  /** An attempt started. Give the returned token back to `fail()` or `succeed()`. */
  begin(): number {
    this.inFlight = true;
    return this.generation;
  }

  /**
   * The attempt failed: wait base, 2x base, 4x base ... up to the cap.
   * Returns false, and changes nothing, when `attempt` is from before the last `reset()`.
   */
  fail(attempt: number, now = Date.now()): boolean {
    return this.settle(attempt, now);
  }

  /**
   * The request was accepted, and the caller did not see the result yet. Wait and count in the same
   * way as a failure: one base interval the first time, so the view can catch up, then longer.
   * Returns false, and changes nothing, when `attempt` is from before the last `reset()`.
   */
  succeed(attempt: number, now = Date.now()): boolean {
    return this.settle(attempt, now);
  }

  /** Forget everything — the result landed, a new target, or the request is no longer necessary. */
  reset(): void {
    this.generation++;
    this.inFlight = false;
    this.consecutiveFailures = 0;
    this.nextAt = 0;
  }

  private settle(attempt: number, now: number): boolean {
    if (attempt !== this.generation) return false;
    this.inFlight = false;
    this.nextAt = now + Math.min(this.baseMs * 2 ** this.consecutiveFailures, this.maxMs);
    this.consecutiveFailures++;
    return true;
  }

  /** Attempts in a row that did not land (failed, or accepted with no result) since the last reset. */
  get failures(): number {
    return this.consecutiveFailures;
  }
}
