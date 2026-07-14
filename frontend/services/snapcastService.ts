interface SnapcastServerInfo {
    name: string;
    version: string;
    host: string;
}

interface SnapcastStreamProperties {
    canPlay?: boolean;
    canPause?: boolean;
    canSeek?: boolean;
    canGoNext?: boolean;
    canGoPrevious?: boolean;
    loopStatus?: string;
    playbackStatus?: string;
    position?: number;
    metadata?: {
        [key: string]: any;
    };
}

interface SnapcastStream {
    id: string;
    status: string;
    uri: {
        raw: string;
        scheme: string;
        host: string;
        path: string;
        fragment: string;
        query: Record<string, string>;
    };
    meta?: {
        title?: string;
        artist?: string;
        album?: string;
        artData?: string;
        duration?: number;
    };
    properties?: SnapcastStreamProperties;
}

interface SnapcastClient {
    id: string;
    host: {
        name: string;
        ip: string;
    };
    config: {
        name: string;
        volume: {
            percent: number;
            muted: boolean;
        };
    };
    connected: boolean;
}

interface SnapcastGroup {
    id: string;
    name: string;
    stream_id: string;
    clients: SnapcastClient[];
    muted: boolean;
}

interface SnapcastError {
    code: number;
    message: string;
    data?: any;
}

interface SnapcastResponse {
    id?: number;
    jsonrpc: string;
    result?: {
        server?: {
            server: SnapcastServerInfo;
            streams: SnapcastStream[];
            groups: SnapcastGroup[];
        };
    };
    error?: SnapcastError;
    method?: string;
    params?: any;
}

export class SnapcastService {
    private ws: WebSocket | null = null;
    private messageId = 1;
    private callbacks: Map<number, (response: any) => void> = new Map();
    private host: string;
    private port: number;
    private isConnected = false;
    private metadataUpdateListeners: Set<(streamId: string, metadata: any) => void> = new Set();
    private playbackStateListeners: Set<(streamId: string, playbackStatus: string, properties: SnapcastStreamProperties) => void> = new Set();
    private positionUpdateListeners: Set<(streamId: string, position: number, duration: number) => void> = new Set();
    private reconnectTimer: number | null = null;
    private reconnectAttempts = 0;
    private maxReconnectAttempts = 10;

    constructor() {
        this.host = window.location.hostname;
        this.port = 1780;
    }

    // Subscribe to metadata updates
    onMetadataUpdate(listener: (streamId: string, metadata: any) => void): () => void {
        this.metadataUpdateListeners.add(listener);
        // Return unsubscribe function
        return () => {
            this.metadataUpdateListeners.delete(listener);
        };
    }

    // Subscribe to playback state updates
    onPlaybackStateUpdate(listener: (streamId: string, playbackStatus: string, properties: SnapcastStreamProperties) => void): () => void {
        this.playbackStateListeners.add(listener);
        // Return unsubscribe function
        return () => {
            this.playbackStateListeners.delete(listener);
        };
    }

    // Subscribe to position updates (for timeline/scrub bar)
    onPositionUpdate(listener: (streamId: string, position: number, duration: number) => void): () => void {
        this.positionUpdateListeners.add(listener);
        // Return unsubscribe function
        return () => {
            this.positionUpdateListeners.delete(listener);
        };
    }

    connect(): Promise<void> {
        return new Promise((resolve, reject) => {
            try {
                const wsUrl = `ws://${this.host}:${this.port}/jsonrpc`;
                this.ws = new WebSocket(wsUrl);

                this.ws.onopen = () => {
                    this.isConnected = true;
                    resolve();
                };

                this.ws.onmessage = (event) => {
                    try {
                        const message: SnapcastResponse = JSON.parse(event.data);

                        if (message.id && this.callbacks.has(message.id)) {
                            const callback = this.callbacks.get(message.id);
                            if (callback) {
                                callback(message);
                                this.callbacks.delete(message.id);
                            }
                        }

                        // Handle notifications (method-based messages)
                        if (message.method) {
                            this.handleNotification(message);
                        }
                    } catch (error) {
                        console.error('Error parsing WebSocket message:', error);
                    }
                };

                this.ws.onerror = (error) => {
                    console.error('WebSocket error:', error);
                    this.isConnected = false;
                    reject(error);
                };

                this.ws.onclose = () => {
                    console.log('[SnapcastService] WebSocket closed');
                    this.ws = null;
                    this.isConnected = false;

                    // Trigger automatic reconnection with exponential backoff
                    this.scheduleReconnect();
                };
            } catch (error) {
                reject(error);
            }
        });
    }

