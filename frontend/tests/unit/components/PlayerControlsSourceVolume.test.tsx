/**
 * Source-volume row behaviour in the REAL PlayerControls component.
 *
 * Reported on the rig 2026-08-05: during a long AirPlay pause the source slider vanished from every
 * unit's GUI at once, and came back on resume. iOS drops the RAOP session when a pause runs long
 * enough, shairport then reports PlaybackStatus "Stopped", and `supports_source_volume` goes false —
 * which the call site was using to gate the VALUE, unmounting the whole row and collapsing the
 * layout mid-session.
 *
 * The level itself survives: the server caches it in `_source_volumes` and only clears it when the
 * source is torn down. So the row can stay put and simply grey out.
 *
 * Note this file imports the real component on purpose. `PlayerControls.test.tsx` next to it renders
 * a `MockPlayerControls` declared inside itself and asserts nothing about production code — see
 * docs/TESTING.md.
 */

import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { PlayerControls } from '../../../components/PlayerControls'
import type { Stream } from '../../../types'

const stream = {
  id: 'unit-211::airplay-1',
  serverId: 'unit-211',
  serverName: 'Pi4-01',
  name: 'AirPlay',
  sourceDevice: 'airplay-1',
  active: true,
  isPlaying: true,
  progress: 0,
  currentTrack: { id: 't', title: 'T', artist: 'A', album: 'Al', albumArtUrl: '', duration: 0 },
} as unknown as Stream

function renderControls(props: Record<string, unknown> = {}) {
  return render(
    <PlayerControls
      stream={stream}
      volume={50}
      onVolumeChange={vi.fn()}
      onPlayPause={vi.fn()}
      onSkip={vi.fn()}
      {...props}
    />,
  )
}

const sourceSlider = () => screen.queryByLabelText('Source volume control')

describe('source volume row', () => {
  it('is absent when the source has never reported a level', () => {
    renderControls()
    expect(sourceSlider()).toBeNull()
  })

  it('is present and live when the sender is connected', () => {
    renderControls({ sourceVolume: 64, onSourceVolumeChange: vi.fn() })
    const el = sourceSlider() as HTMLInputElement
    expect(el).toBeInTheDocument()
    expect(el.disabled).toBe(false)
    expect(el.value).toBe('64')
  })

  it('STAYS in the layout when the sender goes away, showing the last known level', () => {
    renderControls({ sourceVolume: 64, onSourceVolumeChange: vi.fn(), sourceVolumeUnavailable: true })
    const el = sourceSlider() as HTMLInputElement
    expect(el).toBeInTheDocument()
    expect(el.value).toBe('64')
  })

  it('is disabled while the sender is away, so it cannot be driven into the void', () => {
    const onSourceVolumeChange = vi.fn()
    renderControls({ sourceVolume: 64, onSourceVolumeChange, sourceVolumeUnavailable: true })
    const el = sourceSlider() as HTMLInputElement

    expect(el.disabled).toBe(true)
    expect(el.getAttribute('aria-disabled')).toBe('true')
    fireEvent.change(el, { target: { value: '20' } })
    expect(onSourceVolumeChange).not.toHaveBeenCalled()
  })

  it('still drives the sender when it is connected', () => {
    const onSourceVolumeChange = vi.fn()
    renderControls({ sourceVolume: 64, onSourceVolumeChange })
    fireEvent.change(sourceSlider() as HTMLInputElement, { target: { value: '20' } })
    expect(onSourceVolumeChange).toHaveBeenCalledWith(20)
  })

  it('explains itself while disabled rather than looking broken', () => {
    const { container } = renderControls({
      sourceVolume: 64, onSourceVolumeChange: vi.fn(), sourceVolumeUnavailable: true,
    })
    expect(container.querySelector('[title*="not connected"]')).not.toBeNull()
  })
})
