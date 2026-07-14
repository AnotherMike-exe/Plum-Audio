import {snapcastService} from './snapcastService';
import {getStreamPlayback} from './playbackService';
import type {Client, Stream, Track} from '../types';

// Helper function to validate album art URLs
// Detects corrupted data URLs containing binary data instead of proper base64
function isValidAlbumArtUrl(url: string | undefined | null): boolean {
    if (!url || typeof url !== 'string' || url.trim() === '') {
        return false;
    }

    // For data URLs, check if they contain binary characters (null bytes, control chars)
    if (url.startsWith('data:image')) {
        // Check for null bytes or other control characters (except newlines)
        // These indicate binary data was incorrectly placed in a string
        for (let i = 0; i < Math.min(url.length, 200); i++) {
            const code = url.charCodeAt(i);
            // Null bytes, or control chars < 32 (except \n=10, \r=13, \t=9)
            if (code === 0 || (code < 32 && code !== 10 && code !== 13 && code !== 9)) {
                console.error(`[ArtValidation] ❌ Corrupted data URL detected at char ${i}: byte=${code} (${url.substring(0, 100)}...)`);
                return false;
            }
        }

        // Valid base64 should only contain A-Z, a-z, 0-9, +, /, =, and whitespace
        const base64Part = url.substring(url.indexOf(',') + 1);
        if (base64Part && !/^[A-Za-z0-9+/=\s]+$/.test(base64Part.substring(0, 100))) {
            console.error(`[ArtValidation] ❌ Invalid base64 characters detected: ${base64Part.substring(0, 100)}...`);
            return false;
        }
    }

    return true;
}

// Default fallback data for when no metadata is available
const createDefaultTrack = (): Track => ({
    id: 'unknown',
    title: 'Unknown Track',
    artist: 'Unknown Artist',
    album: 'Unknown Album',
    albumArtUrl: 'data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iNDAwIiBoZWlnaHQ9IjQwMCIgdmlld0JveD0iMCAwIDQwMCA0MDAiIHhtbG5zPSJodHRwOi8vd3d3LnczLm9yZy8yMDAwL3N2ZyI+CjxyZWN0IHdpZHRoPSI0MDAiIGhlaWdodD0iNDAwIiBmaWxsPSIjMkEyQTM2Ii8+CjxnIHRyYW5zZm9ybT0idHJhbnNsYXRlKDEwMCwgMTAwKSBzY2FsZSgxMi41KSI+CjxwYXRoIGZpbGw9IiNGMEYwRjAiIGQ9Ik00IDN2OS40Yy0wLjQtMC4yLTAuOS0wLjQtMS41LTAuNC0xLjQgMC0yLjUgMC45LTIuNSAyczEuMSAyIDIuNSAyIDIuNS0wLjkgMi41LTJ2LTcuM2w3LTIuM3Y1LjFjLTAuNC0wLjMtMC45LTAuNS0xLjUtMC41LTEuNCAwLTIuNSAwLjktMi41IDJzMS4xIDIgMi41IDIgMi41LTAuOSAyLjUtMnYtMTFsLTkgM3oiPjwvcGF0aD4KPC9nPgo8L3N2Zz4K',
    duration: 0,
});

const extractMetadataFromStream = (snapStream: any): {
    title?: string;
    artist?: string;
    album?: string;
    artUrl?: string;
    duration?: number
} => {
    // Check for metadata in different possible locations
    let metadata: any = {};

    // Check if stream has properties with metadata
    if (snapStream.properties) {

        // Look for metadata in properties
        if (snapStream.properties.metadata) {
            // Snapcast control script metadata format (simple field names)
            const meta = snapStream.properties.metadata;
            metadata = {
                // Simple field names (title, artist, album, artUrl)
                title: meta.title || meta.name,
                artist: Array.isArray(meta.artist) ? meta.artist.join(', ') : meta.artist,
                album: meta.album,
                artUrl: meta.artUrl,
                duration: meta.duration
            };
        } else if (snapStream.properties.meta) {
            metadata = snapStream.properties.meta;
        } else {
            // Check for individual metadata fields in properties
            metadata = {
                title: snapStream.properties.title || snapStream.properties.TITLE,
                artist: snapStream.properties.artist || snapStream.properties.ARTIST,
                album: snapStream.properties.album || snapStream.properties.ALBUM,
                artUrl: snapStream.properties.artUrl || snapStream.properties.ART_URL,
                duration: snapStream.properties.duration || snapStream.properties.DURATION
            };
        }
    }

    // Also check direct meta property on stream
    if (snapStream.meta) {
        metadata = {...metadata, ...snapStream.meta};
    }

    return metadata;
};

