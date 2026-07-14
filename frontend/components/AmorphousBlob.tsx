import React, { useEffect, useRef, useState } from 'react';
import { useAudioVisualizer } from '../hooks/useAudioVisualizer';
import type { SnapStream } from '../services/snapStreamService';
import type { VisualizerSettings } from '../types';
import { extractDualColorsFromAlbumArt } from '../utils/albumArtColorExtraction';

interface AmorphousBlobProps {
    snapStream: SnapStream | null;
    settings: VisualizerSettings;
    albumArtUrl: string;
    accentColor: string;
    themeSettings: any;
    onColorChange?: (color: string) => void;
}

export const AmorphousBlob: React.FC<AmorphousBlobProps> = ({
    snapStream,
    settings,
    albumArtUrl,
    accentColor,
    themeSettings,
    onColorChange,
}) => {
    const canvasRef = useRef<HTMLCanvasElement>(null);
    const audioData = useAudioVisualizer(snapStream, settings, settings.enabled);
    // Ensure we always have a valid color with fallback
    const [blobColor, setBlobColor] = useState<string>(accentColor || '#aa5cc3');
    const pulsePhase = useRef(0);
    const bassMultiplierRef = useRef(1.0); // Track smoothed bass multiplier across frames
    const rotationAngle = useRef(0); // Track rotation angle for circular visualizers

    // Notify parent when color changes
    useEffect(() => {
        if (onColorChange) {
            onColorChange(blobColor);
        }
    }, [blobColor, onColorChange]);

    // Handle color based on visualizer theme
    useEffect(() => {
        if (settings.theme === 'smart' && albumArtUrl) {
            // Extract color from album art for smart theme
            const isDark = themeSettings.mode === 'dark' || themeSettings.mode === 'system';

            extractDualColorsFromAlbumArt(albumArtUrl, isDark, accentColor || '#aa5cc3')
                .then(colors => {
                    if (colors && colors.accentColor) {
                        setBlobColor(colors.accentColor);
                    } else {
                        // Fallback to user accent if extraction fails
                        setBlobColor(accentColor || '#aa5cc3');
                    }
                })
                .catch(error => {
                    console.error('[AmorphousBlob] Color extraction error:', error);
                    setBlobColor(accentColor || '#aa5cc3');
                });
        } else if (settings.theme === 'user') {
            // Use user's configured accent color
            setBlobColor(accentColor || '#aa5cc3');
        } else if (settings.theme === 'random') {
            // Cycle through random colors
            const randomColors = ['#ff6b6b', '#4ecdc4', '#45b7d1', '#f9ca24', '#6c5ce7', '#fd79a8'];
            const randomColor = randomColors[Math.floor(Math.random() * randomColors.length)];
            setBlobColor(randomColor);
        }
    }, [settings.theme, albumArtUrl, accentColor, themeSettings.mode]);

    // Main rendering loop
    useEffect(() => {
        const canvas = canvasRef.current;
        if (!canvas) return;

        const ctx = canvas.getContext('2d');
        if (!ctx) return;

        // Set canvas size to fill viewport
        const resizeCanvas = () => {
            canvas.width = window.innerWidth;
            canvas.height = window.innerHeight;
        };
        resizeCanvas();
        window.addEventListener('resize', resizeCanvas);

        const centerX = canvas.width / 2;
        // Center vertically between top and play button (50vh - 90px)
        const centerY = canvas.height / 2 - 90;
        const minDimension = Math.min(canvas.width, canvas.height);
        const albumArtRadius = minDimension * 0.15; // Album art takes 15% of smallest dimension
        const maxBlobRadius = minDimension * 0.35;  // Blob can extend to 35%

        let animationId: number;

        const render = () => {
            // Clear canvas
            ctx.clearRect(0, 0, canvas.width, canvas.height);

            // Determine if audio is playing
            const hasAudio = audioData && audioData.frequencyData.some(v => v > 0);

            // Calculate bar heights
            const barHeights = calculateBarHeights(audioData?.frequencyData, settings, hasAudio, bassMultiplierRef);

            // Draw visualization based on type
            const vizType = settings.type || 'circular';
            if (vizType === 'circular') {
                // Circular blob visualization
                drawAmorphousBlob(ctx, {
                    centerX,
                    centerY,
                    innerRadius: albumArtRadius,
                    maxRadius: maxBlobRadius,
                    barHeights,
                    barCount: settings.barCount,
                    color: blobColor,
                    smoothingType: settings.smoothingType,
                    idleState: settings.idleState,
                    hasAudio,
                    pulsePhase: pulsePhase.current,
                    rotationAngle: rotationAngle.current,
                });
            } else if (vizType === 'circular-bars') {
                // Circular bars visualization
                drawCircularBars(ctx, {
                    centerX,
                    centerY,
                    innerRadius: albumArtRadius,
                    maxRadius: maxBlobRadius,
                    barHeights,
                    barCount: settings.barCount,
                    color: blobColor,
                    hasAudio,
                    rotationAngle: rotationAngle.current,
                });
            } else if (vizType === 'bars') {
                // Traditional frequency bars
                drawFrequencyBars(ctx, {
                    centerX,
                    centerY,
                    barHeights,
                    barCount: settings.barCount,
                    color: blobColor,
                    maxHeight: maxBlobRadius * 1.74, // Reduced from 2.0 (13% reduction)
                    hasAudio,
                    mirror: settings.mirror,
                    invert: settings.invert,
                    taper: settings.taper,
                });
            } else if (vizType === 'waveform') {
                // Waveform visualization (mirrored horizontally)
                drawWaveform(ctx, {
                    centerX,
                    centerY,
                    barHeights,
                    barCount: settings.barCount,
                    color: blobColor,
                    width: Math.min(canvas.width * 0.9, 1400), // Increased from 0.8/1200
                    maxHeight: maxBlobRadius * 1.3, // Reduced from 1.5 (13% reduction)
                    hasAudio,
                    mirror: settings.mirror,
                    invert: settings.invert,
                    taper: settings.taper,
                });
            } else if (vizType === 'mixed') {
                // Mixed spectrum: bars on one half, waveform on the other
                drawMixedSpectrum(ctx, {
                    centerX,
                    centerY,
                    barHeights,
                    barCount: settings.barCount,
                    color: blobColor,
                    width: Math.min(canvas.width * 0.9, 1400),
                    maxHeight: maxBlobRadius * 1.3,
                    hasAudio,
                    mirror: settings.mirror,
                    invert: settings.invert,
                    taper: settings.taper,
                    flip: settings.mixedFlip,
                });
            }

            // Draw album art (circular)
            if (albumArtUrl) {
                drawCircularAlbumArt(ctx, albumArtUrl, centerX, centerY, albumArtRadius);
            }

            // Update pulse phase for idle animation
            pulsePhase.current += 0.02;

            // Update rotation angle for circular visualizers
            if (settings.rotate && (vizType === 'circular' || vizType === 'circular-bars')) {
                // Map rotationSpeed (0-100) to degrees per frame (0-5 degrees)
                const degreesPerFrame = (settings.rotationSpeed / 100) * 5;
                const direction = settings.rotationDirection === 'clockwise' ? 1 : -1;
                rotationAngle.current += degreesPerFrame * direction;
                // Keep angle in 0-360 range
                if (rotationAngle.current >= 360) rotationAngle.current -= 360;
                if (rotationAngle.current < 0) rotationAngle.current += 360;
            }

            animationId = requestAnimationFrame(render);
        };

        render();

        return () => {
            cancelAnimationFrame(animationId);
            window.removeEventListener('resize', resizeCanvas);
        };
    }, [audioData, settings, blobColor, albumArtUrl]);

    return (
        <canvas
            ref={canvasRef}
            className="absolute inset-0 w-full h-full pointer-events-none"
            style={{ zIndex: 0 }}
        />
    );
};

