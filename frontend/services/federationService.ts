/**
 * Federation Service
 * REST API client for multi-server Snapcast control
 */

import { Server, Stream, Client } from '../types';

// Use relative path - nginx will proxy /api/federation/ to snapcast-host:5000/api/federation/
const FEDERATION_API_BASE = import.meta.env.VITE_FEDERATION_API_URL || '/api/federation';

export class FederationService {
  private baseUrl: string;
  private polling: boolean = false;
  private pollInterval: number = 5000; // 5 seconds
  private pollTimer: NodeJS.Timeout | null = null;
  private onUpdateCallback: ((data: { servers: Server[]; streams: Stream[]; clients: Client[] }) => void) | null = null;

  constructor(baseUrl: string = FEDERATION_API_BASE) {
    this.baseUrl = baseUrl;
  }

  /**
   * Health check - verify federation service is running in full mode
   * Note: This is called during polling, so failures are expected and silent
   */
  async checkHealth(): Promise<boolean> {
    try {
      const response = await fetch(`${this.baseUrl.replace('/federation', '')}/health`);
      if (!response.ok) {
        return false;
      }
      const data = await response.json();
      // Only return true if in full federation mode (not minimal mode)
      return data.status === 'healthy' && data.service === 'federation';
    } catch (error) {
      // Silent failure - this is expected during backend restarts
      return false;
    }
  }

  /**
   * Get all discovered servers
   */
  async getServers(): Promise<Server[]> {
    try {
      const response = await fetch(`${this.baseUrl}/servers`);
      if (!response.ok) {
        throw new Error(`Failed to fetch servers: ${response.statusText}`);
      }
      const data = await response.json();
      return data.servers || [];
    } catch (error) {
      console.error('Failed to fetch servers:', error);
      return [];
    }
  }

  /**
   * Get all streams from all servers
   */
  async getStreams(): Promise<Stream[]> {
    try {
      const response = await fetch(`${this.baseUrl}/streams`);
      if (!response.ok) {
        throw new Error(`Failed to fetch streams: ${response.statusText}`);
      }
      const data = await response.json();
      return data.streams || [];
    } catch (error) {
      console.error('Failed to fetch streams:', error);
      return [];
    }
  }

  /**
   * Get all clients from all servers
   */
  async getClients(): Promise<Client[]> {
    try {
      const response = await fetch(`${this.baseUrl}/clients`);
      if (!response.ok) {
        throw new Error(`Failed to fetch clients: ${response.statusText}`);
      }
      const data = await response.json();
      return data.clients || [];
    } catch (error) {
      console.error('Failed to fetch clients:', error);
      return [];
    }
  }

  /**
   * Get currently active endpoint (server/client/stream)
   */
  async getActiveEndpoint(): Promise<{ active: boolean; serverId?: string; clientId?: string; streamId?: string }> {
    try {
      const response = await fetch(`${this.baseUrl}/active-endpoint`);
      if (!response.ok) {
        throw new Error(`Failed to fetch active endpoint: ${response.statusText}`);
      }
      const data = await response.json();
      return data;
    } catch (error) {
      console.error('Failed to fetch active endpoint:', error);
      return { active: false };
    }
  }

  /**
   * Get all data (servers, streams, clients) in one call (DEPRECATED - use getSnapshot instead)
   * @deprecated Use getSnapshot() for atomic, race-condition-free data fetching
   */
  async getAll(): Promise<{ servers: Server[]; streams: Stream[]; clients: Client[] }> {
    const [servers, streams, clients] = await Promise.all([
      this.getServers(),
      this.getStreams(),
      this.getClients(),
    ]);

    return { servers, streams, clients };
  }

  /**
   * Get atomic snapshot of all data (servers, streams, clients).
   *
   * This method fetches all data from a single endpoint that provides a consistent
   * view of the system state, preventing race conditions where:
   * - Streams reference servers that don't exist yet
   * - Clients temporarily disappear during state updates
   *
   * Use this instead of getAll() for polling and state updates.
   */
  async getSnapshot(): Promise<{ servers: Server[]; streams: Stream[]; clients: Client[] }> {
    try {
      const response = await fetch(`${this.baseUrl}/snapshot`);
      if (!response.ok) {
        throw new Error(`Failed to fetch snapshot: ${response.statusText}`);
      }
      const data = await response.json();
      return {
        servers: data.servers || [],
        streams: data.streams || [],
        clients: data.clients || [],
      };
    } catch (error) {
      console.error('Failed to fetch snapshot:', error);
      return { servers: [], streams: [], clients: [] };
    }
  }