    private scheduleReconnect() {
        // Clear any existing reconnect timer
        if (this.reconnectTimer) {
            clearTimeout(this.reconnectTimer);
            this.reconnectTimer = null;
        }

        // Don't reconnect if we've exceeded max attempts
        if (this.reconnectAttempts >= this.maxReconnectAttempts) {
            console.error(`[SnapcastService] Max reconnection attempts (${this.maxReconnectAttempts}) reached. Please refresh the page.`);
            return;
        }

        // Calculate exponential backoff delay (1s, 2s, 4s, 8s, max 30s)
        const delay = Math.min(1000 * Math.pow(2, this.reconnectAttempts), 30000);
        this.reconnectAttempts++;

        console.log(`[SnapcastService] Scheduling reconnection attempt ${this.reconnectAttempts}/${this.maxReconnectAttempts} in ${delay}ms`);

        this.reconnectTimer = window.setTimeout(() => {
            this.attemptReconnect();
        }, delay);
    }

    private async attemptReconnect() {
        console.log(`[SnapcastService] Attempting to reconnect (attempt ${this.reconnectAttempts}/${this.maxReconnectAttempts})...`);

        try {
            await this.connect();
            console.log('[SnapcastService] ✓ Reconnected successfully');
            // Reset reconnect attempts on successful connection
            this.reconnectAttempts = 0;
        } catch (error) {
            console.error('[SnapcastService] ✗ Reconnection failed:', error);
            // scheduleReconnect will be called automatically by onclose handler
        }
    }

    private handleNotification(message: SnapcastResponse) {
        // Handle stream property updates (including metadata and playback state)
        if (message.method === 'Plugin.Stream.Player.Properties' || message.method === 'Stream.OnProperties') {
            const params = message.params;
            const streamId = params.id || params.stream_id || 'unknown';

            // Stream.OnProperties has properties nested in params.properties
            // Plugin.Stream.Player.Properties has properties directly in params
            const props = params.properties || params;

            // Notify metadata listeners if metadata changed
            if (props && props.metadata) {
                this.metadataUpdateListeners.forEach(listener => {
                    listener(streamId, props.metadata);
                });
            }

            // Notify playback state listeners if playback status changed
            if (props && props.playbackStatus !== undefined) {
                this.playbackStateListeners.forEach(listener => {
                    listener(streamId, props.playbackStatus, props);
                });
            }

            // Notify position listeners if position changed
            // Position and duration come from backend in SECONDS (already converted by control script)
            if (props && props.position !== undefined) {
                const position = props.position || 0;  // seconds
                const duration = props.metadata?.duration || 0;  // seconds
                console.log(`[SnapcastService] Position update: stream=${streamId}, position=${position}s, duration=${duration}s`);
                this.positionUpdateListeners.forEach(listener => {
                    listener(streamId, position, duration);
                });
            }
        }

        // Handle Stream.OnUpdate notification - Snapcast broadcasts this when stream changes
        // This is also sent when a NEW stream is added dynamically
        if (message.method === 'Stream.OnUpdate') {
            const params = message.params;

            // The notification structure is: {id: 'streamId', stream: {...}}
            const streamId = params.id;
            const stream = params.stream;

            if (stream && streamId) {
                // IMPORTANT: This notification is sent when:
                // 1. An existing stream's properties change (metadata, playback state, etc.)
                // 2. A NEW stream is added to Snapserver
                // We need to handle both cases

                // First, notify playback state listeners which will trigger App.tsx to check for new streams
                let playbackStatus = null;
                let hasControlScriptStatus = false;

                // Prefer properties.playbackStatus if available (from control script)
                if (stream.properties && stream.properties.playbackStatus) {
                    playbackStatus = stream.properties.playbackStatus;
                    hasControlScriptStatus = true;
                }
                // CRITICAL: Do NOT fall back to stream.status for Bluetooth/sources with choppy audio
                // stream.status reflects audio data flow (idle/playing) which can toggle rapidly
                // This causes the GUI to flicker between play/pause states
                // Instead, only use stream.status for streams without control scripts
                // For streams with control scripts, wait for Plugin.Stream.Player.Properties
                // or use the playback API data (handled separately in App.tsx)
                else if (stream.status && !streamId.includes('Bluetooth')) {
                    // Only use stream.status for streams that don't have choppy audio issues
                    // Map stream status to playback status
                    // "playing" -> "Playing", "idle" -> "Paused"
                    playbackStatus = stream.status === 'playing' ? 'Playing' :
                                    stream.status === 'idle' ? 'Paused' : 'Stopped';
                }

                // Only notify playback state listeners if we have a definitive status
                // For Bluetooth streams without properties.playbackStatus, skip notification
                // The playback API will provide the correct state via polling
                if (playbackStatus) {
                    console.log(`[SnapcastService] Stream.OnUpdate: ${streamId} status=${playbackStatus}${hasControlScriptStatus ? ' (from control script)' : ' (from stream.status)'}`);
                    this.playbackStateListeners.forEach(listener => {
                        listener(streamId, playbackStatus, stream.properties || {});
                    });
                } else {
                    console.log(`[SnapcastService] Stream.OnUpdate: ${streamId} - skipping playback state update (waiting for control script or playback API)`);
                }

                // Also check for metadata updates
                if (stream.properties && stream.properties.metadata) {
                    this.metadataUpdateListeners.forEach(listener => {
                        listener(streamId, stream.properties.metadata);
                    });
                }

                // Check for position updates in stream.properties
                if (stream.properties && stream.properties.position !== undefined) {
                    const position = stream.properties.position || 0;
                    const duration = stream.properties.metadata?.duration || 0;
                    console.log(`[SnapcastService] Position update from Stream.OnUpdate: stream=${streamId}, position=${position}s, duration=${duration}s`);
                    this.positionUpdateListeners.forEach(listener => {
                        listener(streamId, position, duration);
                    });
                }
            }
        }

        // Handle Server.OnUpdate notification (Snapcast broadcasts this when anything changes)
        // Use this as a fallback to trigger fetching latest stream properties
        if (message.method === 'Server.OnUpdate') {
            // Notify playback state listeners with a special signal to refetch
            this.playbackStateListeners.forEach(listener => {
                listener('*', 'REFRESH', {});
            });
        }
    }