// Calculate bar heights from frequency data
function calculateBarHeights(
    frequencyData: Uint8Array | undefined,
    settings: VisualizerSettings,
    hasAudio: boolean,
    bassMultiplierRef: React.MutableRefObject<number>
): number[] {
    const barCount = settings.barCount;
    const symmetry = settings.symmetry || 1;
    const frequencyScale = settings.frequencyScale || 'logarithmic-smooth';
    const barHeights = new Array(barCount).fill(0);

    if (!hasAudio || !frequencyData) {
        // When no audio, decay bass multiplier back to 1.0
        const decayRate = 0.15; // Fast decay when silent
        bassMultiplierRef.current = bassMultiplierRef.current * (1 - decayRate) + 1.0 * decayRate;
        return barHeights;
    }

    // Define frequency range to visualize (musical range: ~80Hz - 6kHz)
    // This trims out sub-bass and high frequencies that cause spikes/dropoffs
    const startBinRatio = 0.004; // Start at 0.4% of spectrum (~80Hz)
    const endBinRatio = 0.27;    // End at 27% of spectrum (~6kHz) - core musical range

    const startBin = Math.floor(frequencyData.length * startBinRatio);
    const endBin = Math.floor(frequencyData.length * endBinRatio);
    const usableRange = endBin - startBin;

    // Calculate bass energy (20Hz - 80Hz range) for heavy bass punch detection
    // Narrower range focuses on kick drum fundamentals and sub-bass hits
    const bassEndBin = Math.floor(frequencyData.length * 0.004); // ~80Hz
    let bassSum = 0;
    for (let i = 0; i < bassEndBin; i++) {
        bassSum += frequencyData[i];
    }
    const bassAverage = bassSum / bassEndBin;

    // Convert bass to target multiplier: 0.3 (no bass) to 2.5 (heavy bass)
    // Wider range for more dramatic punch
    const targetMultiplier = 0.3 + (bassAverage / 255) * 2.2;

    // Apply attack/decay smoothing for punchy bass response
    // Fast attack (bass hits punch immediately), faster decay (bass cools down quickly for next hit)
    const attackRate = 0.7;   // Very fast attack - bass hits punch immediately
    const decayRate = 0.15;   // Faster decay - bass cools down quickly for punchier feel

    const previousMultiplier = bassMultiplierRef.current;
    let bassMultiplier: number;

    if (targetMultiplier > previousMultiplier) {
        // Attack: Bass is increasing - respond quickly
        bassMultiplier = previousMultiplier * (1 - attackRate) + targetMultiplier * attackRate;
    } else {
        // Decay: Bass is decreasing - cool down more slowly
        bassMultiplier = previousMultiplier * (1 - decayRate) + targetMultiplier * decayRate;
    }

    // Store smoothed value for next frame
    bassMultiplierRef.current = bassMultiplier;

    // Calculate unique bars based on symmetry
    const uniqueBarCount = Math.floor(barCount / symmetry);

    // Calculate unique bar heights based on frequency scale mode
    const uniqueHeights = new Array(uniqueBarCount).fill(0);

    // Adjust bass multiplier range for proportional scaling
    // Map bassMultiplier (0.3-2.5) to different ranges:
    // - For bars/wave: More dramatic (0.5x to 2.2x)
    // - For circular: More subdued (0.85x to 1.3x) - less growth but maintains punch
    const scaledBassMultiplierDramatic = 0.5 + (bassMultiplier - 0.3) * 0.77;
    const scaledBassMultiplierSubdued = 0.85 + (bassMultiplier - 0.3) * 0.20;

    if (frequencyScale === 'linear') {
        // LINEAR: Simple linear distribution across frequency range
        const samplesPerBar = usableRange / uniqueBarCount;
        for (let i = 0; i < uniqueBarCount; i++) {
            let sum = 0;
            const barStartIndex = startBin + Math.floor(i * samplesPerBar);
            const barEndIndex = startBin + Math.floor((i + 1) * samplesPerBar);
            const barSamples = barEndIndex - barStartIndex;

            for (let j = 0; j < barSamples; j++) {
                const index = barStartIndex + j;
                if (index < endBin && index < frequencyData.length) {
                    sum += frequencyData[index];
                }
            }
            const average = barSamples > 0 ? sum / barSamples : 0;
            const sensitivityMultiplier = 0.5 + (settings.sensitivity / 100) * 1.5;

            // Frequency determines shape, bass scales it proportionally
            // Use subdued multiplier for more controlled growth (primarily for circular)
            uniqueHeights[i] = (average / 255) * sensitivityMultiplier * scaledBassMultiplierSubdued;
        }
    } else if (frequencyScale === 'logarithmic' || frequencyScale === 'logarithmic-smooth') {
        // LOGARITHMIC: Natural hearing distribution (more bass detail)
        for (let i = 0; i < uniqueBarCount; i++) {
            const logStart = Math.log(startBin + 1);
            const logEnd = Math.log(endBin + 1);
            const logRange = logEnd - logStart;

            const t1 = i / uniqueBarCount;
            const t2 = (i + 1) / uniqueBarCount;
            const barStartIndex = Math.floor(Math.exp(logStart + logRange * t1)) - 1;
            const barEndIndex = Math.floor(Math.exp(logStart + logRange * t2)) - 1;

            let sum = 0;
            let count = 0;
            for (let j = barStartIndex; j < barEndIndex && j < frequencyData.length; j++) {
                sum += frequencyData[j];
                count++;
            }
            const average = count > 0 ? sum / count : 0;
            const sensitivityMultiplier = 0.5 + (settings.sensitivity / 100) * 1.5;

            // Frequency determines shape, bass scales it proportionally
            // Use subdued multiplier for more controlled growth (primarily for circular)
            uniqueHeights[i] = (average / 255) * sensitivityMultiplier * scaledBassMultiplierSubdued;
        }

        // LOGARITHMIC-SMOOTH: Add multi-pass smoothing for organic blob
        if (frequencyScale === 'logarithmic-smooth') {
            const smoothPasses = 3;
            for (let pass = 0; pass < smoothPasses; pass++) {
                const temp = [...uniqueHeights];
                for (let i = 0; i < uniqueBarCount; i++) {
                    const prev = temp[(i - 1 + uniqueBarCount) % uniqueBarCount];
                    const curr = temp[i];
                    const next = temp[(i + 1) % uniqueBarCount];
                    uniqueHeights[i] = prev * 0.25 + curr * 0.5 + next * 0.25;
                }
            }

            // Apply minimum floor for smoother appearance
            const minHeight = 0.05;
            for (let i = 0; i < uniqueBarCount; i++) {
                uniqueHeights[i] = Math.max(minHeight, uniqueHeights[i]);
            }
        }
    }

    // Apply mirror/invert to unique heights (for circular visualizers)
    if (settings.mirror) {
        const half = Math.floor(uniqueBarCount / 2);
        const firstHalf = uniqueHeights.slice(0, half);

        if (settings.invert) {
            // Invert: lows in center, highs on edges
            // Reverse the first half, then mirror it
            const reversed = firstHalf.slice().reverse();
            const mirrored = [...reversed, ...reversed.slice().reverse()];
            // Pad or trim to match original length
            for (let i = 0; i < uniqueBarCount; i++) {
                uniqueHeights[i] = mirrored[i % mirrored.length];
            }
        } else {
            // Normal mirror: highs in center, lows on edges
            const mirrored = [...firstHalf, ...firstHalf.slice().reverse()];
            // Pad or trim to match original length
            for (let i = 0; i < uniqueBarCount; i++) {
                uniqueHeights[i] = mirrored[i % mirrored.length];
            }
        }
    }

    // Repeat the unique heights around the circle based on symmetry
    // Pattern repeats 'symmetry' times around the circle with smooth interpolation
    for (let i = 0; i < barCount; i++) {
        const sectionSize = barCount / symmetry; // How many bars per symmetry section
        const sectionProgress = (i % sectionSize) / sectionSize; // 0 to 1 within current section
        const position = sectionProgress * uniqueBarCount; // Map to unique pattern space

        const index = Math.floor(position) % uniqueBarCount;
        const nextIndex = (index + 1) % uniqueBarCount;
        const fraction = position - Math.floor(position);

        // Interpolate between adjacent unique heights for smoother pattern
        barHeights[i] = uniqueHeights[index] * (1 - fraction) + uniqueHeights[nextIndex] * fraction;
    }

    return barHeights;
}

