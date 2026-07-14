import React, { useEffect, useRef } from 'react';
import { useAudioVisualizer } from '../hooks/useAudioVisualizer';
import type { SnapStream } from '../services/snapStreamService';
import type { VisualizerSettings } from '../types';

interface AudioVisualizerProps {
    snapStream: SnapStream | null;
    settings: VisualizerSettings;
    albumArtSize: number;      // Size in pixels
    accentColor: string;
    albumArtColor?: string;
}

export const AudioVisualizer: React.FC<AudioVisualizerProps> = ({
    snapStream,
    settings,
    albumArtSize,
    accentColor,
    albumArtColor
}) => {
    const canvasRef = useRef<HTMLCanvasElement>(null);
    const audioData = useAudioVisualizer(snapStream, settings, settings.enabled);

    const canvasSize = calculateCanvasSize(albumArtSize, settings.size);

    useEffect(() => {
        if (!settings.enabled || !audioData || !canvasRef.current) {
            return;
        }

        const canvas = canvasRef.current;
        const ctx = canvas.getContext('2d');
        if (!ctx) return;

        // Clear canvas
        ctx.clearRect(0, 0, canvas.width, canvas.height);

        // Draw circular bars
        drawCircularBars(ctx, audioData.frequencyData, {
            centerX: canvas.width / 2,
            centerY: canvas.height / 2,
            innerRadius: albumArtSize / 2,
            barCount: settings.barCount,
            sensitivity: settings.sensitivity,
            color: getVisualizerColor(settings, accentColor, albumArtColor)
        });

    }, [audioData, settings, albumArtSize, accentColor, albumArtColor]);

    if (!settings.enabled) return null;

    return (
        <canvas
            ref={canvasRef}
            width={canvasSize}
            height={canvasSize}
            className="absolute top-1/2 left-1/2 transform -translate-x-1/2 -translate-y-1/2"
            style={{ zIndex: 0 }}
        />
    );
};

function calculateCanvasSize(albumArtSize: number, size: 'small' | 'medium' | 'large'): number {
    const multipliers = { small: 1.3, medium: 1.5, large: 1.8 };
    return Math.floor(albumArtSize * multipliers[size]);
}

function getVisualizerColor(
    settings: VisualizerSettings,
    accentColor: string,
    albumArtColor?: string
): string {
    switch (settings.colorMode) {
        case 'album-art':
            return albumArtColor || accentColor;
        case 'custom':
            return settings.customColor || accentColor;
        case 'accent':
        default:
            return accentColor;
    }
}

function drawCircularBars(
    ctx: CanvasRenderingContext2D,
    frequencyData: Uint8Array,
    options: {
        centerX: number;
        centerY: number;
        innerRadius: number;
        barCount: number;
        sensitivity: number;
        color: string;
    }
) {
    const { centerX, centerY, innerRadius, barCount, sensitivity, color } = options;
    const angleStep = (Math.PI * 2) / barCount;
    const maxBarHeight = 100;

    const samplesPerBar = Math.floor(frequencyData.length / barCount);

    for (let i = 0; i < barCount; i++) {
        // Average frequency for this bar
        let sum = 0;
        for (let j = 0; j < samplesPerBar; j++) {
            sum += frequencyData[i * samplesPerBar + j];
        }
        const average = sum / samplesPerBar;

        // Apply sensitivity (0-100 → 0.5-2.0 multiplier)
        const sensitivityMultiplier = 0.5 + (sensitivity / 100) * 1.5;
        const barHeight = (average / 255) * maxBarHeight * sensitivityMultiplier;

        // Calculate bar position
        const angle = i * angleStep - Math.PI / 2; // Start at top
        const x1 = centerX + Math.cos(angle) * innerRadius;
        const y1 = centerY + Math.sin(angle) * innerRadius;
        const x2 = centerX + Math.cos(angle) * (innerRadius + barHeight);
        const y2 = centerY + Math.sin(angle) * (innerRadius + barHeight);

        // Draw bar
        ctx.beginPath();
        ctx.moveTo(x1, y1);
        ctx.lineTo(x2, y2);
        ctx.strokeStyle = color;
        ctx.lineWidth = Math.max(2, (Math.PI * 2 * innerRadius) / barCount * 0.8);
        ctx.lineCap = 'round';
        ctx.stroke();
    }
}
