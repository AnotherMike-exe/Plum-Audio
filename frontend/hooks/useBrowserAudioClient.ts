import { useState, useEffect, useRef } from 'react';
import { SnapStream } from '../services/snapStreamService';

export interface BrowserAudioClientState {
    isActive: boolean;
    isConnecting: boolean; // True while connection is in progress
    isPlaying: boolean;
    volume: number;
    muted: boolean;
    clientId: string;
    currentHost: string | null; // Track which host we're connected to
}

export function useBrowserAudioClient(defaultHost: string) {
    const [state, setState] = useState<BrowserAudioClientState>({
        isActive: false,
        isConnecting: false,
        isPlaying: false,
        volume: 100,
        muted: false,
        clientId: SnapStream.getClientId(),
        currentHost: null
    });

    const snapStreamRef = useRef<SnapStream | null>(null);

    // Start the browser audio client (optionally with a different host)
    const start = async (startMuted: boolean = false, host?: string, port: number = 1780) => {
        const targetHost = host || defaultHost;

        // Prevent multiple simultaneous connection attempts
        if (snapStreamRef.current || state.isConnecting) {
            console.warn("Browser audio client already started or connecting");
            return;
        }

        // Set connecting state
        setState(prev => ({ ...prev, isConnecting: true }));

        try {
            const stream = new SnapStream(targetHost, port);
            // SnapStream.start() now has built-in retry logic and waits for connection
            await stream.start();
            stream.resume(); // Resume audio context (requires user interaction)

            snapStreamRef.current = stream;

            // Set initial volume (muted or unmuted)
            if (startMuted) {
                stream.setVolume(100, true); // Start muted for visualizer
            }

            setState(prev => ({
                ...prev,
                isActive: true,
                isConnecting: false,
                isPlaying: true,
                muted: startMuted,
                currentHost: targetHost
            }));

            console.log(`Browser audio client started on ${targetHost}:${port} (${startMuted ? 'muted' : 'unmuted'})`);
        } catch (error) {
            // Clear connecting state on failure
            setState(prev => ({ ...prev, isConnecting: false }));
            console.error("Failed to start browser audio client:", error);
            throw error;
        }
    };

    // Restart the browser audio client with a different host
    const restart = async (newHost: string, startMuted: boolean = false, port: number = 1780) => {
        // Stop current connection if any
        if (snapStreamRef.current) {
            snapStreamRef.current.stop();
            snapStreamRef.current = null;
        }

        // Start with new host
        await start(startMuted, newHost, port);
    };

    // Stop the browser audio client
    const stop = () => {
        if (!snapStreamRef.current) {
            // Still clear connecting state in case we're stopping during connection
            setState(prev => ({ ...prev, isConnecting: false }));
            return;
        }

        snapStreamRef.current.stop();
        snapStreamRef.current = null;

        setState(prev => ({
            ...prev,
            isActive: false,
            isConnecting: false,
            isPlaying: false
        }));

        console.log("Browser audio client stopped");
    };

    // Set volume
    const setVolume = (volume: number, muted?: boolean) => {
        const isMuted = muted !== undefined ? muted : state.muted;

        if (snapStreamRef.current) {
            snapStreamRef.current.setVolume(volume, isMuted);
        }

        setState(prev => ({
            ...prev,
            volume,
            muted: isMuted
        }));
    };

    // Toggle mute
    const toggleMute = () => {
        const newMuted = !state.muted;

        if (snapStreamRef.current) {
            snapStreamRef.current.setVolume(state.volume, newMuted);
        }

        setState(prev => ({
            ...prev,
            muted: newMuted
        }));
    };

    // Cleanup on unmount
    useEffect(() => {
        return () => {
            if (snapStreamRef.current) {
                snapStreamRef.current.stop();
            }
        };
    }, []);

    // Get muted state (for visualizer UI)
    const isMuted = () => state.muted;

    return {
        state,
        start,
        stop,
        restart, // Restart with a different host (for switching servers)
        setVolume,
        toggleMute,
        isMuted,
        getSnapStream: () => snapStreamRef.current // Expose for visualizer
    };
}