// Draw the amorphous blob shape
function drawAmorphousBlob(
    ctx: CanvasRenderingContext2D,
    options: {
        centerX: number;
        centerY: number;
        innerRadius: number;
        maxRadius: number;
        barHeights: number[];
        barCount: number;
        color: string;
        smoothingType: 'catmull-rom' | 'bezier' | 'simple';
        idleState: 'circle' | 'pulse' | 'nothing';
        hasAudio: boolean;
        pulsePhase: number;
        rotationAngle: number;
    }
) {
    const {
        centerX,
        centerY,
        innerRadius,
        maxRadius,
        barHeights,
        barCount,
        color,
        smoothingType,
        idleState,
        hasAudio,
        pulsePhase,
        rotationAngle,
    } = options;

    // Handle idle state
    if (!hasAudio) {
        if (idleState === 'nothing') {
            return; // Don't draw anything
        }

        if (idleState === 'circle') {
            // Draw perfect circle
            const validColor = color && color.trim() !== '' ? color : '#aa5cc3';
            ctx.beginPath();
            ctx.arc(centerX, centerY, innerRadius, 0, Math.PI * 2);
            ctx.strokeStyle = validColor;
            ctx.lineWidth = 2;
            ctx.stroke();
            return;
        }

        if (idleState === 'pulse') {
            // Draw pulsing circle
            const validColor = color && color.trim() !== '' ? color : '#aa5cc3';
            const pulseRadius = innerRadius + Math.sin(pulsePhase) * 5;
            ctx.beginPath();
            ctx.arc(centerX, centerY, pulseRadius, 0, Math.PI * 2);
            ctx.fillStyle = validColor + '40'; // 25% opacity
            ctx.fill();
            ctx.strokeStyle = validColor;
            ctx.lineWidth = 2;
            ctx.stroke();
            return;
        }
    }

    // Calculate points around the circle
    const points: { x: number; y: number }[] = [];
    const angleStep = (Math.PI * 2) / barCount;
    const rotationRadians = (rotationAngle * Math.PI) / 180; // Convert degrees to radians

    // Reduce dynamic range by 20% for more controlled circular visualization
    const dynamicRange = (maxRadius - innerRadius) * 0.8;

    for (let i = 0; i < barCount; i++) {
        const angle = i * angleStep - Math.PI / 2 + rotationRadians; // Start at top + rotation
        const barHeight = barHeights[i];
        const radius = innerRadius + barHeight * dynamicRange;

        const x = centerX + Math.cos(angle) * radius;
        const y = centerY + Math.sin(angle) * radius;
        points.push({ x, y });
    }

    // Close the loop
    points.push(points[0]);

    // Draw the blob using selected smoothing type
    ctx.beginPath();

    if (smoothingType === 'catmull-rom') {
        drawCatmullRomSpline(ctx, points);
    } else if (smoothingType === 'bezier') {
        drawBezierCurve(ctx, points);
    } else {
        drawSimpleInterpolation(ctx, points);
    }

    ctx.closePath();

    // Ensure color is valid
    const validColor = color && color.trim() !== '' ? color : '#aa5cc3';

    // Fill the blob
    const gradient = ctx.createRadialGradient(centerX, centerY, innerRadius, centerX, centerY, maxRadius);
    gradient.addColorStop(0, validColor + '60'); // 38% opacity at center
    gradient.addColorStop(1, validColor + 'A0'); // 63% opacity at edge
    ctx.fillStyle = gradient;
    ctx.fill();

    // Stroke the outline
    ctx.strokeStyle = validColor;
    ctx.lineWidth = 2;
    ctx.stroke();
}

// Catmull-Rom spline interpolation
function drawCatmullRomSpline(ctx: CanvasRenderingContext2D, points: { x: number; y: number }[]) {
    if (points.length < 2) return;

    ctx.moveTo(points[0].x, points[0].y);

    for (let i = 0; i < points.length - 1; i++) {
        const p0 = points[i === 0 ? points.length - 2 : i - 1];
        const p1 = points[i];
        const p2 = points[i + 1];
        const p3 = points[i + 2] || points[1];

        // Catmull-Rom to Bezier conversion
        const cp1x = p1.x + (p2.x - p0.x) / 6;
        const cp1y = p1.y + (p2.y - p0.y) / 6;
        const cp2x = p2.x - (p3.x - p1.x) / 6;
        const cp2y = p2.y - (p3.y - p1.y) / 6;

        ctx.bezierCurveTo(cp1x, cp1y, cp2x, cp2y, p2.x, p2.y);
    }
}

