/**
 * Unit tests for PlayerControls — the REAL component.
 *
 * The previous version defined a MockPlayerControls inside this file and asserted against that, so
 * production could not fail it. `playerControlsMemo.test.ts` beside this covers the memo comparator;
 * this covers what the component actually renders.
 *
 * The behaviours worth pinning are the conditional ones, because every one of them has a history:
 *
 * Repeat/shuffle are shown only when the source ADVERTISES them (Sendspin controller
 * supported_commands) — Spotify does, AirPlay does not. Rendering them unconditionally offers
 * controls the source will ignore.
 *
 * The source-volume row is shown whenever a level is KNOWN and merely DISABLED when the sender is
 * unreachable. It used to be gated on availability, so an AirPlay pause long enough for iOS to drop
 * the RAOP session made the slider vanish on every unit's GUI at once and reappear on resume. A
 * control that disappears mid-session reads as a bug.
 *
 * The endpoint slider is hidden entirely on a unit with no speaker, where it otherwise rendered a
 * phantom 100% that silently did nothing.
 */

import React from 'react'
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { PlayerControls } from '../../../components/PlayerControls'
import type { Stream } from '../../../types'

const stream = (over: Partial<Stream> = {}): Stream =>
  ({ id: 'unit-a::airplay-1', isPlaying: true, ...over } as unknown as Stream)

const baseProps = {
  stream: stream(),
  volume: 40,
  onVolumeChange: vi.fn(),
  onPlayPause: vi.fn(),
  onSkip: vi.fn(),
}

const sliders = () => screen.queryAllByRole('slider') as HTMLInputElement[]
const endpointSlider = () => screen.getByLabelText('Group volume control') as HTMLInputElement

describe('PlayerControls — transport', () => {
  it('reports the skip direction', () => {
    const onSkip = vi.fn()
    render(<PlayerControls {...baseProps} onSkip={onSkip} />)

    const buttons = screen.getAllByRole('button')
    fireEvent.click(buttons[0])
    fireEvent.click(buttons[2])
    expect(onSkip.mock.calls.map(c => c[0])).toEqual(['prev', 'next'])
  })

  it('calls back on play/pause', () => {
    const onPlayPause = vi.fn()
    render(<PlayerControls {...baseProps} onPlayPause={onPlayPause} />)

    fireEvent.click(screen.getAllByRole('button')[1])
    expect(onPlayPause).toHaveBeenCalled()
  })
})

describe('PlayerControls — repeat and shuffle appear only when advertised', () => {
  it('hides both for a source that does not support them (AirPlay)', () => {
    render(<PlayerControls {...baseProps} />)

    expect(screen.queryByLabelText(/shuffle/i)).toBeNull()
    expect(screen.queryByLabelText(/repeat/i)).toBeNull()
  })

  it('shows both for a source that does (Spotify)', () => {
    render(<PlayerControls {...baseProps} canShuffle canRepeat shuffle={false} repeat="off" />)

    expect(screen.getByLabelText('Shuffle off')).toBeTruthy()
    expect(screen.getByLabelText('Repeat off')).toBeTruthy()
  })

  it('reflects the current shuffle and repeat state', () => {
    render(<PlayerControls {...baseProps} canShuffle canRepeat shuffle repeat="one" />)

    expect(screen.getByLabelText('Shuffle on').getAttribute('aria-pressed')).toBe('true')
    expect(screen.getByLabelText('Repeat one').getAttribute('aria-pressed')).toBe('true')
    expect(screen.getByText('1')).toBeTruthy()  // the repeat-one badge
  })

  it('treats repeat "off" as not active', () => {
    render(<PlayerControls {...baseProps} canRepeat repeat="off" />)
    expect(screen.getByLabelText('Repeat off').getAttribute('aria-pressed')).toBe('false')
  })
})

describe('PlayerControls — the source volume row', () => {
  const withSource = {
    ...baseProps,
    sourceVolume: 70,
    onSourceVolumeChange: vi.fn(),
  }

  it('is absent when the source has never reported a level', () => {
    render(<PlayerControls {...baseProps} />)
    expect(sliders()).toHaveLength(1)  // the endpoint slider only
  })

  it('appears once a level is known', () => {
    render(<PlayerControls {...withSource} />)
    expect(sliders()).toHaveLength(2)
    expect(screen.getByText('70%')).toBeTruthy()
  })

  it('STAYS but goes disabled when the sender is unreachable', () => {
    // The regression this guards: gating on availability made the row vanish on an AirPlay pause
    // long enough for iOS to drop the session, then reappear on resume.
    render(<PlayerControls {...withSource} sourceVolumeUnavailable />)

    expect(sliders()).toHaveLength(2)
    const source = sliders().find(s => s.value === '70')!
    expect(source.disabled).toBe(true)
  })

  it('is drivable when the sender is reachable', () => {
    const onSourceVolumeChange = vi.fn()
    render(<PlayerControls {...withSource} onSourceVolumeChange={onSourceVolumeChange} />)

    const source = sliders().find(s => s.value === '70')!
    expect(source.disabled).toBe(false)
    fireEvent.change(source, { target: { value: '55' } })
    expect(onSourceVolumeChange).toHaveBeenCalledWith(55)
  })
})

describe('PlayerControls — the endpoint volume slider', () => {
  it('shows this unit’s own level and drives it', () => {
    const onVolumeChange = vi.fn()
    render(<PlayerControls {...baseProps} onVolumeChange={onVolumeChange} />)

    expect(endpointSlider().value).toBe('40')
    fireEvent.change(endpointSlider(), { target: { value: '65' } })
    expect(onVolumeChange).toHaveBeenCalledWith(65)
  })

  it('is hidden on a unit with no speaker', () => {
    // It rendered a phantom 100% whose onChange found no client, did nothing, and snapped back.
    render(<PlayerControls {...baseProps} hideEndpointVolume />)

    expect(screen.queryByLabelText('Group volume control')).toBeNull()
    expect(sliders()).toHaveLength(0)
  })

  it('still shows the SOURCE slider on a unit with no speaker', () => {
    // Source volume is the sender's own level — nothing to do with having an output.
    render(
      <PlayerControls {...baseProps} hideEndpointVolume sourceVolume={70} onSourceVolumeChange={vi.fn()} />,
    )

    expect(sliders()).toHaveLength(1)
    expect(screen.getByText('70%')).toBeTruthy()
  })
})