  /**
   * Route a client to a stream (handles cross-server routing)
   */
  async routeClient(clientId: string, streamId: string): Promise<{ success: boolean; message: string }> {
    try {
      const response = await fetch(`${this.baseUrl}/route`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ clientId, streamId }),
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.message || data.error || 'Failed to route client');
      }

      return data;
    } catch (error) {
      console.error('Failed to route client:', error);
      return {
        success: false,
        message: error instanceof Error ? error.message : 'Unknown error',
      };
    }
  }

  /**
   * Set client volume
   */
  async setVolume(clientId: string, volume: number, muted: boolean = false): Promise<{ success: boolean; message: string }> {
    try {
      const response = await fetch(`${this.baseUrl}/client/volume`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ clientId, volume, muted }),
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.message || data.error || 'Failed to set volume');
      }

      return data;
    } catch (error) {
      console.error('Failed to set volume:', error);
      return {
        success: false,
        message: error instanceof Error ? error.message : 'Unknown error',
      };
    }
  }

  /**
   * Control stream playback (play, pause, next, previous)
   */
  async controlStream(streamId: string, command: 'play' | 'pause' | 'next' | 'previous'): Promise<{ success: boolean; message: string }> {
    try {
      const response = await fetch(`${this.baseUrl}/stream/control`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ streamId, command }),
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.message || data.error || 'Failed to control stream');
      }

      return data;
    } catch (error) {
      console.error('Failed to control stream:', error);
      return {
        success: false,
        message: error instanceof Error ? error.message : 'Unknown error',
      };
    }
  }

  /**
   * Set source volume for a stream (controls AirPlay/Spotify/etc volume)
   */
  async setStreamVolume(streamId: string, volume: number): Promise<{ success: boolean; message: string }> {
    try {
      const response = await fetch(`${this.baseUrl}/stream/volume`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ streamId, volume }),
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.message || data.error || 'Failed to set stream volume');
      }

      return data;
    } catch (error) {
      console.error('Failed to set stream volume:', error);
      return {
        success: false,
        message: error instanceof Error ? error.message : 'Unknown error',
      };
    }
  }

  /**
   * Manually add a server
   */
  async addServer(host: string, port: number, name: string): Promise<{ success: boolean; server?: Server; error?: string }> {
    try {
      const response = await fetch(`${this.baseUrl}/server/add`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ host, port, name }),
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || 'Failed to add server');
      }

      return { success: true, server: data.server };
    } catch (error) {
      console.error('Failed to add server:', error);
      return {
        success: false,
        error: error instanceof Error ? error.message : 'Unknown error',
      };
    }
  }

  /**
   * Edit a server
   */
  async editServer(serverId: string, host: string, port: number, name: string): Promise<{ success: boolean; server?: Server; error?: string }> {
    try {
      const response = await fetch(`${this.baseUrl}/server/edit`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ serverId, host, port, name }),
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || 'Failed to edit server');
      }

      return { success: true, server: data.server };
    } catch (error) {
      console.error('Failed to edit server:', error);
      return {
        success: false,
        error: error instanceof Error ? error.message : 'Unknown error',
      };
    }
  }

  /**
   * Remove a server
   */
  async removeServer(serverId: string): Promise<{ success: boolean; error?: string }> {
    try {
      const response = await fetch(`${this.baseUrl}/server/remove`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ serverId }),
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || 'Failed to remove server');
      }

      return { success: true };
    } catch (error) {
      console.error('Failed to remove server:', error);
      return {
        success: false,
        error: error instanceof Error ? error.message : 'Unknown error',
      };
    }
  }

  /**
   * Start polling for updates
   */
  startPolling(callback: (data: { servers: Server[]; streams: Stream[]; clients: Client[] }) => void, interval: number = 5000) {
    if (this.polling) {
      console.warn('Polling already started');
      return;
    }

    this.polling = true;
    this.pollInterval = interval;
    this.onUpdateCallback = callback;

    // Initial fetch
    this.poll();

    // Start polling
    this.pollTimer = setInterval(() => {
      this.poll();
    }, this.pollInterval);

    console.log('Federation polling started');
  }

  /**
   * Stop polling for updates
   */
  stopPolling() {
    if (!this.polling) {
      return;
    }

    this.polling = false;
    if (this.pollTimer) {
      clearInterval(this.pollTimer);
      this.pollTimer = null;
    }

    console.log('Federation polling stopped');
  }

  /**
   * Manually trigger an immediate poll (useful after state-changing operations)
   */
  async triggerPoll() {
    await this.poll();
  }

  /**
   * Internal: Perform one poll
   */
  private async poll() {
    try {
      const data = await this.getSnapshot();
      if (this.onUpdateCallback) {
        this.onUpdateCallback(data);
      }
    } catch (error) {
      console.error('Polling failed:', error);
    }
  }
}

// Singleton instance
export const federationService = new FederationService();
