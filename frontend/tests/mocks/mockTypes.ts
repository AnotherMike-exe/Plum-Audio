/**
 * Mock Type Factories for Testing
 *
 * Provides factory functions to create test data with sensible defaults.
 * All factories accept partial overrides for customization.
 */

import type {
  Track,
  Stream,
  Client,
  Server,
  PlaybackData,
  Settings,
  AirPlayEndpoint,
  SpotifyEndpoint,
  DLNAEndpoint,
  VisualizerSettings,
  DEFAULT_VISUALIZER_SETTINGS
} from '../../types'

// =============================================================================
// Core Type Factories
// =============================================================================

/**
 * Create a mock Track
 */
export function createMockTrack(overrides: Partial<Track> = {}): Track {
  return {
    id: 'track-1',
    title: 'Test Song',
    artist: 'Test Artist',
    album: 'Test Album',
    albumArtUrl: '',
    duration: 180,
    ...overrides
  }
}

/**
 * Create a mock Stream
 */
export function createMockStream(overrides: Partial<Stream> = {}): Stream {
  return {
    id: 'stream-1',
    serverId: 'server-192-168-1-100',
    serverName: 'Test Server',
    name: 'AirPlay',
    sourceDevice: 'AirPlay',
    currentTrack: createMockTrack(),
    isPlaying: false,
    progress: 0,
    playback: undefined,
    ...overrides
  }
}

/**
 * Create a mock Client
 */
export function createMockClient(overrides: Partial<Client> = {}): Client {
  return {
    id: 'client-1',
    serverId: 'server-192-168-1-100',
    serverName: 'Test Server',
    name: 'Living Room Speaker',
    currentStreamId: null,
    volume: 100,
    connected: true,
    ...overrides
  }
}

/**
 * Create a mock Server (federation)
 */
export function createMockServer(overrides: Partial<Server> = {}): Server {
  return {
    id: 'server-192-168-1-100',
    name: 'Test Server',
    host: '192.168.1.100',
    port: 1780,
    connected: true,
    isLocal: true,
    ...overrides
  }
}

/**
 * Create mock PlaybackData
 */
export function createMockPlaybackData(overrides: Partial<PlaybackData> = {}): PlaybackData {
  return {
    position: 0,
    duration: 180000,
    interpolated_position: 0,
    playback_status: 'stopped',
    is_stale: false,
    ...overrides
  }
}

// =============================================================================
// Endpoint Factories
// =============================================================================

/**
 * Create a mock AirPlay endpoint
 */
export function createMockAirPlayEndpoint(overrides: Partial<AirPlayEndpoint> = {}): AirPlayEndpoint {
  return {
    id: '1',
    enabled: true,
    deviceName: 'Test AirPlay',
    port: 5050,
    udpPortBase: 6001,
    ...overrides
  }
}

/**
 * Create a mock Spotify endpoint
 */
export function createMockSpotifyEndpoint(overrides: Partial<SpotifyEndpoint> = {}): SpotifyEndpoint {
  return {
    id: '1',
    enabled: false,
    deviceName: 'Test Spotify',
    zeroconfPort: 5354,
    ...overrides
  }
}

/**
 * Create a mock DLNA endpoint
 */
export function createMockDLNAEndpoint(overrides: Partial<DLNAEndpoint> = {}): DLNAEndpoint {
  return {
    id: '1',
    enabled: true,
    deviceName: 'Test DLNA',
    port: 49494,
    uuid: 'test-uuid-1234',
    ...overrides
  }
}

// =============================================================================
// Settings Factory
// =============================================================================

/**
 * Create mock Settings
 */
export function createMockSettings(overrides: Partial<Settings> = {}): Settings {
  const defaultSettings: Settings = {
    deviceName: 'Test Plum Snapcast',
    hostname: 'test-plum-snapcast',
    integrations: {
      airplay: {
        endpoints: [createMockAirPlayEndpoint()]
      },
      bluetooth: {
        enabled: false,
        deviceName: 'Test Bluetooth',
        adapter: 'hci0',
        autoPair: true,
        discoverable: true
      },
      spotify: {
        bitrate: 320,
        endpoints: [createMockSpotifyEndpoint()]
      },
      dlna: {
        endpoints: []
      },
      plexamp: {
        available: false,
        enabled: false,
        sourceName: 'Plexamp'
      },
      snapcast: true,
      visualizer: createMockVisualizerSettings()
    },
    theme: {
      mode: 'system',
      accent: 'purple',
      customColor: undefined,
      useAlbumArtColors: false
    },
    display: {
      showOfflineDevices: false
    },
    federation: {
      enabled: false,
      autoDiscover: true
    }
  }

  // Deep merge overrides
  return deepMerge(defaultSettings, overrides) as Settings
}

