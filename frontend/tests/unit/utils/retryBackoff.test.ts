import { describe, it, expect } from 'vitest';
import { RetryBackoff } from '../../../utils/retryBackoff';

/** Drive the backoff the way the browser-player reconciler does: one look every 500 ms. */
function attemptsOver(durationMs: number, backoff: RetryBackoff, outcome: 'fail' | 'succeed'): number[] {
  const attemptTimes: number[] = [];
  for (let now = 0; now <= durationMs; now += 500) {
    if (!backoff.ready(now)) continue;
    backoff.begin();
    attemptTimes.push(now);
    if (outcome === 'fail') backoff.fail(now);
    else backoff.succeed(now);
  }
  return attemptTimes;
}

describe('RetryBackoff', () => {
  it('is ready at once the first time', () => {
    expect(new RetryBackoff().ready(0)).toBe(true);
  });

  it('doubles the wait after each failure, up to the cap', () => {
    const times = attemptsOver(120_000, new RetryBackoff(1_000, 30_000), 'fail');
    const gaps = times.slice(1).map((t, i) => t - times[i]);
    expect(gaps.slice(0, 6)).toEqual([1_000, 2_000, 4_000, 8_000, 16_000, 30_000]);
    expect(Math.max(...gaps)).toBe(30_000);
  });

  it('turns a failing route into a few requests, not two each second', () => {
    // Before: one POST for each 500 ms tick — 121 in a minute. Now: 1, 2, 4, 8, 16, 30 s apart.
    const times = attemptsOver(60_000, new RetryBackoff(1_000, 30_000), 'fail');
    expect(times.length).toBeLessThanOrEqual(7);
  });

  it('does not start a second attempt while one is in flight', () => {
    const backoff = new RetryBackoff();
    backoff.begin();
    expect(backoff.ready(1_000_000)).toBe(false);
    backoff.fail(1_000_000);
    expect(backoff.ready(1_000_000)).toBe(false);
    expect(backoff.ready(1_001_000)).toBe(true);
  });

  it('counts the failures in a row', () => {
    const backoff = new RetryBackoff();
    backoff.begin();
    backoff.fail(0);
    backoff.begin();
    backoff.fail(0);
    expect(backoff.failures).toBe(2);
    backoff.begin();
    backoff.succeed(0);
    expect(backoff.failures).toBe(0);
  });

  it('waits one base interval after a success, so the view can catch up', () => {
    const backoff = new RetryBackoff(1_000, 30_000);
    backoff.begin();
    backoff.succeed(5_000);
    expect(backoff.ready(5_500)).toBe(false);
    expect(backoff.ready(6_000)).toBe(true);
  });

  it('starts again from the base after reset()', () => {
    const backoff = new RetryBackoff(1_000, 30_000);
    for (let i = 0; i < 6; i++) {
      backoff.begin();
      backoff.fail(0);
    }
    backoff.reset();
    expect(backoff.ready(0)).toBe(true);
    expect(backoff.failures).toBe(0);
  });
});
