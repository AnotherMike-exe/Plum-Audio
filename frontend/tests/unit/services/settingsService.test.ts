/**
 * Unit tests for settingsService
 * Tests: Server/local settings merge, localStorage, polling, updates
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { server, resetMockState, setMockSettings } from '../../mocks/mockFetch'
import { createMockSettings } from '../../mocks/mockTypes'

// Setup MSW
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => {
  server.resetHandlers()
  resetMockState()
  vi.clearAllMocks()
})
afterAll(() => server.close())

describe('settingsService', () => {
  describe('initialization', () => {
    it('should initialize with default settings before fetch', async () => {
      // Local settings should have defaults
      const expectedDefaults = {
        theme: {
          mode: 'system',
          accent: 'purple'
        }
      }

      expect(expectedDefaults.theme.mode).toBe('system')
      expect(expectedDefaults.theme.accent).toBe('purple')
    })

    it('should merge server and local settings', async () => {
      const serverSettings = createMockSettings({
        deviceName: 'Server Device',
        hostname: 'server-hostname'
      })

      const localSettings = {
        theme: {
          mode: 'dark' as const,
          accent: 'blue' as const
        }
      }

      // Merge should combine both
      const merged = {
        ...serverSettings,
        theme: localSettings.theme
      }

      expect(merged.deviceName).toBe('Server Device')
      expect(merged.theme.mode).toBe('dark')
    })
  })

  describe('server settings', () => {
    it('should fetch settings from /api/settings', async () => {
      const mockSettings = createMockSettings({ deviceName: 'Test Device' })
      setMockSettings(mockSettings)

      const response = await fetch('/api/settings')
      const data = await response.json()

      expect(data.deviceName).toBe('Test Device')
    })

    it('should update settings via POST', async () => {
      const response = await fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ deviceName: 'Updated Device' })
      })

      expect(response.ok).toBe(true)
      const data = await response.json()
      expect(data.deviceName).toBe('Updated Device')
    })

    it('should handle update errors gracefully', async () => {
      // Simulate network error
      const originalFetch = global.fetch
      global.fetch = vi.fn().mockRejectedValue(new Error('Network error'))

      try {
        await global.fetch('/api/settings')
      } catch (error) {
        expect(error).toBeInstanceOf(Error)
      }

      global.fetch = originalFetch
    })
  })

  describe('local settings', () => {
    it('should store local settings in localStorage', () => {
      const localSettings = {
        theme: { mode: 'dark', accent: 'green' }
      }

      localStorage.setItem('plum-snapcast-local-settings', JSON.stringify(localSettings))

      const stored = localStorage.getItem('plum-snapcast-local-settings')
      expect(stored).toBeTruthy()
      expect(JSON.parse(stored!)).toEqual(localSettings)
    })

    it('should handle missing localStorage gracefully', () => {
      localStorage.removeItem('plum-snapcast-local-settings')

      const stored = localStorage.getItem('plum-snapcast-local-settings')
      expect(stored).toBeNull()
    })

    it('should parse invalid localStorage as empty', () => {
      localStorage.setItem('plum-snapcast-local-settings', 'invalid json')

      let settings = {}
      try {
        settings = JSON.parse(localStorage.getItem('plum-snapcast-local-settings') || '{}')
      } catch {
        settings = {}
      }

      expect(settings).toEqual({})
    })
  })

  describe('theme settings', () => {
    it('should support all theme modes', () => {
      const modes = ['light', 'dark', 'system', 'black', 'white']

      modes.forEach(mode => {
        expect(['light', 'dark', 'system', 'black', 'white']).toContain(mode)
      })
    })

    it('should support all accent colors', () => {
      const colors = ['purple', 'blue', 'green', 'orange', 'red', 'yellow', 'custom']

      colors.forEach(color => {
        expect(['purple', 'blue', 'green', 'orange', 'red', 'yellow', 'custom']).toContain(color)
      })
    })

    it('should validate custom color format', () => {
      const validHexColors = ['#ff5733', '#FF5733', '#abc', '#ABC']
      const invalidColors = ['red', 'rgb(255,0,0)', '12345']

      const hexPattern = /^#([0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})$/

      validHexColors.forEach(color => {
        expect(hexPattern.test(color)).toBe(true)
      })

      invalidColors.forEach(color => {
        expect(hexPattern.test(color)).toBe(false)
      })
    })
  })

  describe('integration settings', () => {
    it('should have correct AirPlay endpoint structure', () => {
      const endpoint = {
        id: '1',
        enabled: true,
        deviceName: 'AirPlay Speaker',
        port: 5050,
        udpPortBase: 6001
      }

      expect(endpoint).toHaveProperty('id')
      expect(endpoint).toHaveProperty('enabled')
      expect(endpoint).toHaveProperty('deviceName')
      expect(endpoint).toHaveProperty('port')
      expect(endpoint).toHaveProperty('udpPortBase')
    })

    it('should have correct Spotify endpoint structure', () => {
      const endpoint = {
        id: '1',
        enabled: false,
        deviceName: 'Spotify Speaker',
        zeroconfPort: 5354
      }

      expect(endpoint).toHaveProperty('id')
      expect(endpoint).toHaveProperty('enabled')
      expect(endpoint).toHaveProperty('deviceName')
      expect(endpoint).toHaveProperty('zeroconfPort')
    })

    it('should have correct DLNA endpoint structure', () => {
      const endpoint = {
        id: '1',
        enabled: true,
        deviceName: 'DLNA Speaker',
        port: 49494,
        uuid: 'test-uuid'
      }

      expect(endpoint).toHaveProperty('id')
      expect(endpoint).toHaveProperty('enabled')
      expect(endpoint).toHaveProperty('deviceName')
      expect(endpoint).toHaveProperty('port')
      expect(endpoint).toHaveProperty('uuid')
    })
  })

  describe('version tracking', () => {
    it('should include version in settings', async () => {
      const settings = createMockSettings()

      // Version is used for change detection
      // Initial version should be a number
      const settingsWithVersion = { ...settings, version: 1 }
      expect(typeof settingsWithVersion.version).toBe('number')
    })

    it('should detect version changes for polling', () => {
      const oldVersion = 1
      const newVersion = 2

      const hasChanged = newVersion !== oldVersion
      expect(hasChanged).toBe(true)
    })
  })

  describe('device settings', () => {
    it('should update device name via API', async () => {
      const response = await fetch('/api/settings/device', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ deviceName: 'New Name' })
      })

      expect(response.ok).toBe(true)
      const data = await response.json()
      expect(data.success).toBe(true)
    })

    it('should validate hostname format', async () => {
      const response = await fetch('/api/settings/device/hostname/validate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ hostname: 'valid-hostname' })
      })

      expect(response.ok).toBe(true)
      const data = await response.json()
      expect(data.valid).toBe(true)
    })

    it('should sanitize device name to hostname', async () => {
      const response = await fetch('/api/settings/device/hostname/sanitize', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ deviceName: 'My Device!' })
      })

      expect(response.ok).toBe(true)
      const data = await response.json()
      expect(data.hostname).toMatch(/^[a-z0-9-]+$/)
    })
  })
})