/**
 * Create mock VisualizerSettings
 */
export function createMockVisualizerSettings(overrides: Partial<VisualizerSettings> = {}): VisualizerSettings {
  return {
    enabled: true,
    theme: 'user',
    type: 'circular',
    barCount: 128,
    sensitivity: 50,
    smoothing: 70,
    smoothingType: 'catmull-rom',
    frequencyScale: 'logarithmic-smooth',
    idleState: 'circle',
    symmetry: 1,
    mirror: false,
    invert: false,
    taper: true,
    mixedFlip: false,
    rotate: false,
    rotationSpeed: 30,
    rotationDirection: 'clockwise',
    cycleEnabled: false,
    cyclePresetIds: [],
    advanced: {
      bassAnalysis: false,
      particles: false
    },
    ...overrides
  }
}

// =============================================================================
// List Factories
// =============================================================================

/**
 * Create multiple mock streams
 */
export function createMockStreams(count: number = 3): Stream[] {
  const sources = ['AirPlay', 'Spotify', 'Bluetooth', 'DLNA', 'Plexamp']
  return Array.from({ length: count }, (_, i) => createMockStream({
    id: `stream-${i + 1}`,
    name: sources[i % sources.length],
    sourceDevice: sources[i % sources.length],
    isPlaying: i === 0
  }))
}

/**
 * Create multiple mock clients
 */
export function createMockClients(count: number = 3): Client[] {
  const rooms = ['Living Room', 'Kitchen', 'Bedroom', 'Office', 'Bathroom']
  return Array.from({ length: count }, (_, i) => createMockClient({
    id: `client-${i + 1}`,
    name: `${rooms[i % rooms.length]} Speaker`,
    connected: i < count - 1 // Last one disconnected
  }))
}

/**
 * Create multiple mock servers
 */
export function createMockServers(count: number = 2): Server[] {
  const names = ['Main Server', 'Kitchen Server', 'Garage Server']
  return Array.from({ length: count }, (_, i) => createMockServer({
    id: `server-192-168-1-${100 + i}`,
    name: names[i % names.length],
    host: `192.168.1.${100 + i}`,
    isLocal: i === 0,
    connected: true
  }))
}

// =============================================================================
// Snapcast API Response Factories
// =============================================================================

/**
 * Create a mock Snapcast server status response
 */
export function createMockSnapcastStatus(options: {
  streams?: Partial<Stream>[]
  clients?: Partial<Client>[]
} = {}) {
  const streams = (options.streams || [{ id: 'airplay1' }]).map((s, i) => ({
    id: s.id || `stream-${i}`,
    status: s.isPlaying ? 'playing' : 'idle',
    properties: {
      metadata: s.currentTrack ? {
        artist: s.currentTrack.artist,
        title: s.currentTrack.title,
        album: s.currentTrack.album,
        duration: (s.currentTrack.duration || 0) * 1000
      } : {}
    },
    uri: {
      raw: `pipe:///tmp/${s.id || `stream-${i}`}-fifo`,
      scheme: 'pipe',
      host: '',
      path: `/tmp/${s.id || `stream-${i}`}-fifo`,
      query: { name: s.id || `stream-${i}` }
    }
  }))

  const clients = (options.clients || [{ id: 'client1', name: 'Test' }]).map((c, i) => ({
    id: c.id || `client-${i}`,
    config: {
      name: c.name || `Client ${i}`,
      volume: { percent: c.volume ?? 100, muted: false }
    },
    connected: c.connected ?? true,
    host: { name: c.name || `Client ${i}`, mac: `aa:bb:cc:dd:ee:${i.toString(16).padStart(2, '0')}` }
  }))

  const groups = clients.map((c, i) => ({
    id: `group-${i}`,
    name: '',
    stream_id: options.clients?.[i]?.currentStreamId || 'none',
    muted: false,
    clients: [c]
  }))

  return {
    server: {
      groups,
      streams
    }
  }
}

// =============================================================================
// Utility Functions
// =============================================================================

/**
 * Deep merge two objects
 */
function deepMerge<T extends Record<string, unknown>>(target: T, source: Partial<T>): T {
  const result = { ...target }

  for (const key in source) {
    const sourceValue = source[key]
    const targetValue = result[key]

    if (
      sourceValue !== null &&
      typeof sourceValue === 'object' &&
      !Array.isArray(sourceValue) &&
      targetValue !== null &&
      typeof targetValue === 'object' &&
      !Array.isArray(targetValue)
    ) {
      result[key] = deepMerge(
        targetValue as Record<string, unknown>,
        sourceValue as Record<string, unknown>
      ) as T[Extract<keyof T, string>]
    } else if (sourceValue !== undefined) {
      result[key] = sourceValue as T[Extract<keyof T, string>]
    }
  }

  return result
}