    private sendRequest(method: string, params: any = {}): Promise<any> {
        return new Promise((resolve, reject) => {
            if (!this.ws || this.ws.readyState !== WebSocket.OPEN || !this.isConnected) {
                reject(new Error('WebSocket not connected'));
                return;
            }

            const id = this.messageId++;
            const request = {
                id,
                jsonrpc: '2.0',
                method,
                params
            };

            let settled = false;
            this.callbacks.set(id, (response: SnapcastResponse) => {
                settled = true;
                if (response.result !== undefined) {
                    resolve(response.result);
                } else if (response.error) {
                    // Enhanced error reporting
                    const errorMsg = response.error.message || 'Unknown error';
                    const errorCode = response.error.code;
                    const errorData = response.error.data;
                    console.error(`Snapcast API Error [${errorCode}]: ${errorMsg}`, errorData);
                    reject(new Error(`${errorMsg} (Code: ${errorCode})`));
                } else {
                    reject(new Error('No result in response'));
                }
            });

            this.ws.send(JSON.stringify(request));

            // Timeout after 5 seconds. Uses a local settled flag (not callbacks.has) so
            // reconnectTo's callbacks.clear() doesn't cause the reject to be silently dropped.
            setTimeout(() => {
                if (!settled) {
                    this.callbacks.delete(id);
                    reject(new Error('Request timeout'));
                }
            }, 5000);
        });
    }

    async getServerStatus(): Promise<any> {
        // Add a small delay to ensure connection is fully established
        await new Promise(resolve => setTimeout(resolve, 100));
        return this.sendRequest('Server.GetStatus');
    }

    // Get current stream status from server
    async getStreamStatus(streamId: string): Promise<SnapcastStream | null> {
        try {
            const serverStatus = await this.getServerStatus();
            const stream = serverStatus.server?.streams?.find((s: any) => s.id === streamId);

            if (!stream) {
                // Stream not found - this is expected when a stream is removed
                console.debug(`Stream ${streamId} not found in server status (may have been removed)`);
            }

            return stream || null;
        } catch (error) {
            // Actual WebSocket/network error
            console.error(`Failed to get stream status for ${streamId}:`, error);
            return null;
        }
    }

