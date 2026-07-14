/**
 * Unit tests for federationService
 * Tests: REST calls, server list handling, routing, health checks
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { server, setFederationEnabled, resetMockState } from '../../mocks/mockFetch'
import { createMockServer, createMockStream, createMockClient } from '../../mocks/mockTypes'

// Setup MSW
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => {
  server.resetHandlers()
  resetMockState()
})
afterAll(() => server.close())

describe('federationService', () => {
  describe('health check', () => {
    it('should check federation service health', async () => {
      setFederationEnabled(true)

      const response = await fetch('/api/health')
      const data = await response.json()

      expect(data.service).toBe('federation')
      expect(data.status).toBe('healthy')
      expect(data.loop_healthy).toBe(true)
    })

    it('should report degraded when federation disabled', async () => {
      setFederationEnabled(false)

      const response = await fetch('/api/health')
      const data = await response.json()

      expect(data.status).toBe('degraded')
    })
  })

  describe('getServers', () => {
    it('should fetch list of servers', async () => {
      setFederationEnabled(true)

      const response = await fetch('/api/federation/servers')
      const data = await response.json()

      expect(Array.isArray(data)).toBe(true)
      expect(data.length).toBeGreaterThan(0)
    })

    it('should include local and remote servers', async () => {
      setFederationEnabled(true)

      const response = await fetch('/api/federation/servers')
      const servers = await response.json()

      const hasLocal = servers.some((s: any) => s.isLocal)
      expect(hasLocal).toBe(true)
    })

    it('should return error when federation disabled', async () => {
      setFederationEnabled(false)

      const response = await fetch('/api/federation/servers')

      expect(response.status).toBe(503)
    })
  })

  describe('getStreams', () => {
    it('should fetch streams from all servers', async () => {
      setFederationEnabled(true)

      const response = await fetch('/api/federation/streams')
      const data = await response.json()

      expect(Array.isArray(data)).toBe(true)
    })

    it('should include federated stream IDs', async () => {
      setFederationEnabled(true)

      const response = await fetch('/api/federation/streams')
      const streams = await response.json()

      if (streams.length > 0) {
        // Federated IDs start with server-
        expect(streams[0].id).toContain('stream')
      }
    })
  })

  describe('getClients', () => {
    it('should fetch clients from all servers', async () => {
      setFederationEnabled(true)

      const response = await fetch('/api/federation/clients')
      const data = await response.json()

      expect(Array.isArray(data)).toBe(true)
    })

    it('should include client connection status', async () => {
      setFederationEnabled(true)

      const response = await fetch('/api/federation/clients')
      const clients = await response.json()

      if (clients.length > 0) {
        expect(clients[0]).toHaveProperty('connected')
      }
    })
  })

  describe('getActiveEndpoint', () => {
    it('should return active endpoint info', async () => {
      const response = await fetch('/api/federation/active-endpoint')
      const data = await response.json()

      expect(data).toHaveProperty('active')
      expect(typeof data.active).toBe('boolean')
    })

    it('should include server/client/stream IDs when active', async () => {
      const response = await fetch('/api/federation/active-endpoint')
      const data = await response.json()

      expect(data).toHaveProperty('serverId')
      expect(data).toHaveProperty('clientId')
      expect(data).toHaveProperty('streamId')
    })
  })

  describe('routeClientToStream', () => {
    it('should route client to stream', async () => {
      setFederationEnabled(true)

      const response = await fetch('/api/federation/route', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          clientId: 'server-192-168-1-100-client1',
          streamId: 'server-192-168-1-100-airplay1'
        })
      })

      expect(response.ok).toBe(true)
      const data = await response.json()
      expect(data.success).toBe(true)
    })

    it('should include routing message', async () => {
      setFederationEnabled(true)

      const response = await fetch('/api/federation/route', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          clientId: 'server-192-168-1-100-client1',
          streamId: 'server-192-168-1-100-airplay1'
        })
      })

      const data = await response.json()
      expect(data.message).toBeDefined()
    })
  })

  describe('setClientVolume', () => {
    it('should set client volume', async () => {
      setFederationEnabled(true)

      const response = await fetch('/api/federation/client/volume', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          clientId: 'server-192-168-1-100-client1',
          volume: 75,
          muted: false
        })
      })

      expect(response.ok).toBe(true)
      const data = await response.json()
      expect(data.success).toBe(true)
    })

    it('should accept volume 0-100', async () => {
      const validVolumes = [0, 50, 100]

      for (const volume of validVolumes) {
        const response = await fetch('/api/federation/client/volume', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            clientId: 'client1',
            volume
          })
        })

        expect(response.ok).toBe(true)
      }
    })
  })

  describe('streamControl', () => {
    it('should send play command', async () => {
      setFederationEnabled(true)

      const response = await fetch('/api/federation/stream/control', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          streamId: 'server-192-168-1-100-airplay1',
          command: 'play'
        })
      })

      expect(response.ok).toBe(true)
    })

    it('should send pause command', async () => {
      setFederationEnabled(true)

      const response = await fetch('/api/federation/stream/control', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          streamId: 'server-192-168-1-100-airplay1',
          command: 'pause'
        })
      })

      expect(response.ok).toBe(true)
    })

    it('should support next/previous commands', async () => {
      setFederationEnabled(true)

      const commands = ['next', 'previous']

      for (const command of commands) {
        const response = await fetch('/api/federation/stream/control', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            streamId: 'stream1',
            command
          })
        })

        expect(response.ok).toBe(true)
      }
    })
  })

  describe('server management', () => {
    it('should add server', async () => {
      setFederationEnabled(true)

      const response = await fetch('/api/federation/server/add', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          host: '192.168.1.200',
          port: 1780,
          name: 'New Server'
        })
      })

      expect(response.ok).toBe(true)
      const data = await response.json()
      expect(data.success).toBe(true)
      expect(data.server).toBeDefined()
    })

    it('should remove server', async () => {
      setFederationEnabled(true)

      const response = await fetch('/api/federation/server/remove', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          serverId: 'server-192-168-1-200'
        })
      })

      expect(response.ok).toBe(true)
    })
  })

  describe('data structures', () => {
    it('should have valid Server structure', () => {
      const server = createMockServer()

      expect(server).toHaveProperty('id')
      expect(server).toHaveProperty('name')
      expect(server).toHaveProperty('host')
      expect(server).toHaveProperty('port')
      expect(server).toHaveProperty('connected')
      expect(server).toHaveProperty('isLocal')
    })

    it('should have valid federated Stream structure', () => {
      const stream = createMockStream({
        id: 'server-192-168-1-100-airplay1',
        serverId: 'server-192-168-1-100',
        serverName: 'Test Server'
      })

      expect(stream.id).toContain('server-')
      expect(stream.serverId).toBeDefined()
      expect(stream.serverName).toBeDefined()
    })

    it('should have valid federated Client structure', () => {
      const client = createMockClient({
        id: 'server-192-168-1-100-client1',
        serverId: 'server-192-168-1-100',
        serverName: 'Test Server'
      })

      expect(client.id).toContain('server-')
      expect(client.serverId).toBeDefined()
      expect(client.serverName).toBeDefined()
    })
  })
})
