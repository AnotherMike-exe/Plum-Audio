/**
 * Unit tests for OutputDeviceSection — the REAL component, not a mock of it.
 *
 * Deliberately a real component test. Four suites in this repo (NowPlaying, PlayerControls,
 * integrationsService, settingsService) define a mock of the thing they claim to cover and assert
 * against that, so they pass no matter what production does; this feature should not add a fifth.
 * Only `audioService` is stubbed — the network boundary, not the component.
 *
 * What is worth guarding: the two kinds of pending must not look alike. A device-to-device switch
 * resolves on its own and gets a spinner. Crossing to or from "No output" waits for a container
 * restart and gets a static warning — a spinner there promises something that will never happen.
 */

import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import { OutputDeviceSection } from '../../../components/settings/OutputDeviceSection'
import { audioService, DeviceType, type AudioDevice, type CurrentOutput } from '../../../services/audioService'

const HAT: AudioDevice = {
  id: 'sndrpihifiberry:0',
  hwId: 'hw:2,0',
  friendlyName: 'HiFiBerry DAC+ Pro (HAT)',
  type: DeviceType.HAT,
  isAvailable: true,
  unavailableReason: null,
  inUse: false,
  isActive: true,
}

const NONE: AudioDevice = {
  id: 'none',
  hwId: '',
  friendlyName: 'No output',
  type: DeviceType.NONE,
  isAvailable: true,
  unavailableReason: null,
  inUse: false,
  isActive: false,
}

const current = (over: Partial<CurrentOutput> = {}): CurrentOutput => ({
  configured: 'sndrpihifiberry:0',
  playingOn: 'sndrpihifiberry:0',
  pending: false,
  restartRequired: false,
  resolved: true,
  friendlyName: 'HiFiBerry DAC+ Pro (HAT)',
  unavailableReason: null,
  ...over,
})

function stub(devices: AudioDevice[], output: CurrentOutput) {
  vi.spyOn(audioService, 'getOutputDevices').mockResolvedValue(devices)
  vi.spyOn(audioService, 'getCurrentOutput').mockResolvedValue(output)
}

beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true }))
afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('OutputDeviceSection', () => {
  it('lists real hardware and the "No output" row together', async () => {
    stub([HAT, NONE], current())
    render(<OutputDeviceSection />)

    await screen.findByText('HiFiBerry DAC+ Pro (HAT)')
    // Scoped to the rows: "No output" also appears in the section blurb above them, and asserting a
    // bare count would silently pass if a row vanished and some other copy gained the phrase.
    const rows = screen.getAllByRole('button').filter(b => b.textContent?.includes('HAT')
      || b.textContent?.includes('No output'))
    expect(rows.map(r => within(r).getAllByText(/HAT|No output/)[0].textContent))
      .toEqual(['HiFiBerry DAC+ Pro (HAT)', 'No output'])
  })

  it('does not offer a test tone on the "No output" row', async () => {
    // There is nothing to play a tone through; the backend 409s it, so the button must not exist.
    // The HAT is made non-active here on purpose — otherwise NEITHER row offers a tone (the active
    // device is excluded too) and the assertion would pass without proving anything.
    stub([{ ...HAT, isActive: false }, NONE], current({ configured: 'none', playingOn: 'none' }))
    render(<OutputDeviceSection />)
    await screen.findByText('No output')

    const tones = await screen.findAllByText('Play test tone')
    expect(tones).toHaveLength(1)
    expect(tones[0].closest('div')?.parentElement?.textContent).toContain('HiFiBerry')
  })

  it('renders no empty hw-address line for the synthetic row', async () => {
    stub([HAT, NONE], current())
    const { container } = render(<OutputDeviceSection />)
    await screen.findByText('No output')

    const monoLines = Array.from(container.querySelectorAll('.font-mono')).map(n => n.textContent)
    expect(monoLines).toEqual(['hw:2,0'])  // the HAT's, and nothing blank for "No output"
  })

  it('warns without a spinner when a restart is required', async () => {
    stub([HAT, NONE], current({
      configured: 'none', friendlyName: 'No output', pending: true, restartRequired: true,
    }))
    const { container } = render(<OutputDeviceSection />)

    await waitFor(() => expect(screen.getByText(/Restart this unit/)).toBeTruthy())
    expect(screen.getByText(/still playing through/)).toBeTruthy()
    expect(container.querySelector('.animate-spin')).toBeNull()  // nothing is in flight
  })

  it('keeps the spinner for an ordinary device-to-device switch', async () => {
    stub([HAT, NONE], current({
      configured: 'Headphones:0', friendlyName: 'Built-in Headphones', pending: true, restartRequired: false,
    }))
    const { container } = render(<OutputDeviceSection />)

    await waitFor(() => expect(screen.getByText(/Switching to/)).toBeTruthy())
    expect(container.querySelector('.animate-spin')).toBeTruthy()
    expect(screen.queryByText(/Restart this unit/)).toBeNull()
  })

  it('explains what a unit with no playback hardware can still do', async () => {
    // The old copy here was "No playback devices found on this unit." — true, and misleading: it
    // sounds broken, when the unit is a perfectly good ingest and routing node.
    stub([NONE], current({ configured: 'none', friendlyName: 'No output' }))
    render(<OutputDeviceSection />)

    expect(await screen.findByText(/No playback hardware was detected/)).toBeTruthy()
    expect(screen.getByText(/send them to other rooms/)).toBeTruthy()
  })

  it('says nothing about missing hardware when a device is present', async () => {
    stub([HAT, NONE], current())
    render(<OutputDeviceSection />)
    await screen.findByText('HiFiBerry DAC+ Pro (HAT)')

    expect(screen.queryByText(/No playback hardware was detected/)).toBeNull()
  })
})
