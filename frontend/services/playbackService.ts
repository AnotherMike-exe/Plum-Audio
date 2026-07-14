/**
 * Playback Service
 * Provides real-time playback position data independent of Snapcast's notification system.
 *
 * This service polls our backend playback API which receives position updates from
 * control scripts. This avoids audio stuttering that occurs when position updates
 * are pushed through Snapcast's Plugin.Stream.Player.Properties notifications.
 */

const PLAYBACK_API_URL = '/api/playback';

export interface PlaybackData {
  stream_id: string;
  position: number;  // milliseconds
  duration: number;  // milliseconds
  playback_status: 'playing' | 'paused' | 'stopped' | 'unknown';
  interpolated_position: number;  // milliseconds (calculated by server based on elapsed time)
  timestamp: number;  // Unix timestamp when position was last updated
  age_seconds: number;  // How long since the position was updated
  is_stale: boolean;  // Whether the data is too old to be reliable
  // Out-of-band metadata + source volume, delivered through the playback API instead of
  // Snapcast Properties (avoids onResync stutter and the partial-payload flap). See
  // fd95db0 / docs/ARCHITECTURE.md. Present only for sources that post them (e.g. AirPlay).
  title?: string;
  artist?: string;
  album?: string;
  artUrl?: string;  // self-contained data: URL
  volume?: number;  // source volume 0-100
}

export interface PlaybackResponse {
  success: boolean;
  streams: Record<string, PlaybackData>;
}

export interface SinglePlaybackResponse extends PlaybackData {
  success: boolean;
}

/**
 * Get playback data for all streams
 */
export async function getAllPlayback(): Promise<Record<string, PlaybackData>> {
  try {
    const response = await fetch(PLAYBACK_API_URL);
    if (!response.ok) {
      console.warn('[PlaybackService] Failed to fetch playback data:', response.status);
      return {};
    }
    const data: PlaybackResponse = await response.json();
    return data.success ? data.streams : {};
  } catch (error) {
    console.warn('[PlaybackService] Error fetching playback data:', error);
    return {};
  }
}

/**
 * Get playback data for a specific stream
 */
export async function getStreamPlayback(streamId: string): Promise<PlaybackData | null> {
  try {
    const encodedStreamId = encodeURIComponent(streamId);
    const response = await fetch(`${PLAYBACK_API_URL}/${encodedStreamId}`);
    if (!response.ok) {
      console.warn('[PlaybackService] Failed to fetch stream playback:', response.status);
      return null;
    }
    const data: SinglePlaybackResponse = await response.json();
    // API now returns 200 with success: false when no data available (instead of 404)
    // This prevents browser console spam from 404 errors
    return data.success ? data : null;
  } catch (error) {
    console.warn('[PlaybackService] Error fetching stream playback:', error);
    return null;
  }
}

/**
 * Convert playback data to the format used by useAudioSync
 */
export function toAudioSyncFormat(playback: PlaybackData | null): {
  position: number | undefined;  // seconds
  duration: number | undefined;  // seconds
  isPlaying: boolean;
} {
  if (!playback || playback.is_stale) {
    return {
      position: undefined,
      duration: undefined,
      isPlaying: false,
    };
  }

  return {
    // Convert from milliseconds to seconds
    position: playback.interpolated_position / 1000,
    duration: playback.duration / 1000,
    isPlaying: playback.playback_status === 'playing',
  };
}

export default {
  getAllPlayback,
  getStreamPlayback,
  toAudioSyncFormat,
};
