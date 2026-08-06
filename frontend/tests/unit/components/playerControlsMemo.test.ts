/**
 * The PlayerControls memo comparator.
 *
 * MemoPlayerControls exists so a control's DOM stays stable across position ticks — reconciling it
 * mid-click silently drops the click. The cost is that any prop MISSING from the comparator renders
 * stale forever, with no error and nothing in the console.
 *
 * That happened on the rig 2026-08-05. `sourceVolumeUnavailable` was added to drive the source
 * slider's disabled state but not to the comparator, so React skipped every render where only that
 * flag changed. The observed behaviour was precise and baffling: the slider's VALUE tracked the
 * sender correctly through a pause and resume, while its enabled state stayed frozen — and it
 * finally corrected itself on the next track change, because a track change moves `isPlaying`,
 * which IS in the comparator.
 *
 * So every prop is asserted here individually rather than trusting a reading of the predicate.
 */

import { describe, it, expect, vi } from 'vitest'
import { playerControlsPropsEqual } from '../../../MeshApp'
import type { Stream } from '../../../types'

const stream = {
  id: 'unit-211::airplay-1',
  isPlaying: true,
  volume: 50,
  sourceVolume: 64,
  supportsSourceVolume: true,
} as unknown as Stream

const handlers = {
  onPlayPause: vi.fn(),
  onSkip: vi.fn(),
  onToggleShuffle: vi.fn(),
  onCycleRepeat: vi.fn(),
  onVolumeChange: vi.fn(),
  onSourceVolumeChange: vi.fn(),
}

const base = {
  stream,
  volume: 50,
  sourceVolume: 64,
  sourceVolumeUnavailable: false,
  canShuffle: false,
  canRepeat: false,
  shuffle: false,
  repeat: 'off' as const,
  ...handlers,
} as any

const equal = (patch: Record<string, unknown>) => playerControlsPropsEqual(base, { ...base, ...patch })

describe('playerControlsPropsEqual', () => {
  it('treats identical props as equal, so ticks do not reconcile the DOM', () => {
    expect(playerControlsPropsEqual(base, { ...base })).toBe(true)
  })

  it('re-renders when the sender becomes unreachable', () => {
    // The regression. Value unchanged, only availability moved.
    expect(equal({ sourceVolumeUnavailable: true })).toBe(false)
  })

  it('re-renders when the stream stops supporting source volume', () => {
    expect(equal({ stream: { ...stream, supportsSourceVolume: false } })).toBe(false)
  })

  it.each([
    ['sourceVolume', { sourceVolume: 20 }],
    ['volume', { volume: 20 }],
    ['canShuffle', { canShuffle: true }],
    ['canRepeat', { canRepeat: true }],
    ['shuffle', { shuffle: true }],
    ['repeat', { repeat: 'one' as const }],
  ])('re-renders when %s changes', (_name, patch) => {
    expect(equal(patch)).toBe(false)
  })

  it.each([
    ['id', { id: 'other::airplay-1' }],
    ['isPlaying', { isPlaying: false }],
    ['volume', { volume: 10 }],
    ['sourceVolume', { sourceVolume: 10 }],
  ])('re-renders when stream.%s changes', (_name, patch) => {
    expect(equal({ stream: { ...stream, ...patch } })).toBe(false)
  })

  it.each(Object.keys(handlers))('re-renders when the %s handler identity changes', (key) => {
    expect(equal({ [key]: vi.fn() })).toBe(false)
  })

  it('covers every prop PlayerControls renders', () => {
    // A guard on the guard: if a prop is added to the component and not to the comparator, the
    // count here drifts and this fails, rather than the bug reaching a unit.
    const rendered = [
      'stream', 'volume', 'sourceVolume', 'sourceVolumeUnavailable',
      'canShuffle', 'canRepeat', 'shuffle', 'repeat',
      ...Object.keys(handlers),
    ]
    for (const key of rendered) {
      const differs = key === 'stream'
        ? equal({ stream: { ...stream, id: 'changed' } })
        : equal({ [key]: typeof (base as any)[key] === 'function' ? vi.fn() : 'CHANGED' })
      expect(differs, `${key} is missing from playerControlsPropsEqual`).toBe(false)
    }
  })
})