    // Check if stream is currently playing based on its status and properties
    isStreamPlaying(stream: SnapcastStream | null): boolean {
        if (!stream) return false;

        // First check the playbackStatus property if available (but ignore "unknown")
        if (stream.properties?.playbackStatus) {
            const status = stream.properties.playbackStatus.toLowerCase();
            if (status !== 'unknown') {
                return status === 'playing';
            }
        }

        // Fall back to the stream status field
        const status = stream.status?.toLowerCase();
        return status === 'playing';
    }

    // Set the stream for a specific group
    async setGroupStream(groupId: string, streamId: string): Promise<any> {
        return this.sendRequest('Group.SetStream', {
            id: groupId,
            stream_id: streamId
        });
    }

    // Set volume for a specific client
    async setClientVolume(clientId: string, volume: number, muted: boolean = false): Promise<any> {
        return this.sendRequest('Client.SetVolume', {
            id: clientId,
            volume: {
                percent: volume,
                muted: muted
            }
        });
    }

    // Get stream properties - this will tell us what controls are available
    async getStreamProperties(streamId: string): Promise<SnapcastStreamProperties> {
        try {
            const serverStatus = await this.getServerStatus();
            const stream = serverStatus.server?.streams?.find((s: any) => s.id === streamId);
            return stream?.properties || {};
        } catch (error) {
            console.error(`Failed to get stream properties for ${streamId}:`, error);
            return {};
        }
    }

    // Get full properties directly from control script (includes volume)
    // This uses Stream.Control with getProperties command which the control script handles
    async getStreamControlProperties(streamId: string): Promise<any> {
        try {
            const result = await this.sendStreamControl(streamId, 'getProperties');
            return result || {};
        } catch (error) {
            console.warn(`Failed to get control properties for ${streamId}:`, error);
            return {};
        }
    }

    // Use the correct Stream.Control method for playback commands [[1]](https://github.com/badaix/snapcast/blob/develop/doc/json_rpc_api/stream_plugin.md)
    private async sendStreamControl(streamId: string, command: string, params: any = {}): Promise<any> {
        return this.sendRequest('Stream.Control', {
            id: streamId,
            command: command,
            params: params
        });
    }

    // Playback controls using the correct Stream.Control API
    async playStream(streamId: string): Promise<any> {
        const properties = await this.getStreamProperties(streamId);

        if (!properties.canPlay) {
            throw new Error(`Stream ${streamId} does not support play control`);
        }

        // Try the standard play command
        try {
            return await this.sendStreamControl(streamId, 'play');
        } catch (error) {
            // If play doesn't work, try playPause if currently paused
            console.warn('Direct play failed, trying playPause:', error);
            return await this.sendStreamControl(streamId, 'playPause');
        }
    }

    async pauseStream(streamId: string): Promise<any> {
        const properties = await this.getStreamProperties(streamId);

        if (!properties.canPause) {
            throw new Error(`Stream ${streamId} does not support pause control`);
        }

        // Try the standard pause command
        try {
            return await this.sendStreamControl(streamId, 'pause');
        } catch (error) {
            // If pause doesn't work, try playPause [[1]](https://github.com/badaix/snapcast/blob/develop/doc/json_rpc_api/stream_plugin.md)
            console.warn('Direct pause failed, trying playPause:', error);
            return await this.sendStreamControl(streamId, 'playPause');
        }
    }

    async nextTrack(streamId: string): Promise<any> {
        const properties = await this.getStreamProperties(streamId);

        if (!properties.canGoNext) {
            throw new Error(`Stream ${streamId} does not support next track control`);
        }

        // Use the next command [[1]](https://github.com/badaix/snapcast/blob/develop/doc/json_rpc_api/stream_plugin.md)
        return this.sendStreamControl(streamId, 'next');
    }

    async previousTrack(streamId: string): Promise<any> {
        const properties = await this.getStreamProperties(streamId);

        if (!properties.canGoPrevious) {
            throw new Error(`Stream ${streamId} does not support previous track control`);
        }

        // Use the previous command
        try {
            return await this.sendStreamControl(streamId, 'previous');
        } catch (error) {
            // Some plugins might use 'prev' instead
            console.warn('Previous failed, trying prev:', error);
            return await this.sendStreamControl(streamId, 'prev');
        }
    }

    async seekTo(streamId: string, position: number): Promise<any> {
        const properties = await this.getStreamProperties(streamId);
        if (!properties.canSeek) {
            throw new Error(`Stream ${streamId} does not support seek control`);
        }

        // Use the seek command with position parameter
        return this.sendStreamControl(streamId, 'seek', {position: position});
    }

