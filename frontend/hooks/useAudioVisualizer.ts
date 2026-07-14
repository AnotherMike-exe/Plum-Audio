import { useEffect, useRef, useState } from 'react';
import type { SnapStream } from '../services/snapStreamService';
import type { VisualizerSettings } from '../types';

export interface AudioVisualizerData {
    frequencyData: Uint8Array;
    timestamp: number;
}

export function useAudioVisualizer(
    snapStream: SnapStream | null,
    settings: VisualizerSettings,
    enabled: boolean
) {
    const [audioData, setAudioData] = useState<AudioVisualizerData | null>(null);
    const animationFrameRef = useRef<number | undefined>(undefined);

    useEffect(() => {
        if (!enabled || !snapStream) {
            setAudioData(null);
            return;
        }

        // Update analyser settings
        const fftSize = calculateFFTSize(settings.barCount);
        snapStream.updateAnalyserSettings(fftSize, settings.smoothing);

        // Animation loop
        const updateAudioData = () => {
            const data = snapStream.getFrequencyData();
            if (data) {
                setAudioData({
                    frequencyData: data,
                    timestamp: Date.now()
                });
            }
            animationFrameRef.current = requestAnimationFrame(updateAudioData);
        };

        animationFrameRef.current = requestAnimationFrame(updateAudioData);

        return () => {
            if (animationFrameRef.current) {
                cancelAnimationFrame(animationFrameRef.current);
            }
        };
    }, [enabled, snapStream, settings.barCount, settings.smoothing]);

    return audioData;
}

// FFT size must be power of 2
function calculateFFTSize(barCount: number): number {
    if (barCount <= 32) return 512;
    if (barCount <= 64) return 1024;
    if (barCount <= 128) return 2048;
    return 4096;
}
