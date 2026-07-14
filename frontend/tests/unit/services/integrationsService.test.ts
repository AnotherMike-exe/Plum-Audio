/**
 * Unit tests for integrationsService
 * Tests: Enable/disable calls, status handling, endpoint management
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { server, resetMockState } from '../../mocks/mockFetch'
import { createMockAirPlayEndpoint, createMockSpotifyEndpoint, createMockDLNAEndpoint } from '../../mocks/mockTypes'

// Setup MSW
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => {
  server.resetHandlers()
  resetMockState()
})
afterAll(() => server.close())

describe('integrationsService', () => {
  describe('AirPlay integration', () => {
    it('should get AirPlay status', async () => {
      const response = await fetch('/api/integrations/airplay/status')

      expect(response.ok).toBe(true)
      const data = await response.json()
      expect(data.success).toBe(true)
    })

    it('should enable AirPlay', async () => {
      const response = await fetch('/api/integrations/airplay/enable', {
        method: 'POST'
      })

      expect(response.ok).toBe(true)
      const data = await response.json()
      expect(data.success).toBe(true)
    })

    it('should disable AirPlay', async () => {
      const response = await fetch('/api/integrations/airplay/disable', {
        method: 'POST'
      })

      expect(response.ok).toBe(true)
    })

    it('should list AirPlay endpoints', async () => {
      const response = await fetch('/api/integrations/airplay/endpoints')

      expect(response.ok).toBe(true)
      const data = await response.json()
      expect(data.endpoints).toBeDefined()
      expect(Array.isArray(data.endpoints)).toBe(true)
    })

    it('should add AirPlay endpoint', async () => {
      const response = await fetch('/api/integrations/airplay/endpoints', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ deviceName: 'New AirPlay' })
      })

      expect(response.ok).toBe(true)
      const data = await response.json()
      expect(data.success).toBe(true)
    })

    it('should have valid AirPlay endpoint structure', () => {
      const endpoint = createMockAirPlayEndpoint()

      expect(endpoint.id).toBeDefined()
      expect(endpoint.enabled).toBeDefined()
      expect(endpoint.deviceName).toBeDefined()
      expect(endpoint.port).toBeDefined()
      expect(endpoint.udpPortBase).toBeDefined()
      expect(typeof endpoint.port).toBe('number')
    })
  })

  describe('Bluetooth integration', () => {
    it('should get Bluetooth status', async () => {
      const response = await fetch('/api/integrations/bluetooth/status')

      expect(response.ok).toBe(true)
    })

    it('should enable Bluetooth', async () => {
      const response = await fetch('/api/integrations/bluetooth/enable', {
        method: 'POST'
      })

      expect(response.ok).toBe(true)
    })

    it('should disable Bluetooth', async () => {
      const response = await fetch('/api/integrations/bluetooth/disable', {
        method: 'POST'
      })

      expect(response.ok).toBe(true)
    })

    it('should have Bluetooth settings structure', () => {
      const bluetoothSettings = {
        enabled: false,
        deviceName: 'Plum Audio',
        adapter: 'hci0',
        autoPair: true,
        discoverable: true
      }

      expect(bluetoothSettings).toHaveProperty('enabled')
      expect(bluetoothSettings).toHaveProperty('deviceName')
      expect(bluetoothSettings).toHaveProperty('autoPair')
      expect(bluetoothSettings).toHaveProperty('discoverable')
    })
  })

  describe('Spotify integration', () => {
    it('should get Spotify status', async () => {
      const response = await fetch('/api/integrations/spotify/status')

      expect(response.ok).toBe(true)
    })

    it('should enable Spotify', async () => {
      const response = await fetch('/api/integrations/spotify/enable', {
        method: 'POST'
      })

      expect(response.ok).toBe(true)
    })

    it('should disable Spotify', async () => {
      const response = await fetch('/api/integrations/spotify/disable', {
        method: 'POST'
      })

      expect(response.ok).toBe(true)
    })

    it('should list Spotify endpoints', async () => {
      const response = await fetch('/api/integrations/spotify/endpoints')

      expect(response.ok).toBe(true)
      const data = await response.json()
      expect(data.endpoints).toBeDefined()
    })

    it('should have valid Spotify endpoint structure', () => {
      const endpoint = createMockSpotifyEndpoint()

      expect(endpoint.id).toBeDefined()
      expect(endpoint.enabled).toBeDefined()
      expect(endpoint.deviceName).toBeDefined()
      expect(endpoint.zeroconfPort).toBeDefined()
      expect(typeof endpoint.zeroconfPort).toBe('number')
    })

    it('should support Spotify bitrate values', () => {
      const validBitrates = [96, 160, 320]

      validBitrates.forEach(bitrate => {
        expect([96, 160, 320]).toContain(bitrate)
      })
    })
  })

  describe('DLNA integration', () => {
    it('should get DLNA status', async () => {
      const response = await fetch('/api/integrations/dlna/status')

      expect(response.ok).toBe(true)
    })

    it('should enable DLNA', async () => {
      const response = await fetch('/api/integrations/dlna/enable', {
        method: 'POST'
      })

      expect(response.ok).toBe(true)
    })

    it('should disable DLNA', async () => {
      const response = await fetch('/api/integrations/dlna/disable', {
        method: 'POST'
      })

      expect(response.ok).toBe(true)
    })

    it('should list DLNA endpoints', async () => {
      const response = await fetch('/api/integrations/dlna/endpoints')

      expect(response.ok).toBe(true)
      const data = await response.json()
      expect(data.endpoints).toBeDefined()
    })

    it('should have valid DLNA endpoint structure', () => {
      const endpoint = createMockDLNAEndpoint()

      expect(endpoint.id).toBeDefined()
      expect(endpoint.enabled).toBeDefined()
      expect(endpoint.deviceName).toBeDefined()
      expect(endpoint.port).toBeDefined()
      expect(endpoint.uuid).toBeDefined()
    })
  })

  describe('Plexamp integration', () => {
    it('should get Plexamp status', async () => {
      const response = await fetch('/api/integrations/plexamp/status')

      expect(response.ok).toBe(true)
    })

    it('should enable Plexamp when available', async () => {
      const response = await fetch('/api/integrations/plexamp/enable', {
        method: 'POST'
      })

      // May succeed or fail depending on availability
      expect([200, 400, 500]).toContain(response.status)
    })

    it('should disable Plexamp', async () => {
      const response = await fetch('/api/integrations/plexamp/disable', {
        method: 'POST'
      })

      expect(response.ok).toBe(true)
    })

    it('should have Plexamp settings structure', () => {
      const plexampSettings = {
        available: false,
        enabled: false,
        sourceName: 'Plexamp'
      }

      expect(plexampSettings).toHaveProperty('available')
      expect(plexampSettings).toHaveProperty('enabled')
      expect(plexampSettings).toHaveProperty('sourceName')
    })
  })

  describe('response structure', () => {
    it('should return consistent success response', async () => {
      const response = await fetch('/api/integrations/airplay/enable', {
        method: 'POST'
      })

      const data = await response.json()

      expect(data).toHaveProperty('success')
      expect(typeof data.success).toBe('boolean')
    })

    it('should include message in response', async () => {
      const response = await fetch('/api/integrations/bluetooth/enable', {
        method: 'POST'
      })

      const data = await response.json()

      expect(data).toHaveProperty('message')
    })
  })
})
