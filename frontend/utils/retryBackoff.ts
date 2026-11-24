/**
 * Exponential backoff for one repeated request, with a cap and an in-flight guard.
 *
 * Written for MeshApp's browser-player reconciler. That effect runs on every model emit — twice a
 * second — and it re-sent a failing route on every run, for as long as the route failed. The caller
 * asks `ready()` before each attempt and reports the outcome; this class only keeps the time.
 *
 * `now` is a parameter everywhere so a test can drive the clock.
 */
export class RetryBackoff {
  private consecutiveFailures = 0;
  private nextAt = 0;
  private inFlight = false;

  constructor(
    private readonly baseMs = 1_000,
    private readonly maxMs = 30_000,
  ) {}

  /** May an attempt start now? False while one is in flight, and until the wait is over. */
  ready(now = Date.now()): boolean {
    return !this.inFlight && now >= this.nextAt;
  }

  /** An attempt started. */
  begin(): void {
    this.inFlight = true;
  }

  /** The attempt failed: wait base, 2x base, 4x base ... up to the cap. */
  fail(now = Date.now()): void {
    this.inFlight = false;
    this.nextAt = now + Math.min(this.baseMs * 2 ** this.consecutiveFailures, this.maxMs);
    this.consecutiveFailures++;
  }

  /**
   * The attempt succeeded. Still wait one base interval: the caller sees the result only on a later
   * poll, and until then it would send the same request again on every run.
   */
  succeed(now = Date.now()): void {
    this.inFlight = false;
    this.consecutiveFailures = 0;
    this.nextAt = now + this.baseMs;
  }

  /** Forget everything — a new target, or the request is no longer necessary. */
  reset(): void {
    this.inFlight = false;
    this.consecutiveFailures = 0;
    this.nextAt = 0;
  }

  /** Failures in a row since the last success or reset. */
  get failures(): number {
    return this.consecutiveFailures;
  }
}