    // Toggle play/pause - more reliable for many stream plugins
    async togglePlayPause(streamId: string): Promise<any> {
        const properties = await this.getStreamProperties(streamId);
        if (!properties.canPause) {
            throw new Error(`Stream ${streamId} does not support play/pause control`);
        }

        return this.sendStreamControl(streamId, 'playPause');
    }

    // Stop playback
    async stopStream(streamId: string): Promise<any> {
        return this.sendStreamControl(streamId, 'stop');
    }

    // Set source volume (controls the integration - AirPlay, Spotify, etc.)
    async setStreamVolume(streamId: string, volume: number): Promise<any> {
        const clampedVolume = Math.max(0, Math.min(100, Math.round(volume)));
        return this.sendStreamControl(streamId, 'setVolume', {volume: clampedVolume});
    }

    // Get source volume from stream properties
    async getStreamVolume(streamId: string): Promise<number> {
        try {
            const properties = await this.getStreamProperties(streamId);
            return properties.volume ?? 100;
        } catch (error) {
            console.error(`Failed to get stream volume for ${streamId}:`, error);
            return 100;
        }
    }

    // Check if stream supports various controls
    async getStreamCapabilities(streamId: string): Promise<{
        canPlay: boolean;
        canPause: boolean;
        canSeek: boolean;
        canGoNext: boolean;
        canGoPrevious: boolean;
        playbackStatus?: string;
    }> {
        try {
            const properties = await this.getStreamProperties(streamId);
            return {
                canPlay: properties.canPlay || false,
                canPause: properties.canPause || false,
                canSeek: properties.canSeek || false,
                canGoNext: properties.canGoNext || false,
                canGoPrevious: properties.canGoPrevious || false,
                playbackStatus: properties.playbackStatus || 'Unknown'
            };
        } catch (error) {
            console.error(`Failed to get stream capabilities for ${streamId}:`, error);
            return {
                canPlay: false,
                canPause: false,
                canSeek: false,
                canGoNext: false,
                canGoPrevious: false
            };
        }
    }

    // Get current playback state from stream properties
    async getPlaybackState(streamId: string): Promise<'playing' | 'paused' | 'stopped' | 'unknown'> {
        try {
            const properties = await this.getStreamProperties(streamId);
            const status = properties.playbackStatus?.toLowerCase();

            if (status === 'playing') return 'playing';
            if (status === 'paused') return 'paused';
            if (status === 'stopped') return 'stopped';

            return 'unknown';
        } catch (error) {
            console.error(`Failed to get playback state for stream ${streamId}:`, error);
            return 'unknown';
        }
    }

    // Find which group a client belongs to
    async findClientGroup(clientId: string): Promise<string | null> {
        try {
            const status = await this.getServerStatus();
            const {server} = status;

            if (server && server.groups) {
                for (const group of server.groups) {
                    if (group.clients && group.clients.some((client: any) => client.id === clientId)) {
                        return group.id;
                    }
                }
            }
            return null;
        } catch (error) {
            console.error('Error finding client group:', error);
            return null;
        }
    }

    /**
     * Get the base HTTP URL for the Snapcast server
     * Used for constructing cover art URLs
     */
    getHttpUrl(): string {
        return `http://${this.host}:${this.port}`;
    }

    /**
     * Reconnect to a different Snapcast server host/port.
     * Used when slave mode switches snapclient to a remote master server.
     */
    async reconnectTo(host: string, port: number): Promise<void> {
        if (this.host === host && this.port === port && this.isConnected) {
            return; // Already connected to this target
        }

        console.log(`[SnapcastService] Switching connection: ${this.host}:${this.port} → ${host}:${port}`);

        // Cancel any pending reconnect timer
        if (this.reconnectTimer) {
            clearTimeout(this.reconnectTimer);
            this.reconnectTimer = null;
        }
        this.reconnectAttempts = 0;

        // Close existing connection without triggering auto-reconnect
        if (this.ws) {
            this.ws.onclose = null;
            this.ws.onerror = null;
            this.ws.close();
            this.ws = null;
        }
        this.isConnected = false;
        this.callbacks.clear(); // settled flags in each timeout will fire the reject after 5s

        // Update target and connect
        this.host = host;
        this.port = port;
        return this.connect();
    }

    disconnect() {
        if (this.ws) {
            this.ws.close();
            this.ws = null;
        }
        this.callbacks.clear();
        this.isConnected = false;
    }
}

export const snapcastService = new SnapcastService();