// Bezier curve interpolation
function drawBezierCurve(ctx: CanvasRenderingContext2D, points: { x: number; y: number }[]) {
    if (points.length < 2) return;

    ctx.moveTo(points[0].x, points[0].y);

    for (let i = 0; i < points.length - 1; i++) {
        const p1 = points[i];
        const p2 = points[i + 1];
        const midX = (p1.x + p2.x) / 2;
        const midY = (p1.y + p2.y) / 2;

        ctx.quadraticCurveTo(p1.x, p1.y, midX, midY);
    }
}

// Simple interpolation (straight lines with slight rounding)
function drawSimpleInterpolation(ctx: CanvasRenderingContext2D, points: { x: number; y: number }[]) {
    if (points.length < 2) return;

    ctx.moveTo(points[0].x, points[0].y);

    for (let i = 1; i < points.length; i++) {
        ctx.lineTo(points[i].x, points[i].y);
    }
}

// Draw circular bars radiating outward from center
function drawCircularBars(
    ctx: CanvasRenderingContext2D,
    options: {
        centerX: number;
        centerY: number;
        innerRadius: number;
        maxRadius: number;
        barHeights: number[];
        barCount: number;
        color: string;
        hasAudio: boolean;
        rotationAngle: number;
    }
) {
    const {
        centerX,
        centerY,
        innerRadius,
        maxRadius,
        barHeights,
        barCount,
        color,
        hasAudio,
        rotationAngle,
    } = options;

    if (!hasAudio) {
        // Draw idle circle
        const validColor = color && color.trim() !== '' ? color : '#aa5cc3';
        ctx.beginPath();
        ctx.arc(centerX, centerY, innerRadius, 0, Math.PI * 2);
        ctx.strokeStyle = validColor;
        ctx.lineWidth = 2;
        ctx.stroke();
        return;
    }

    const validColor = color && color.trim() !== '' ? color : '#aa5cc3';
    const angleStep = (Math.PI * 2) / barCount;
    const rotationRadians = (rotationAngle * Math.PI) / 180;

    // Full dynamic range like bars visualization (no reduction)
    const dynamicRange = maxRadius - innerRadius;
    const barWidth = angleStep * 0.8; // 80% of angle step for small gaps

    for (let i = 0; i < barCount; i++) {
        const angle = i * angleStep - Math.PI / 2 + rotationRadians; // Start at top + rotation
        const barHeight = barHeights[i];
        // Apply 1.7x boost for dramatic bass punch (same as regular bars)
        const outerRadius = innerRadius + (barHeight * 1.7) * dynamicRange;

        // Create gradient for each bar (from inner to outer)
        const gradient = ctx.createRadialGradient(centerX, centerY, innerRadius, centerX, centerY, outerRadius);
        gradient.addColorStop(0, validColor + '40'); // 25% opacity at inner
        gradient.addColorStop(1, validColor); // Full opacity at outer

        // Draw bar as a wedge
        ctx.beginPath();
        ctx.arc(centerX, centerY, innerRadius, angle - barWidth / 2, angle + barWidth / 2);
        ctx.arc(centerX, centerY, outerRadius, angle + barWidth / 2, angle - barWidth / 2, true);
        ctx.closePath();
        ctx.fillStyle = gradient;
        ctx.fill();

        // Draw outline
        ctx.strokeStyle = validColor;
        ctx.lineWidth = 1;
        ctx.stroke();
    }
}

// Draw circular album art
const albumArtCache = new Map<string, HTMLImageElement | null>();

function drawCircularAlbumArt(
    ctx: CanvasRenderingContext2D,
    url: string,
    centerX: number,
    centerY: number,
    radius: number
) {
    // Validate URL before attempting to load
    if (!url || typeof url !== 'string' || url.trim() === '') {
        return;
    }

    // Check for malformed data URLs (common issue with corrupted metadata)
    if (url.startsWith('data:')) {
        // Validate data URL structure
        const dataUrlRegex = /^data:image\/(jpeg|jpg|png|gif|webp);base64,([A-Za-z0-9+/=]+)$/;
        if (!dataUrlRegex.test(url)) {
            console.warn('[AmorphousBlob] Invalid data URL detected, skipping album art');
            albumArtCache.set(url, null); // Cache the failure to avoid retrying
            return;
        }
    }

    let img = albumArtCache.get(url);

    // If we've cached a null (failed load), don't retry
    if (img === null) {
        return;
    }

    if (!img) {
        img = new Image();
        img.crossOrigin = 'anonymous';

        img.onload = () => {
            // Image will be drawn on next frame
        };

        img.onerror = (error) => {
            console.warn('[AmorphousBlob] Failed to load album art:', error);
            // Cache null to prevent retrying this URL
            albumArtCache.set(url, null);
        };

        img.src = url;
        albumArtCache.set(url, img);

        return; // Skip this frame, wait for load
    }

    // Check if image loaded successfully before drawing
    if (!img.complete || img.naturalWidth === 0) {
        return;
    }

    try {
        // Create circular clip path
        ctx.save();
        ctx.beginPath();
        ctx.arc(centerX, centerY, radius, 0, Math.PI * 2);
        ctx.closePath();
        ctx.clip();

        // Draw image centered and cropped to fill circle
        const imgSize = radius * 2;
        ctx.drawImage(img, centerX - radius, centerY - radius, imgSize, imgSize);

        ctx.restore();

        // Draw circle outline
        ctx.beginPath();
        ctx.arc(centerX, centerY, radius, 0, Math.PI * 2);
        ctx.strokeStyle = 'rgba(255, 255, 255, 0.2)';
        ctx.lineWidth = 2;
        ctx.stroke();
    } catch (error) {
        console.error('[AmorphousBlob] Error drawing album art:', error);
        // Remove from cache so it can be retried
        albumArtCache.delete(url);
        ctx.restore(); // Ensure we restore context state even on error
    }
}