const formatArtist = (artist: any): string => {
    if (Array.isArray(artist)) {
        return artist.join(', ');
    }
    return artist || 'Unknown Artist';
};

const getStreamName = (snapStream: any): string => {
    // Try to get a meaningful name from various sources
    if (snapStream.properties?.name) {
        return snapStream.properties.name;
    }

    // Use the name from URI query if available
    if (snapStream.uri?.query?.name) {
        return snapStream.uri.query.name;
    }

    if (snapStream.uri?.path) {
        const pathName = snapStream.uri.path.split('/').pop();
        if (pathName && pathName !== '') {
            return pathName;
        }
    }

    if (snapStream.uri?.raw) {
        return snapStream.uri.raw;
    }

    return `Stream ${snapStream.id}`;
};

const getSourceDevice = (snapStream: any): string => {
    // Try to extract source device info
    if (snapStream.uri?.scheme) {
        switch (snapStream.uri.scheme.toLowerCase()) {
            case 'pipe':
                return 'Named Pipe';
            case 'file':
                return 'File';
            case 'tcp':
                return 'TCP Stream';
            case 'alsa':
                return 'ALSA Device';
            case 'spotify':
                return 'Spotify Connect';
            case 'airplay':
                return 'AirPlay';
            default:
                return snapStream.uri.scheme.toUpperCase();
        }
    }

    return 'Unknown Source';
};

const convertSnapcastStreamToStream = async (snapStream: any): Promise<Stream> => {
    const metadata = extractMetadataFromStream(snapStream);

    // Handle artwork URL from metadata
    let albumArtUrl = createDefaultTrack().albumArtUrl;

    if (metadata.artUrl) {
        let resolvedUrl = metadata.artUrl;

        // Check if it's a data URL (embedded image)
        if (metadata.artUrl.startsWith('data:')) {
            // Data URL - use directly
            resolvedUrl = metadata.artUrl;
        } else if (metadata.artUrl.startsWith('/')) {
            // Relative path from Snapserver
            // Route through our CORS proxy for /coverart/ paths to enable ColorThief extraction
            if (metadata.artUrl.startsWith('/coverart/')) {
                // Extract filename and use proxy endpoint (runs on federation service port 5001)
                const filename = metadata.artUrl.replace('/coverart/', '');
                const apiPort = window.location.hostname === 'localhost' ? '5001' : '5001';
                resolvedUrl = `http://${window.location.hostname}:${apiPort}/api/settings/proxy/coverart/${filename}`;
            } else {
                // Other relative paths - use Snapserver directly
                resolvedUrl = `${snapcastService.getHttpUrl()}${metadata.artUrl}`;
            }
        } else {
            // Absolute URL - use directly
            resolvedUrl = metadata.artUrl;
        }

        // Validate before using
        if (isValidAlbumArtUrl(resolvedUrl)) {
            albumArtUrl = resolvedUrl;
        } else {
            console.error(`[DataService] ❌ Invalid artwork URL detected - using placeholder instead`);
            // Keep default placeholder
        }
    }

    const track: Track = {
        id: snapStream.id || 'unknown',
        title: metadata.title || 'Unknown Track',
        artist: formatArtist(metadata.artist),
        album: metadata.album || 'Unknown Album',
        albumArtUrl: albumArtUrl,
        // Duration comes from backend in seconds (per Snapcast API)
        duration: metadata.duration ? Math.floor(metadata.duration) : 0,
    };

    // Determine if stream is playing - check properties.playbackStatus first, then fall back to status
    let isPlaying = false;
    if (snapStream.properties?.playbackStatus && snapStream.properties.playbackStatus.toLowerCase() !== 'unknown') {
        // Use playbackStatus from control script (more accurate) - but ignore "unknown"
        isPlaying = snapStream.properties.playbackStatus.toLowerCase() === 'playing';
    } else {
        // Fall back to stream status
        isPlaying = snapStream.status === 'playing';
    }

    // Extract position - prefer playback API for real-time interpolation
    let progress = 0;
    let duration = track.duration;

    // Helper to extract position from Snapcast stream properties
    const getPositionFromProperties = (): number => {
        if (snapStream.properties?.position !== undefined && snapStream.properties.position !== null) {
            return Math.floor(snapStream.properties.position);  // Already in seconds
        }
        return 0;
    };

    // Try to get position from playback API (server-side interpolation)
    // This ensures page refreshes show correct position immediately
    if (isPlaying && !snapStream.id.includes('none-')) {
        try {
            const playbackData = await getStreamPlayback(snapStream.id);
            if (playbackData && !playbackData.is_stale) {
                // Use interpolated position from playback API
                progress = Math.floor(playbackData.interpolated_position / 1000);
                duration = Math.floor(playbackData.duration / 1000);
                console.log(`[DataService] Loaded initial position from playback API: ${progress}s / ${duration}s for stream ${snapStream.id}`);
            } else {
                // Fall back to stream properties
                progress = getPositionFromProperties();
            }
        } catch (error) {
            console.warn(`[DataService] Failed to fetch playback data for ${snapStream.id}:`, error);
            // Fall back to stream properties
            progress = getPositionFromProperties();
        }
    } else {
        // Not playing or none stream - use stream properties
        progress = getPositionFromProperties();
    }

    // Extract source volume from stream properties (set by control script)
    // Snapcast's Server.GetStatus doesn't include volume from control script notifications,
    // so we need to fetch it directly from the control script via Stream.Control getProperties
    let sourceVolume = snapStream.properties?.volume;

    // If volume not in stream properties and stream can be controlled, fetch from control script
    if (sourceVolume === undefined && snapStream.properties?.canControl && !snapStream.id.includes('none-')) {
        try {
            const controlProps = await snapcastService.getStreamControlProperties(snapStream.id);
            if (typeof controlProps?.volume === 'number') {
                sourceVolume = controlProps.volume;
            }
        } catch (error) {
            console.warn(`[DataService] Failed to get control properties for ${snapStream.id}:`, error);
        }
    }

    return {
        id: snapStream.id,
        name: getStreamName(snapStream),
        sourceDevice: getSourceDevice(snapStream),
        currentTrack: {
            ...track,
            duration: duration  // Update duration from playback API if available
        },
        isPlaying: isPlaying,
        progress: progress,
        volume: typeof sourceVolume === 'number' ? sourceVolume : undefined,
    };
};

