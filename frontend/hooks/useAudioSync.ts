import {useEffect, useRef} from 'react';
import type {Stream} from '../types';

/**
 * Audio sync hook for hybrid client/server progress tracking.
 *
 * HYBRID APPROACH:
 * - Backend provides authoritative baseline position (via stream.playback.interpolated_position)
 * - Frontend does smooth 1-second local interpolation for UI smoothness
 * - Resync to backend on: page load, track change, play/pause events, seeks
 *
 * SEEK DETECTION:
 * - Track server's last reported position AND timestamp
 * - Calculate expected position based on elapsed time
 * - If server position differs significantly from expected, that's a seek
 * - This avoids thrashing from polling timing differences
 *
 * This ensures:
 * - Page refresh preserves timeline progress (backend is source of truth)
 * - Smooth UI animations (local interpolation between server updates)
 * - Proper sync on track changes, playback state changes, and seeks
 */
export const useAudioSync = (
    stream: Stream | undefined,
    updateProgress: (streamId: string, newProgress: number) => void
) => {
    const intervalRef = useRef<number | null>(null);
    const lastProgressRef = useRef<number>(0);
    const lastTrackRef = useRef<string>('');
    const lastPlayingRef = useRef<boolean>(false);
    const lastStreamIdRef = useRef<string>('');
    const hasInitializedRef = useRef<boolean>(false);
    const hadPlaybackDataRef = useRef<boolean>(false);
    // For seek detection: track server's reported position and when we received it
    const lastServerPositionRef = useRef<number>(0);
    const lastServerUpdateTimeRef = useRef<number>(Date.now());

    useEffect(() => {
        // Clear any existing interval
        if (intervalRef.current) {
            clearInterval(intervalRef.current);
            intervalRef.current = null;
        }

        if (!stream) {
            return;
        }

        // Build track identifier for change detection
        const currentTrack = `${stream.currentTrack?.title || ''}-${stream.currentTrack?.artist || ''}`;

        // Get server position: prefer playback.interpolated_position, fallback to stream.progress
        // IMPORTANT: Only use playback data if it's not stale to avoid oscillation
        const hasValidPlaybackData = stream.playback?.interpolated_position !== undefined && !stream.playback.is_stale;
        const serverPosition = hasValidPlaybackData
            ? stream.playback.interpolated_position / 1000  // Convert ms to seconds
            : stream.progress;

        // DEBUG: Log position sources for Bluetooth streams
        if (stream.id.includes('Bluetooth')) {
            console.log(`[useAudioSync] Position sources: playback=${stream.playback?.interpolated_position}ms (stale=${stream.playback?.is_stale}), progress=${stream.progress}s, using=${serverPosition.toFixed(1)}s`);
        }

        // Detect resync conditions
        const isNewStream = stream.id !== lastStreamIdRef.current;
        const isInitial = !hasInitializedRef.current || isNewStream;

        // Reset playback data tracking on stream change
        if (isNewStream) {
            hadPlaybackDataRef.current = false;
        }
        const trackChanged = currentTrack !== lastTrackRef.current && lastTrackRef.current !== '' && !isInitial;
        const playStateChanged = stream.isPlaying !== lastPlayingRef.current && lastTrackRef.current !== '' && !isInitial;
        const transitioningToPaused = playStateChanged && !stream.isPlaying;
        const transitioningToPlaying = playStateChanged && stream.isPlaying;

        // DEBUG: Log state changes for Bluetooth streams
        if (stream.id.includes('Bluetooth') && playStateChanged) {
            console.log(`[DEBUG useAudioSync] Play state changed: ${lastPlayingRef.current} → ${stream.isPlaying}`);
        }

        // Detect when playback API data becomes available for the first time
        // This happens after initial render when polling attaches playback data to stream
        const hasPlaybackData = stream.playback?.interpolated_position !== undefined && !stream.playback.is_stale;
        const playbackDataJustArrived = hasPlaybackData && !hadPlaybackDataRef.current;

        // Seek detection: compare server position to what we'd expect based on elapsed time
        // This avoids thrashing because normal playback has server position ≈ expected position
        // Only check for seeks when playing (paused position shouldn't change)
        let isSeek = false;
        if (!isInitial && !trackChanged && !playStateChanged && stream.isPlaying) {
            const now = Date.now();
            const elapsedSeconds = (now - lastServerUpdateTimeRef.current) / 1000;
            const expectedServerPosition = lastServerPositionRef.current + elapsedSeconds;
            // Use 3-second threshold (reduced from 5) for more responsive seek detection
            const positionDelta = Math.abs(serverPosition - expectedServerPosition);

            // DEBUG: Log seek detection for Bluetooth streams
            if (stream.id.includes('Bluetooth')) {
                console.log(`[useAudioSync] Seek check: server=${serverPosition.toFixed(1)}s, expected=${expectedServerPosition.toFixed(1)}s, delta=${positionDelta.toFixed(1)}s, threshold=3s`);
            }

            if (positionDelta > 3) {
                isSeek = true;
                console.log(`[useAudioSync] SEEK DETECTED: ${lastProgressRef.current.toFixed(1)}s → ${serverPosition.toFixed(1)}s`);
            }
        }

        // When transitioning to paused: preserve current position unless server shows significantly different
        // This prevents resetting to 0 when server position data is stale or unavailable
        if (transitioningToPaused) {
            const positionDelta = Math.abs(serverPosition - lastProgressRef.current);
            // Only update if server position differs by >5s (indicating seek before pause)
            // AND server position is valid (>0, unless we're at the very start of a track)
            const shouldUpdatePosition = positionDelta > 5 && (serverPosition > 0 || lastProgressRef.current < 5);

            console.log(`[useAudioSync] Pause: keeping position at ${lastProgressRef.current.toFixed(1)}s (server: ${serverPosition.toFixed(1)}s, delta: ${positionDelta.toFixed(1)}s)`);

            lastTrackRef.current = currentTrack;
            lastPlayingRef.current = stream.isPlaying;
            lastStreamIdRef.current = stream.id;
            hasInitializedRef.current = true;

            if (shouldUpdatePosition) {
                console.log(`[useAudioSync] Pause with seek: updating to ${serverPosition.toFixed(1)}s`);
                lastProgressRef.current = serverPosition;
                updateProgress(stream.id, serverPosition);
            }
            // Don't update progress - keep current position when pausing
        }
        // Resync for other conditions (initial, track change, resume, seek, playback data arrival)
        else if (isInitial || trackChanged || transitioningToPlaying || isSeek || playbackDataJustArrived) {
            const reason = isInitial ? 'initial' :
                           trackChanged ? 'track-change' :
                           transitioningToPlaying ? 'resume' :
                           playbackDataJustArrived ? 'playback-data-arrived' : 'seek';

            console.log(`[useAudioSync] Resync (${reason}): ${lastProgressRef.current.toFixed(1)}s → ${serverPosition.toFixed(1)}s`);

            lastProgressRef.current = serverPosition;
            lastTrackRef.current = currentTrack;
            lastPlayingRef.current = stream.isPlaying;
            lastStreamIdRef.current = stream.id;
            hasInitializedRef.current = true;

            // Update progress immediately
            updateProgress(stream.id, serverPosition);
        } else if (stream.id.includes('Bluetooth')) {
            // DEBUG: Log why we didn't resync
            console.log(`[useAudioSync] No resync: isInitial=${isInitial}, trackChanged=${trackChanged}, transitioningToPlaying=${transitioningToPlaying}, isSeek=${isSeek}, playbackDataJustArrived=${playbackDataJustArrived}`);
        }

        // Always update server position tracking (for seek detection on next poll)
        lastServerPositionRef.current = serverPosition;
        lastServerUpdateTimeRef.current = Date.now();

        // Track playback data availability for next iteration
        hadPlaybackDataRef.current = hasPlaybackData;

        // Start local interpolation when playing
        if (stream.isPlaying) {
            intervalRef.current = window.setInterval(() => {
                // Don't increment past duration if we have playback data
                const duration = stream.playback?.duration
                    ? stream.playback.duration / 1000
                    : stream.currentTrack?.duration || Infinity;

                const newProgress = Math.min(lastProgressRef.current + 1, duration);
                lastProgressRef.current = newProgress;
                updateProgress(stream.id, newProgress);
            }, 1000);
        }

        return () => {
            if (intervalRef.current) {
                clearInterval(intervalRef.current);
            }
        };
        // Dependencies: include interpolated_position for seek detection, but the smart
        // seek detection logic prevents thrashing by comparing to expected position
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [
        stream?.isPlaying,
        stream?.id,
        stream?.playback?.interpolated_position,
        stream?.currentTrack?.title,
        stream?.currentTrack?.artist,
        updateProgress
    ]);
};