// Draw traditional frequency bars visualization
function drawFrequencyBars(
    ctx: CanvasRenderingContext2D,
    options: {
        centerX: number;
        centerY: number;
        barHeights: number[];
        barCount: number;
        color: string;
        maxHeight: number;
        hasAudio: boolean;
        mirror: boolean;
        invert: boolean;
        taper: boolean;
    }
) {
    const { centerX, centerY, barHeights, barCount, color, maxHeight, hasAudio, mirror, invert, taper } = options;

    if (!hasAudio) {
        return; // Don't draw bars when idle
    }

    const validColor = color && color.trim() !== '' ? color : '#aa5cc3';
    const totalWidth = Math.min(900, centerX * 1.8); // Increased from 800/1.6 to fill more width

    // Define minimum baseline height (8% of max) - reduced to preserve dynamic range
    const minBaselineHeight = maxHeight * 0.08;

    // Process bar heights for mirror/taper
    let processedHeights = [...barHeights];

    if (mirror) {
        const half = Math.floor(barCount / 2);
        const firstHalf = processedHeights.slice(0, half);

        if (invert) {
            // Invert: lows in center, highs on edges
            // Reverse the first half, then mirror it
            const reversed = firstHalf.slice().reverse();
            processedHeights = [...reversed, ...reversed.slice().reverse()];
        } else {
            // Normal mirror: highs in center, lows on edges
            processedHeights = [...firstHalf, ...firstHalf.slice().reverse()];
        }
    }

    const barWidth = totalWidth / barCount;
    const barGap = barWidth * 0.2; // 20% gap between bars
    const actualBarWidth = barWidth - barGap;
    const startX = centerX - totalWidth / 2;

    for (let i = 0; i < barCount; i++) {
        // Calculate dynamic height (0-100% from frequency + bass)
        // Apply 1.7x boost to restore dramatic bass punch (heights use subdued multiplier)
        let dynamicHeight = processedHeights[i] * maxHeight * 1.7;

        // Apply taper at edges to dynamic portion
        if (taper) {
            const edgeDistance = Math.min(i, barCount - 1 - i); // Distance from nearest edge
            const taperZone = Math.floor(barCount * 0.1); // Taper in outer 10%
            if (edgeDistance < taperZone) {
                const taperFactor = edgeDistance / taperZone;
                dynamicHeight *= taperFactor;
            }
        }

        // Add minimum baseline to ensure bars are always visible
        const height = minBaselineHeight + dynamicHeight;

        const x = startX + i * barWidth;
        const y = centerY - height / 2; // Center bars vertically

        // Create gradient for each bar
        const gradient = ctx.createLinearGradient(x, y + height, x, y);
        gradient.addColorStop(0, validColor + '40'); // 25% opacity at bottom
        gradient.addColorStop(1, validColor); // Full opacity at top

        ctx.fillStyle = gradient;
        ctx.fillRect(x, y, actualBarWidth, height);

        // Draw outline
        ctx.strokeStyle = validColor;
        ctx.lineWidth = 1;
        ctx.strokeRect(x, y, actualBarWidth, height);
    }
}

// Draw spectrum analyzer visualization
function drawSpectrum(
    ctx: CanvasRenderingContext2D,
    options: {
        centerX: number;
        centerY: number;
        barHeights: number[];
        barCount: number;
        color: string;
        width: number;
        maxHeight: number;
        hasAudio: boolean;
        mirror: boolean;
        taper: boolean;
    }
) {
    const { centerX, centerY, barHeights, barCount, color, width, maxHeight, hasAudio, mirror, taper } = options;

    if (!hasAudio) {
        return; // Don't draw spectrum when idle
    }

    const validColor = color && color.trim() !== '' ? color : '#aa5cc3';

    // Define minimum baseline height (5% of max for spectrum) - reduced to preserve dynamic range
    const minBaselineHeight = maxHeight * 0.05;

    // Process bar heights for mirror/taper
    let processedHeights = [...barHeights];

    if (mirror) {
        // Mirror: highs in center, lows on edges
        const half = Math.floor(barCount / 2);
        const firstHalf = processedHeights.slice(0, half);
        processedHeights = [...firstHalf, ...firstHalf.slice().reverse()];
    }

    const barWidth = width / barCount;
    const startX = centerX - width / 2;

    // Draw filled spectrum
    ctx.beginPath();
    ctx.moveTo(startX, centerY);

    // Draw top edge
    for (let i = 0; i < barCount; i++) {
        // Apply 1.7x boost to restore dramatic bass punch (heights use subdued multiplier)
        let dynamicHeight = processedHeights[i] * maxHeight * 1.7;

        // Apply taper at edges to dynamic portion
        if (taper) {
            const edgeDistance = Math.min(i, barCount - 1 - i);
            const taperZone = Math.floor(barCount * 0.1);
            if (edgeDistance < taperZone) {
                const taperFactor = edgeDistance / taperZone;
                dynamicHeight *= taperFactor;
            }
        }

        // Add minimum baseline
        const height = minBaselineHeight + dynamicHeight;

        const x = startX + i * barWidth;
        const y = centerY - height;

        if (i === 0) {
            ctx.lineTo(x, y);
        } else {
            // Smooth curve through points
            const prevI = i - 1;
            let prevDynamicHeight = processedHeights[prevI] * maxHeight * 1.7;
            if (taper) {
                const prevEdgeDistance = Math.min(prevI, barCount - 1 - prevI);
                const taperZone = Math.floor(barCount * 0.1);
                if (prevEdgeDistance < taperZone) {
                    prevDynamicHeight *= prevEdgeDistance / taperZone;
                }
            }
            const prevHeight = minBaselineHeight + prevDynamicHeight;
            const prevX = startX + prevI * barWidth;
            const prevY = centerY - prevHeight;
            const midX = (prevX + x) / 2;
            const midY = (prevY + y) / 2;
            ctx.quadraticCurveTo(prevX, prevY, midX, midY);
        }
    }

    // Complete the shape
    ctx.lineTo(startX + width, centerY);
    ctx.lineTo(startX, centerY);
    ctx.closePath();

    // Fill with gradient
    const gradient = ctx.createLinearGradient(0, centerY - (minBaselineHeight + maxHeight), 0, centerY);
    gradient.addColorStop(0, validColor); // Full opacity at top
    gradient.addColorStop(1, validColor + '20'); // 12% opacity at bottom
    ctx.fillStyle = gradient;
    ctx.fill();

    // Draw outline
    ctx.beginPath();
    ctx.moveTo(startX, centerY);
    for (let i = 0; i < barCount; i++) {
        let dynamicHeight = processedHeights[i] * maxHeight * 1.7;
        if (taper) {
            const edgeDistance = Math.min(i, barCount - 1 - i);
            const taperZone = Math.floor(barCount * 0.1);
            if (edgeDistance < taperZone) {
                dynamicHeight *= edgeDistance / taperZone;
            }
        }
        const height = minBaselineHeight + dynamicHeight;
        const x = startX + i * barWidth;
        const y = centerY - height;

        if (i === 0) {
            ctx.lineTo(x, y);
        } else {
            const prevI = i - 1;
            let prevDynamicHeight = processedHeights[prevI] * maxHeight * 1.7;
            if (taper) {
                const prevEdgeDistance = Math.min(prevI, barCount - 1 - prevI);
                const taperZone = Math.floor(barCount * 0.1);
                if (prevEdgeDistance < taperZone) {
                    prevDynamicHeight *= prevEdgeDistance / taperZone;
                }
            }
            const prevHeight = minBaselineHeight + prevDynamicHeight;
            const prevX = startX + prevI * barWidth;
            const prevY = centerY - prevHeight;
            const midX = (prevX + x) / 2;
            const midY = (prevY + y) / 2;
            ctx.quadraticCurveTo(prevX, prevY, midX, midY);
        }
    }
    ctx.strokeStyle = validColor;
    ctx.lineWidth = 2;
    ctx.stroke();
}

