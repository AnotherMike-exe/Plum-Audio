/**
 * Mock Fetch Handlers using MSW (Mock Service Worker)
 *
 * Provides HTTP request handlers for testing services that use fetch.
 * These handlers simulate the backend REST APIs.
 */

import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import {
  createMockSettings,
  createMockStreams,
  createMockClients,
  createMockServers
} from './mockTypes'
import type { Settings } from '../../types'

// =============================================================================
// Mock State
// =============================================================================

let mockSettings: Settings = createMockSettings()
let mockPlaybackData: Record<string, unknown> = {}
let mockFederationEnabled = false

/**
 * Reset mock state between tests
 */
export function resetMockState(): void {
  mockSettings = createMockSettings()
  mockPlaybackData = {}
  mockFederationEnabled = false
}

/**
 * Set mock settings for tests
 */
export function setMockSettings(settings: Partial<Settings>): void {
  mockSettings = { ...mockSettings, ...settings }
}

/**
 * Set mock playback data for a stream
 */
export function setMockPlayback(streamId: string, data: unknown): void {
  mockPlaybackData[streamId] = data
}

/**
 * Enable/disable federation for tests
 */
export function setFederationEnabled(enabled: boolean): void {
  mockFederationEnabled = enabled
}

// =============================================================================
// Request Handlers
// =============================================================================

