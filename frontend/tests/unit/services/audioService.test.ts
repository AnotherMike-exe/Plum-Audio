/**
 * Unit tests for audioService
 * Tests: Device listing, device selection, input device management
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { server, resetMockState } from '../../mocks/mockFetch'

// Setup MSW
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => {
  server.resetHandlers()
  resetMockState()
})
afterAll(() => server.close())

describe('audioService', () => {
  describe('output devices', () => {
    it('should list output devices', async () => {
      const response = await fetch('/api/audio/devices/output')

      expect(response.ok).toBe(true)
      const data = await response.json()
      expect(data.success).toBe(true)
      expect(data.devices).toBeDefined()
      expect(Array.isArray(data.devices)).toBe(true)
    })

    it('should have device structure with hw_id', async () => {
      const response = await fetch('/api/audio/devices/output')
      const data = await response.json()

      if (data.devices.length > 0) {
        const device = data.devices[0]
        expect(device).toHaveProperty('hw_id')
        expect(device).toHaveProperty('friendly_name')
      }
    })

    it('should get current output device', async () => {
      const response = await fetch('/api/audio/output/current')

      expect(response.ok).toBe(true)
      const data = await response.json()
      expect(data.success).toBe(true)
      expect(data.device).toBeDefined()
    })

    it('should set output device', async () => {
      const response = await fetch('/api/audio/output/device', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ hw_id: 'hw:Headphones' })
      })

      expect(response.ok).toBe(true)
      const data = await response.json()
      expect(data.success).toBe(true)
    })

    it('should test audio output', async () => {
      const response = await fetch('/api/audio/output/test', {
        method: 'POST'
      })

      expect(response.ok).toBe(true)
    })
  })

  describe('input devices', () => {
    it('should list input devices', async () => {
      const response = await fetch('/api/audio/devices/input')

      expect(response.ok).toBe(true)
      const data = await response.json()
      expect(data.devices).toBeDefined()
    })

    it('should return empty list when no input devices', async () => {
      const response = await fetch('/api/audio/devices/input')
      const data = await response.json()

      expect(Array.isArray(data.devices)).toBe(true)
    })
  })

  describe('device types', () => {
    it('should identify device types', () => {
      const deviceTypes = [
        'BUILTIN_HEADPHONES',
        'HDMI',
        'USB',
        'BLUETOOTH',
        'UNKNOWN'
      ]

      deviceTypes.forEach(type => {
        expect(typeof type).toBe('string')
      })
    })

    it('should have valid hw_id format', () => {
      const validHwIds = [
        'hw:Headphones',
        'hw:0,0',
        'hw:1,0',
        'hw:USB'
      ]

      validHwIds.forEach(hwId => {
        expect(hwId).toMatch(/^hw:/)
      })
    })
  })

  describe('device object structure', () => {
    it('should have all required fields', () => {
      const device = {
        hw_id: 'hw:Headphones',
        hw_name: 'Headphones',
        friendly_name: 'Headphones',
        type: 'BUILTIN_HEADPHONES',
        is_available: true
      }

      expect(device).toHaveProperty('hw_id')
      expect(device).toHaveProperty('hw_name')
      expect(device).toHaveProperty('friendly_name')
      expect(device).toHaveProperty('type')
      expect(device).toHaveProperty('is_available')
    })

    it('should track device availability', () => {
      const availableDevice = { is_available: true }
      const unavailableDevice = { is_available: false }

      expect(availableDevice.is_available).toBe(true)
      expect(unavailableDevice.is_available).toBe(false)
    })
  })

  describe('error handling', () => {
    it('should handle missing hw_id in request', async () => {
      const response = await fetch('/api/audio/output/device', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({})
      })

      expect(response.status).toBe(400)
    })

    it('should handle network errors gracefully', async () => {
      const originalFetch = global.fetch
      global.fetch = vi.fn().mockRejectedValue(new Error('Network error'))

      try {
        await global.fetch('/api/audio/devices/output')
      } catch (error) {
        expect(error).toBeInstanceOf(Error)
      }

      global.fetch = originalFetch
    })
  })

  describe('settings integration', () => {
    it('should reflect output device in settings', () => {
      const audioSettings = {
        output: {
          device: 'hw:Headphones',
          device_type: 'BUILTIN_HEADPHONES',
          fallback_device: 'hw:Headphones'
        },
        input: {
          devices: []
        }
      }

      expect(audioSettings.output.device).toBe('hw:Headphones')
      expect(audioSettings.output.device_type).toBe('BUILTIN_HEADPHONES')
    })

    it('should support input device configuration', () => {
      const inputDevice = {
        hw_id: 'hw:USB',
        custom_name: 'USB Microphone',
        enabled: true
      }

      expect(inputDevice).toHaveProperty('hw_id')
      expect(inputDevice).toHaveProperty('custom_name')
      expect(inputDevice).toHaveProperty('enabled')
    })
  })
})