// Draw waveform visualization (mirrored horizontally)
function drawWaveform(
    ctx: CanvasRenderingContext2D,
    options: {
        centerX: number;
        centerY: number;
        barHeights: number[];
        barCount: number;
        color: string;
        width: number;
        maxHeight: number;
        hasAudio: boolean;
        mirror: boolean;
        invert: boolean;
        taper: boolean;
    }
) {
    const { centerX, centerY, barHeights, barCount, color, width, maxHeight, hasAudio, mirror, invert, taper } = options;

    if (!hasAudio) {
        return; // Don't draw waveform when idle
    }

    const validColor = color && color.trim() !== '' ? color : '#aa5cc3';

    // Define minimum baseline height (4% of max for waveform, split between top/bottom) - reduced to preserve dynamic range
    const minBaselineHeight = (maxHeight / 2) * 0.04;

    // Process bar heights for mirror/taper
    let processedHeights = [...barHeights];

    if (mirror) {
        const half = Math.floor(barCount / 2);
        const firstHalf = processedHeights.slice(0, half);

        if (invert) {
            // Invert: lows in center, highs on edges
            // Reverse the first half, then mirror it
            const reversed = firstHalf.slice().reverse();
            processedHeights = [...reversed, ...reversed.slice().reverse()];
        } else {
            // Normal mirror: highs in center, lows on edges
            processedHeights = [...firstHalf, ...firstHalf.slice().reverse()];
        }
    }

    const barWidth = width / barCount;
    const startX = centerX - width / 2;

    // Draw top waveform
    ctx.beginPath();
    ctx.moveTo(startX, centerY);

    for (let i = 0; i < barCount; i++) {
        // Half height for symmetry, with 1.7x boost for dramatic bass punch
        let dynamicHeight = processedHeights[i] * (maxHeight / 2) * 1.7;

        // Apply taper at edges to dynamic portion
        if (taper) {
            const edgeDistance = Math.min(i, barCount - 1 - i);
            const taperZone = Math.floor(barCount * 0.1);
            if (edgeDistance < taperZone) {
                dynamicHeight *= edgeDistance / taperZone;
            }
        }

        // Add minimum baseline
        const height = minBaselineHeight + dynamicHeight;

        const x = startX + i * barWidth;
        const yTop = centerY - height;

        if (i === 0) {
            ctx.lineTo(x, yTop);
        } else {
            const prevI = i - 1;
            let prevDynamicHeight = processedHeights[prevI] * (maxHeight / 2) * 1.7;
            if (taper) {
                const prevEdgeDistance = Math.min(prevI, barCount - 1 - prevI);
                const taperZone = Math.floor(barCount * 0.1);
                if (prevEdgeDistance < taperZone) {
                    prevDynamicHeight *= prevEdgeDistance / taperZone;
                }
            }
            const prevHeight = minBaselineHeight + prevDynamicHeight;
            const prevX = startX + prevI * barWidth;
            const prevYTop = centerY - prevHeight;
            const midX = (prevX + x) / 2;
            const midY = (prevYTop + yTop) / 2;
            ctx.quadraticCurveTo(prevX, prevYTop, midX, midY);
        }
    }

    // Complete the top curve by drawing to the actual last point
    const lastI = barCount - 1;
    let lastDynamicHeight = processedHeights[lastI] * (maxHeight / 2) * 1.7;
    if (taper) {
        const edgeDistance = Math.min(lastI, barCount - 1 - lastI);
        const taperZone = Math.floor(barCount * 0.1);
        if (edgeDistance < taperZone) {
            lastDynamicHeight *= edgeDistance / taperZone;
        }
    }
    const lastHeight = minBaselineHeight + lastDynamicHeight;
    const lastX = startX + lastI * barWidth;
    const lastYTop = centerY - lastHeight;
    ctx.lineTo(lastX, lastYTop);

    // Draw bottom waveform (mirrored)
    for (let i = barCount - 1; i >= 0; i--) {
        let dynamicHeight = processedHeights[i] * (maxHeight / 2) * 1.7;

        if (taper) {
            const edgeDistance = Math.min(i, barCount - 1 - i);
            const taperZone = Math.floor(barCount * 0.1);
            if (edgeDistance < taperZone) {
                dynamicHeight *= edgeDistance / taperZone;
            }
        }

        const height = minBaselineHeight + dynamicHeight;
        const x = startX + i * barWidth;
        const yBottom = centerY + height;

        if (i === barCount - 1) {
            ctx.lineTo(x, yBottom);
        } else {
            const nextI = i + 1;
            let nextDynamicHeight = processedHeights[nextI] * (maxHeight / 2) * 1.7;
            if (taper) {
                const nextEdgeDistance = Math.min(nextI, barCount - 1 - nextI);
                const taperZone = Math.floor(barCount * 0.1);
                if (nextEdgeDistance < taperZone) {
                    nextDynamicHeight *= nextEdgeDistance / taperZone;
                }
            }
            const nextHeight = minBaselineHeight + nextDynamicHeight;
            const nextX = startX + nextI * barWidth;
            const nextYBottom = centerY + nextHeight;
            const midX = (x + nextX) / 2;
            const midY = (yBottom + nextYBottom) / 2;
            ctx.quadraticCurveTo(nextX, nextYBottom, midX, midY);
        }
    }

    // Complete the bottom curve by drawing to the actual first point
    const firstI = 0;
    let firstDynamicHeight = processedHeights[firstI] * (maxHeight / 2) * 1.7;
    if (taper) {
        const edgeDistance = Math.min(firstI, barCount - 1 - firstI);
        const taperZone = Math.floor(barCount * 0.1);
        if (edgeDistance < taperZone) {
            firstDynamicHeight *= edgeDistance / taperZone;
        }
    }
    const firstHeight = minBaselineHeight + firstDynamicHeight;
    const firstX = startX + firstI * barWidth;
    const firstYBottom = centerY + firstHeight;
    ctx.lineTo(firstX, firstYBottom);

    ctx.closePath();

    // Fill with gradient (adjusted for baseline)
    const totalHeight = minBaselineHeight + (maxHeight / 2) * 1.7;
    const gradient = ctx.createLinearGradient(0, centerY - totalHeight, 0, centerY + totalHeight);
    gradient.addColorStop(0, validColor); // Full opacity at top
    gradient.addColorStop(0.5, validColor + '40'); // 25% opacity at center
    gradient.addColorStop(1, validColor); // Full opacity at bottom
    ctx.fillStyle = gradient;
    ctx.fill();

    // Draw outline (top)
    ctx.beginPath();
    // Start at first point, not center
    let outlineFirstDynamicHeight = processedHeights[0] * (maxHeight / 2) * 1.7;
    if (taper) {
        const edgeDistance = 0;
        const taperZone = Math.floor(barCount * 0.1);
        if (edgeDistance < taperZone) {
            outlineFirstDynamicHeight *= edgeDistance / taperZone;
        }
    }
    const outlineFirstHeight = minBaselineHeight + outlineFirstDynamicHeight;
    ctx.moveTo(startX, centerY - outlineFirstHeight);

    for (let i = 0; i < barCount; i++) {
        let dynamicHeight = processedHeights[i] * (maxHeight / 2) * 1.7;
        if (taper) {
            const edgeDistance = Math.min(i, barCount - 1 - i);
            const taperZone = Math.floor(barCount * 0.1);
            if (edgeDistance < taperZone) {
                dynamicHeight *= edgeDistance / taperZone;
            }
        }
        const height = minBaselineHeight + dynamicHeight;
        const x = startX + i * barWidth;
        const y = centerY - height;

        if (i === 0) {
            // Already at this point from moveTo
            continue;
        } else {
            const prevI = i - 1;
            let prevDynamicHeight = processedHeights[prevI] * (maxHeight / 2) * 1.7;
            if (taper) {
                const prevEdgeDistance = Math.min(prevI, barCount - 1 - prevI);
                const taperZone = Math.floor(barCount * 0.1);
                if (prevEdgeDistance < taperZone) {
                    prevDynamicHeight *= prevEdgeDistance / taperZone;
                }
            }
            const prevHeight = minBaselineHeight + prevDynamicHeight;
            const prevX = startX + prevI * barWidth;
            const prevY = centerY - prevHeight;
            const midX = (prevX + x) / 2;
            const midY = (prevY + y) / 2;
            ctx.quadraticCurveTo(prevX, prevY, midX, midY);
        }
    }
    // Complete top outline to the actual last point
    const outlineLastI = barCount - 1;
    let outlineLastDynamicHeight = processedHeights[outlineLastI] * (maxHeight / 2) * 1.7;
    if (taper) {
        const edgeDistance = Math.min(outlineLastI, barCount - 1 - outlineLastI);
        const taperZone = Math.floor(barCount * 0.1);
        if (edgeDistance < taperZone) {
            outlineLastDynamicHeight *= edgeDistance / taperZone;
        }
    }
    const outlineLastHeight = minBaselineHeight + outlineLastDynamicHeight;
    const outlineLastX = startX + outlineLastI * barWidth;
    const outlineLastY = centerY - outlineLastHeight;
    ctx.lineTo(outlineLastX, outlineLastY);
    ctx.strokeStyle = validColor;
    ctx.lineWidth = 2;
    ctx.stroke();

    // Draw bottom outline
    ctx.beginPath();
    // Start at first point, not center
    let outlineBottomFirstDynamicHeight = processedHeights[0] * (maxHeight / 2) * 1.7;
    if (taper) {
        const edgeDistance = 0;
        const taperZone = Math.floor(barCount * 0.1);
        if (edgeDistance < taperZone) {
            outlineBottomFirstDynamicHeight *= edgeDistance / taperZone;
        }
    }
    const outlineBottomFirstHeight = minBaselineHeight + outlineBottomFirstDynamicHeight;
    ctx.moveTo(startX, centerY + outlineBottomFirstHeight);

    for (let i = 0; i < barCount; i++) {
        let dynamicHeight = processedHeights[i] * (maxHeight / 2) * 1.7;
        if (taper) {
            const edgeDistance = Math.min(i, barCount - 1 - i);
            const taperZone = Math.floor(barCount * 0.1);
            if (edgeDistance < taperZone) {
                dynamicHeight *= edgeDistance / taperZone;
            }
        }
        const height = minBaselineHeight + dynamicHeight;
        const x = startX + i * barWidth;
        const y = centerY + height;

        if (i === 0) {
            // Already at this point from moveTo
            continue;
        } else {
            const prevI = i - 1;
            let prevDynamicHeight = processedHeights[prevI] * (maxHeight / 2) * 1.7;
            if (taper) {
                const prevEdgeDistance = Math.min(prevI, barCount - 1 - prevI);
                const taperZone = Math.floor(barCount * 0.1);
                if (prevEdgeDistance < taperZone) {
                    prevDynamicHeight *= prevEdgeDistance / taperZone;
                }
            }
            const prevHeight = minBaselineHeight + prevDynamicHeight;
            const prevX = startX + prevI * barWidth;
            const prevY = centerY + prevHeight;
            const midX = (prevX + x) / 2;
            const midY = (prevY + y) / 2;
            ctx.quadraticCurveTo(prevX, prevY, midX, midY);
        }
    }
    // Complete bottom outline to the actual last point
    const outlineBottomLastI = barCount - 1;
    let outlineBottomLastDynamicHeight = processedHeights[outlineBottomLastI] * (maxHeight / 2) * 1.7;
    if (taper) {
        const edgeDistance = Math.min(outlineBottomLastI, barCount - 1 - outlineBottomLastI);
        const taperZone = Math.floor(barCount * 0.1);
        if (edgeDistance < taperZone) {
            outlineBottomLastDynamicHeight *= edgeDistance / taperZone;
        }
    }
    const outlineBottomLastHeight = minBaselineHeight + outlineBottomLastDynamicHeight;
    const outlineBottomLastX = startX + outlineBottomLastI * barWidth;
    const outlineBottomLastY = centerY + outlineBottomLastHeight;
    ctx.lineTo(outlineBottomLastX, outlineBottomLastY);
    ctx.strokeStyle = validColor;
    ctx.lineWidth = 2;
    ctx.stroke();
}

