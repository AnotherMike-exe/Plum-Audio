/**
 * Unit tests for audioService.
 *
 * These exercise the SERVICE, not the mock. The previous version of this file asserted a
 * {success, devices} envelope that no version of this API has ever returned, and built object
 * literals inline to assert properties on them — so it passed without touching audioService at all.
 *
 * The cases that matter are the two the UI can get wrong:
 *   - `id` vs `hwId`: the stable identity is the card NAME; the hw address moves when cards
 *     renumber (measured — a HiFiBerry went card 2 -> card 1 across one reboot).
 *   - `pending`: saving a device is not playing it, and a switch that failed stays pending rather
 *     than resolving. The UI must be able to tell those apart from success.
 */

import { describe, it, expect, afterEach, beforeAll, afterAll } from 'vitest'
import { server, resetMockState, setMockPlayingOn } from '../../mocks/mockFetch'
import { audioService, DeviceType } from '../../../services/audioService'

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => {
  server.resetHandlers()
  resetMockState()
})
afterAll(() => server.close())

describe('audioService', () => {
  describe('device listing', () => {
    it('maps the API payload to camelCase', async () => {
      const devices = await audioService.getOutputDevices()

      expect(devices).toHaveLength(2)
      expect(devices[0]).toMatchObject({
        id: 'Headphones:0',
        hwId: 'hw:0,0',
        friendlyName: 'Built-in Headphones (3.5mm)',
        type: DeviceType.BUILTIN_HEADPHONES,
        isAvailable: true,
        inUse: true,
        isActive: true
      })
    })

    it('keeps the stable id distinct from the hw address', async () => {
      const [headphones] = await audioService.getOutputDevices()

      expect(headphones.id).toBe('Headphones:0')
      expect(headphones.id).not.toContain('hw:')  // persisting hw: would follow a renumbering
    })

    it('carries the reason a device cannot be selected', async () => {
      const devices = await audioService.getOutputDevices()
      const hdmi = devices.find(d => d.id === 'vc4hdmi0:0')!

      expect(hdmi.isAvailable).toBe(false)
      expect(hdmi.unavailableReason).toMatch(/no display is attached/)
    })
  })

  describe('current output', () => {
    it('is not pending when the echo matches the choice', async () => {
      const current = await audioService.getCurrentOutput()

      expect(current.configured).toBe('Headphones:0')
      expect(current.playingOn).toBe('Headphones:0')
      expect(current.pending).toBe(false)
    })

    it('is pending while the player still has another device open', async () => {
      setMockPlayingOn('sndrpihifiberry:0')

      const current = await audioService.getCurrentOutput()
      expect(current.pending).toBe(true)
      expect(current.playingOn).toBe('sndrpihifiberry:0')  // where the audio actually is
      expect(current.configured).toBe('Headphones:0')      // where it was asked to go
    })
  })

  describe('selecting a device', () => {
    it('sends the stable id', async () => {
      const result = await audioService.setOutputDevice('Headphones:0')
      expect(result.message).toContain('Headphones:0')
    })

    it('surfaces a refusal rather than reporting success', async () => {
      // The backend 409s an unopenable device instead of persisting it; saving one would leave the
      // unit silent after its next restart with settings.json looking entirely correct.
      await expect(audioService.setOutputDevice('vc4hdmi0:0')).rejects.toThrow(/cannot open/i)
    })

    it('rejects an empty id at the API', async () => {
      await expect(audioService.setOutputDevice('')).rejects.toThrow(/id is required/)
    })
  })

  describe('test tone', () => {
    it('explains why the active device cannot be tested', async () => {
      // speaker-test on a held card returns EBUSY, which reads as "broken" when it means "in use".
      await expect(audioService.testOutputDevice('Headphones:0')).rejects.toThrow(/already this unit's output/)
    })

    it('plays on a device that is not in use', async () => {
      const result = await audioService.testOutputDevice('vc4hdmi0:0')
      expect(result.message).toBe('Test tone sent')
    })
  })

  describe('presentation helpers', () => {
    it('labels each device type', () => {
      expect(audioService.getDeviceTypeLabel(DeviceType.HAT)).toBe('HAT')
      expect(audioService.getDeviceTypeLabel(DeviceType.BUILTIN_HEADPHONES)).toBe('Built-in')
      expect(audioService.getDeviceTypeLabel(DeviceType.BUILTIN_HDMI)).toBe('HDMI')
    })

    it('returns icon names that exist in the icon set', () => {
      const valid = ['headphones', 'desktop', 'volume-high', 'waveform']
      for (const type of Object.values(DeviceType)) {
        expect(valid).toContain(audioService.getDeviceTypeIcon(type))
      }
    })
  })
})