const convertSnapcastClientToClient = (snapClient: any, groupStreamId: string | null): Client => ({
    id: snapClient.id,
    name: snapClient.config?.name || snapClient.host?.name || 'Unknown Device',
    currentStreamId: groupStreamId,
    volume: snapClient.config?.volume?.percent || 0,
});

export const getSnapcastData = async (): Promise<{
    initialStreams: Stream[];
    initialClients: Client[];
    serverName?: string;
}> => {
    try {
        // Connect to Snapcast server
        await snapcastService.connect();

        // Get server status
        const serverStatus = await snapcastService.getServerStatus();
        const {server} = serverStatus;

        if (!server) {
            throw new Error('Invalid server response');
        }

        // Fix: Extract server name correctly as a string
        const serverName = server.server?.snapserver?.name ||
            server.server?.host?.name ||
            'Snapcast Server';

        // Convert streams (now async to fetch artwork)
        const streamPromises = server.streams?.map((snapStream: any) =>
            convertSnapcastStreamToStream(snapStream)
        ) || [];
        const initialStreams: Stream[] = await Promise.all(streamPromises);

        // Convert clients from groups
        const initialClients: Client[] = [];

        if (server.groups) {
            server.groups.forEach((group: any) => {
                if (group.clients) {
                    group.clients.forEach((client: any) => {
                        initialClients.push(convertSnapcastClientToClient(client, group.stream_id));
                    });
                }
            });
        }

        // If no clients found, create a default "You" client
        if (initialClients.length === 0) {
            initialClients.push({
                id: 'client-1',
                name: 'My Device (You)',
                currentStreamId: initialStreams.length > 0 ? initialStreams[0].id : null,
                volume: 75,
            });
        }

        return {
            initialStreams,
            initialClients,
            serverName
        };

    } catch (error) {
        console.error('Failed to connect to Snapcast server:', error);

        // Fallback to basic mock data structure with connection error info
        return {
            initialStreams: [{
                id: 'error-stream',
                name: 'Connection Error',
                sourceDevice: 'Snapcast Server Unavailable',
                currentTrack: createDefaultTrack(),
                isPlaying: false,
                progress: 0,
            }],
            initialClients: [{
                id: 'client-1',
                name: 'My Device (You)',
                currentStreamId: 'error-stream',
                volume: 75,
            }],
            serverName: 'Snapcast Server (Disconnected)'
        };
    }
};