// Draw mixed spectrum visualization (bars on one half, waveform on the other)
function drawMixedSpectrum(
    ctx: CanvasRenderingContext2D,
    options: {
        centerX: number;
        centerY: number;
        barHeights: number[];
        barCount: number;
        color: string;
        width: number;
        maxHeight: number;
        hasAudio: boolean;
        mirror: boolean;
        invert: boolean;
        taper: boolean;
        flip: boolean;
    }
) {
    const { centerX, centerY, barHeights, barCount, color, width, maxHeight, hasAudio, mirror, invert, taper, flip } = options;

    if (!hasAudio) {
        return; // Don't draw when idle
    }

    const validColor = color && color.trim() !== '' ? color : '#aa5cc3';
    const minBaselineHeight = (maxHeight / 2) * 0.04;

    // Process bar heights for mirror/taper
    let processedHeights = [...barHeights];

    if (mirror) {
        const half = Math.floor(barCount / 2);
        const firstHalf = processedHeights.slice(0, half);

        if (invert) {
            const reversed = firstHalf.slice().reverse();
            processedHeights = [...reversed, ...reversed.slice().reverse()];
        } else {
            processedHeights = [...firstHalf, ...firstHalf.slice().reverse()];
        }
    }

    const barWidth = width / barCount;
    const startX = centerX - width / 2;

    // Determine which visualization goes on top/bottom based on flip
    const drawBarsOnTop = !flip;
    const topHalfHeight = maxHeight / 2;
    const bottomHalfHeight = maxHeight / 2;

    if (drawBarsOnTop) {
        // Draw frequency bars on TOP half
        for (let i = 0; i < barCount; i++) {
            let dynamicHeight = processedHeights[i] * topHalfHeight * 1.7;

            if (taper) {
                const edgeDistance = Math.min(i, barCount - 1 - i);
                const taperZone = Math.floor(barCount * 0.1);
                if (edgeDistance < taperZone) {
                    dynamicHeight *= edgeDistance / taperZone;
                }
            }

            const height = minBaselineHeight + dynamicHeight;
            const x = startX + i * barWidth;
            const actualBarWidth = Math.max(1, barWidth * 0.8);

            // Draw bar extending upward from center
            const y = centerY - height;

            // Fill
            ctx.fillStyle = validColor + '80';
            ctx.fillRect(x, y, actualBarWidth, height);

            // Outline
            ctx.strokeStyle = validColor;
            ctx.lineWidth = 1;
            ctx.strokeRect(x, y, actualBarWidth, height);
        }

        // Draw waveform on BOTTOM half
        ctx.beginPath();
        let firstDynamicHeight = processedHeights[0] * bottomHalfHeight * 1.7;
        if (taper && 0 < Math.floor(barCount * 0.1)) {
            firstDynamicHeight *= 0;
        }
        const firstHeight = minBaselineHeight + firstDynamicHeight;
        ctx.moveTo(startX, centerY + firstHeight);

        for (let i = 0; i < barCount; i++) {
            let dynamicHeight = processedHeights[i] * bottomHalfHeight * 1.7;

            if (taper) {
                const edgeDistance = Math.min(i, barCount - 1 - i);
                const taperZone = Math.floor(barCount * 0.1);
                if (edgeDistance < taperZone) {
                    dynamicHeight *= edgeDistance / taperZone;
                }
            }

            const height = minBaselineHeight + dynamicHeight;
            const x = startX + i * barWidth;
            const y = centerY + height;

            if (i === 0) {
                continue;
            } else {
                const prevI = i - 1;
                let prevDynamicHeight = processedHeights[prevI] * bottomHalfHeight * 1.7;
                if (taper) {
                    const prevEdgeDistance = Math.min(prevI, barCount - 1 - prevI);
                    const taperZone = Math.floor(barCount * 0.1);
                    if (prevEdgeDistance < taperZone) {
                        prevDynamicHeight *= prevEdgeDistance / taperZone;
                    }
                }
                const prevHeight = minBaselineHeight + prevDynamicHeight;
                const prevX = startX + prevI * barWidth;
                const prevY = centerY + prevHeight;
                const midX = (prevX + x) / 2;
                const midY = (prevY + y) / 2;
                ctx.quadraticCurveTo(prevX, prevY, midX, midY);
            }
        }

        const lastI = barCount - 1;
        let lastDynamicHeight = processedHeights[lastI] * bottomHalfHeight * 1.7;
        if (taper) {
            const edgeDistance = Math.min(lastI, barCount - 1 - lastI);
            const taperZone = Math.floor(barCount * 0.1);
            if (edgeDistance < taperZone) {
                lastDynamicHeight *= edgeDistance / taperZone;
            }
        }
        const lastHeight = minBaselineHeight + lastDynamicHeight;
        const lastX = startX + lastI * barWidth;
        const lastY = centerY + lastHeight;
        ctx.lineTo(lastX, lastY);

        ctx.strokeStyle = validColor;
        ctx.lineWidth = 2;
        ctx.stroke();

    } else {
        // Draw waveform on TOP half
        ctx.beginPath();
        let firstDynamicHeight = processedHeights[0] * topHalfHeight * 1.7;
        if (taper && 0 < Math.floor(barCount * 0.1)) {
            firstDynamicHeight *= 0;
        }
        const firstHeight = minBaselineHeight + firstDynamicHeight;
        ctx.moveTo(startX, centerY - firstHeight);

        for (let i = 0; i < barCount; i++) {
            let dynamicHeight = processedHeights[i] * topHalfHeight * 1.7;

            if (taper) {
                const edgeDistance = Math.min(i, barCount - 1 - i);
                const taperZone = Math.floor(barCount * 0.1);
                if (edgeDistance < taperZone) {
                    dynamicHeight *= edgeDistance / taperZone;
                }
            }

            const height = minBaselineHeight + dynamicHeight;
            const x = startX + i * barWidth;
            const y = centerY - height;

            if (i === 0) {
                continue;
            } else {
                const prevI = i - 1;
                let prevDynamicHeight = processedHeights[prevI] * topHalfHeight * 1.7;
                if (taper) {
                    const prevEdgeDistance = Math.min(prevI, barCount - 1 - prevI);
                    const taperZone = Math.floor(barCount * 0.1);
                    if (prevEdgeDistance < taperZone) {
                        prevDynamicHeight *= prevEdgeDistance / taperZone;
                    }
                }
                const prevHeight = minBaselineHeight + prevDynamicHeight;
                const prevX = startX + prevI * barWidth;
                const prevY = centerY - prevHeight;
                const midX = (prevX + x) / 2;
                const midY = (prevY + y) / 2;
                ctx.quadraticCurveTo(prevX, prevY, midX, midY);
            }
        }

        const lastI = barCount - 1;
        let lastDynamicHeight = processedHeights[lastI] * topHalfHeight * 1.7;
        if (taper) {
            const edgeDistance = Math.min(lastI, barCount - 1 - lastI);
            const taperZone = Math.floor(barCount * 0.1);
            if (edgeDistance < taperZone) {
                lastDynamicHeight *= edgeDistance / taperZone;
            }
        }
        const lastHeight = minBaselineHeight + lastDynamicHeight;
        const lastX = startX + lastI * barWidth;
        const lastY = centerY - lastHeight;
        ctx.lineTo(lastX, lastY);

        ctx.strokeStyle = validColor;
        ctx.lineWidth = 2;
        ctx.stroke();

        // Draw frequency bars on BOTTOM half
        for (let i = 0; i < barCount; i++) {
            let dynamicHeight = processedHeights[i] * bottomHalfHeight * 1.7;

            if (taper) {
                const edgeDistance = Math.min(i, barCount - 1 - i);
                const taperZone = Math.floor(barCount * 0.1);
                if (edgeDistance < taperZone) {
                    dynamicHeight *= edgeDistance / taperZone;
                }
            }

            const height = minBaselineHeight + dynamicHeight;
            const x = startX + i * barWidth;
            const actualBarWidth = Math.max(1, barWidth * 0.8);

            // Draw bar extending downward from center
            const y = centerY;

            // Fill
            ctx.fillStyle = validColor + '80';
            ctx.fillRect(x, y, actualBarWidth, height);

            // Outline
            ctx.strokeStyle = validColor;
            ctx.lineWidth = 1;
            ctx.strokeRect(x, y, actualBarWidth, height);
        }
    }
}