export const handlers = [
  // -------------------------------------------------------------------------
  // Settings API
  // -------------------------------------------------------------------------

  http.get('/api/settings', () => {
    return HttpResponse.json(mockSettings)
  }),

  http.post('/api/settings', async ({ request }) => {
    const updates = await request.json() as Partial<Settings>
    mockSettings = {
      ...mockSettings,
      ...updates,
      version: ((mockSettings as unknown as { version?: number }).version ?? 0) + 1
    } as Settings
    return HttpResponse.json(mockSettings)
  }),

  http.post('/api/settings/device', async ({ request }) => {
    const { deviceName, hostname } = await request.json() as { deviceName?: string; hostname?: string }
    if (deviceName) mockSettings.deviceName = deviceName
    if (hostname) mockSettings.hostname = hostname
    return HttpResponse.json({
      success: true,
      message: 'Device settings updated',
      settings: mockSettings
    })
  }),

  http.post('/api/settings/device/hostname/validate', async ({ request }) => {
    const { hostname } = await request.json() as { hostname: string }
    const isValid = /^[a-z0-9-]+$/.test(hostname) && hostname.length <= 63
    return HttpResponse.json({
      valid: isValid,
      error: isValid ? null : 'Invalid hostname format'
    })
  }),

  http.post('/api/settings/device/hostname/sanitize', async ({ request }) => {
    const { deviceName } = await request.json() as { deviceName: string }
    const sanitized = deviceName.toLowerCase().replace(/[^a-z0-9-]/g, '-').slice(0, 63)
    return HttpResponse.json({ hostname: sanitized })
  }),

  // -------------------------------------------------------------------------
  // Playback API
  // -------------------------------------------------------------------------

  http.get('/api/playback', () => {
    return HttpResponse.json({
      success: true,
      streams: mockPlaybackData
    })
  }),

  http.get('/api/playback/:streamId', ({ params }) => {
    const streamId = params.streamId as string
    const data = mockPlaybackData[streamId]
    if (data) {
      return HttpResponse.json({ success: true, ...data as object })
    }
    return HttpResponse.json({ success: false, message: 'No playback data available' })
  }),

  // -------------------------------------------------------------------------
  // Integrations API
  // -------------------------------------------------------------------------

  http.get('/api/integrations/:integration/status', ({ params }) => {
    const integration = params.integration as string
    const integrationData = (mockSettings.integrations as Record<string, unknown>)[integration]
    return HttpResponse.json({
      success: true,
      status: integrationData ? 'enabled' : 'disabled',
      details: integrationData
    })
  }),

  http.post('/api/integrations/:integration/enable', ({ params }) => {
    const integration = params.integration as string
    return HttpResponse.json({
      success: true,
      message: `${integration} enabled`
    })
  }),

  http.post('/api/integrations/:integration/disable', ({ params }) => {
    const integration = params.integration as string
    return HttpResponse.json({
      success: true,
      message: `${integration} disabled`
    })
  }),

  http.get('/api/integrations/airplay/endpoints', () => {
    return HttpResponse.json({
      success: true,
      endpoints: mockSettings.integrations.airplay.endpoints
    })
  }),

  http.post('/api/integrations/airplay/endpoints', async ({ request }) => {
    const { deviceName } = await request.json() as { deviceName: string }
    const newEndpoint = {
      id: String(mockSettings.integrations.airplay.endpoints.length + 1),
      enabled: true,
      deviceName,
      port: 5050 + mockSettings.integrations.airplay.endpoints.length,
      udpPortBase: 6001 + mockSettings.integrations.airplay.endpoints.length * 10
    }
    mockSettings.integrations.airplay.endpoints.push(newEndpoint)
    return HttpResponse.json({ success: true, endpoint: newEndpoint })
  }),

  http.get('/api/integrations/spotify/endpoints', () => {
    return HttpResponse.json({
      success: true,
      endpoints: mockSettings.integrations.spotify.endpoints
    })
  }),

  http.get('/api/integrations/dlna/endpoints', () => {
    return HttpResponse.json({
      success: true,
      endpoints: mockSettings.integrations.dlna.endpoints
    })
  }),

  // -------------------------------------------------------------------------
  // Audio API
  // -------------------------------------------------------------------------

  http.get('/api/audio/devices/output', () => {
    return HttpResponse.json({
      success: true,
      devices: [
        {
          hw_id: 'hw:Headphones',
          hw_name: 'Headphones',
          friendly_name: 'Headphones',
          type: 'BUILTIN_HEADPHONES',
          is_available: true
        },
        {
          hw_id: 'hw:HDMI',
          hw_name: 'HDMI',
          friendly_name: 'HDMI Output',
          type: 'HDMI',
          is_available: true
        }
      ]
    })
  }),

  http.get('/api/audio/output/current', () => {
    return HttpResponse.json({
      success: true,
      device: {
        hw_id: 'hw:Headphones',
        device_type: 'BUILTIN_HEADPHONES'
      }
    })
  }),

  http.post('/api/audio/output/device', async ({ request }) => {
    const { hw_id } = await request.json() as { hw_id?: string }
    if (!hw_id) {
      return HttpResponse.json(
        { success: false, message: 'hw_id is required' },
        { status: 400 }
      )
    }
    return HttpResponse.json({
      success: true,
      message: `Output device set to ${hw_id}`
    })
  }),

  http.post('/api/audio/output/test', () => {
    return HttpResponse.json({ success: true })
  }),

  http.get('/api/audio/devices/input', () => {
    return HttpResponse.json({
      success: true,
      devices: []
    })
  }),

  // -------------------------------------------------------------------------
  // Federation API
  // -------------------------------------------------------------------------

  http.get('/api/health', () => {
    return HttpResponse.json({
      status: mockFederationEnabled ? 'healthy' : 'degraded',
      service: 'federation',
      loop_healthy: true
    })
  }),

  http.get('/api/federation/servers', () => {
    if (!mockFederationEnabled) {
      return HttpResponse.json({ success: false, error: 'Federation not enabled' }, { status: 503 })
    }
    return HttpResponse.json(createMockServers(2))
  }),

  http.get('/api/federation/streams', () => {
    if (!mockFederationEnabled) {
      return HttpResponse.json({ success: false, error: 'Federation not enabled' }, { status: 503 })
    }
    return HttpResponse.json(createMockStreams(3))
  }),

  http.get('/api/federation/clients', () => {
    if (!mockFederationEnabled) {
      return HttpResponse.json({ success: false, error: 'Federation not enabled' }, { status: 503 })
    }
    return HttpResponse.json(createMockClients(3))
  }),

  http.get('/api/federation/active-endpoint', () => {
    return HttpResponse.json({
      active: false,
      serverId: null,
      clientId: null,
      streamId: null
    })
  }),

  http.post('/api/federation/route', async ({ request }) => {
    const { clientId, streamId } = await request.json() as { clientId: string; streamId: string }
    return HttpResponse.json({
      success: true,
      message: `Routed ${clientId} to ${streamId}`
    })
  }),

  http.post('/api/federation/client/volume', async ({ request }) => {
    const { clientId, volume } = await request.json() as { clientId: string; volume: number }
    return HttpResponse.json({
      success: true,
      message: `Set ${clientId} volume to ${volume}`
    })
  }),

  http.post('/api/federation/stream/control', async ({ request }) => {
    const { streamId, command } = await request.json() as { streamId: string; command: string }
    return HttpResponse.json({
      success: true,
      message: `${command} sent to ${streamId}`
    })
  }),

  http.post('/api/federation/server/add', async ({ request }) => {
    const { host, port, name } = await request.json() as { host: string; port: number; name: string }
    return HttpResponse.json({
      success: true,
      server: {
        id: `server-${host.replace(/\./g, '-')}`,
        name,
        host,
        port,
        connected: true,
        isLocal: false
      }
    })
  }),

  http.post('/api/federation/server/remove', async ({ request }) => {
    await request.json()
    return HttpResponse.json({ success: true })
  })
]

// =============================================================================
// MSW Server Setup
// =============================================================================

/**
 * Create and configure the MSW server
 *
 * Usage in tests:
 *   import { server } from './mocks/mockFetch'
 *
 *   beforeAll(() => server.listen())
 *   afterEach(() => server.resetHandlers())
 *   afterAll(() => server.close())
 */
export const server = setupServer(...handlers)

/**
 * Add a custom handler for a specific test
 *
 * Usage:
 *   addHandler(http.get('/api/custom', () => HttpResponse.json({ custom: true })))
 */
export function addHandler(handler: ReturnType<typeof http.get>): void {
  server.use(handler)
}

/**
 * Make an endpoint return an error
 *
 * Usage:
 *   mockEndpointError('/api/settings', 500, 'Internal Server Error')
 */
export function mockEndpointError(path: string, status: number = 500, message: string = 'Error'): void {
  server.use(
    http.get(path, () => HttpResponse.json({ error: message }, { status })),
    http.post(path, () => HttpResponse.json({ error: message }, { status }))
  )
}
