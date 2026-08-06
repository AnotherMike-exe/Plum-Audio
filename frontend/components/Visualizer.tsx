import React, { useEffect, useState } from 'react';
import type { Settings, Stream, VisualizerSettings } from '../types';
import { AmorphousBlob } from './AmorphousBlob';
import { StreamSelector } from './StreamSelector';
import { Icon } from './Icon';
import { formatTime } from '../utils/time';
import type { VizFrame } from '../services/sendspinControllerClient';

interface DualColorExtractionResult {
    backgroundColor: string;
    accentColor: string;
    isDarkTheme: boolean;
    contrastRatio: number;
}

interface VisualizerProps {
    stream: Stream | null;
    streams: Stream[];
    settings: Settings;
    getSpectrum: (() => VizFrame | null) | null;
    browserAudioMuted: boolean;
    extractedAlbumArtColors: DualColorExtractionResult | null;
    onPlayPause: () => void;
    onSkip: (direction: 'previous' | 'next') => void;
    onVolumeChange: (volume: number) => void;
    onStreamChange: (streamId: string | null) => void;
    onOpenSettings: () => void;
    onOpenVisualizerSettings: () => void;
    onStartBrowserAudio: () => void;
    onToggleBrowserAudioMute: () => void;
    onClose: () => void;
    currentVolume: number;
    isOpen: boolean;
}

export const Visualizer: React.FC<VisualizerProps> = ({
    stream,
    streams,
    settings,
    getSpectrum,
    browserAudioMuted,
    extractedAlbumArtColors,
    onPlayPause,
    onSkip,
    onVolumeChange,
    onStreamChange,
    onOpenSettings,
    onOpenVisualizerSettings,
    onStartBrowserAudio,
    onToggleBrowserAudioMute,
    onClose,
    currentVolume,
    isOpen,
}) => {
    const [isFullscreen, setIsFullscreen] = useState(false);
    const [visualizerColor, setVisualizerColor] = useState<string>('#aa5cc3');

    // Handle both legacy boolean and new object visualizer settings
    const visualizerSettings: VisualizerSettings = typeof settings.integrations.visualizer === 'object'
        ? {
            ...settings.integrations.visualizer,
            symmetry: settings.integrations.visualizer.symmetry || 1,
            frequencyScale: settings.integrations.visualizer.frequencyScale || 'logarithmic-smooth',
            mirror: settings.integrations.visualizer.mirror ?? false,
            invert: settings.integrations.visualizer.invert ?? false,
            taper: settings.integrations.visualizer.taper ?? true,
            mixedFlip: settings.integrations.visualizer.mixedFlip ?? false,
            rotate: settings.integrations.visualizer.rotate ?? false,
            rotationSpeed: settings.integrations.visualizer.rotationSpeed ?? 30,
            rotationDirection: settings.integrations.visualizer.rotationDirection || 'clockwise',
        }
        : {
            enabled: typeof settings.integrations.visualizer === 'boolean' ? settings.integrations.visualizer : false,
            // 'user', to agree with types.ts DEFAULT_VISUALIZER_SETTINGS — which is what
            // VisualizerTab.getVisualizerSettings() expands the same legacy boolean against. This said
            // 'smart', so a unit on the boolean shape RENDERED album-art colours while the Settings tab
            // showed "user" selected: the tab reported a value that was not in effect. Latent while the
            // boolean only came from legacy files; live on every unit now that the backend defaults the
            // visualizer ON as a bare `true`.
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
                particles: false,
            }
        };

    // Handle keyboard shortcuts
    useEffect(() => {
        if (!isOpen) return;

        const handleKeyPress = (e: KeyboardEvent) => {
            switch (e.key) {
                case 'Escape':
                    if (isFullscreen) {
                        document.exitFullscreen();
                    } else {
                        onClose();
                    }
                    break;
                case ' ':
                    e.preventDefault();
                    onPlayPause();
                    break;
                case 'ArrowUp':
                    e.preventDefault();
                    onVolumeChange(Math.min(100, currentVolume + 5));
                    break;
                case 'ArrowDown':
                    e.preventDefault();
                    onVolumeChange(Math.max(0, currentVolume - 5));
                    break;
            }
        };

        window.addEventListener('keydown', handleKeyPress);
        return () => window.removeEventListener('keydown', handleKeyPress);
    }, [isOpen, isFullscreen, onClose, onPlayPause, onVolumeChange, currentVolume]);

    // Handle fullscreen changes
    useEffect(() => {
        const handleFullscreenChange = () => {
            setIsFullscreen(!!document.fullscreenElement);
        };

        document.addEventListener('fullscreenchange', handleFullscreenChange);
        return () => document.removeEventListener('fullscreenchange', handleFullscreenChange);
    }, []);

    const toggleFullscreen = () => {
        if (!document.fullscreenElement) {
            document.documentElement.requestFullscreen();
        } else {
            document.exitFullscreen();
        }
    };

    const currentTrack = stream?.currentTrack;
    const albumArtUrl = currentTrack?.albumArtUrl || '';
    const progress = stream?.progress || 0;
    const duration = currentTrack?.duration || 0;

    // Get the accent color for the visualizer
    // Priority: Extracted album art colors (if enabled) > User-selected colors
    const getUserAccentColor = (): string => {
        // Use extracted album art accent color if enabled and available
        if (settings.theme.useAlbumArtColors && extractedAlbumArtColors) {
            return extractedAlbumArtColors.accentColor;
        }

        // Fall back to custom color if set
        if (settings.theme.accent === 'custom' && settings.theme.customColor) {
            return settings.theme.customColor;
        }

        // Default accent colors
        const accentColors: Record<string, string> = {
            purple: '#aa5cc3',
            blue: '#3b82f6',
            green: '#22c55e',
            orange: '#f97316',
            red: '#ef4444',
            yellow: '#eab308'
        };

        return accentColors[settings.theme.accent] || '#aa5cc3';
    };

    const accentColor = getUserAccentColor();
    const isBrowserAudioActive = true; // native visualizer role, not browser audio — always ready

    // Calculate progress percentage for the ring (match main GUI calculation)
    const progressPercent = duration > 0
        ? Math.min(100, Math.max(0, (progress / duration) * 100))
        : 0;

    // Volume slider style to match main GUI
    // Adjust gradient to account for thumb width (1rem = 16px) on 240px slider
    const thumbSize = 16; // 1rem in pixels
    const sliderWidth = 240;
    const offsetPercent = (thumbSize / sliderWidth / 2) * 100; // 3.33%
    const rangePercent = (1 - thumbSize / sliderWidth) * 100; // 93.33%
    const adjustedVolumePercent = offsetPercent + (currentVolume / 100) * rangePercent;
    const volumeSliderStyle = {
        background: `linear-gradient(to right, var(--accent-color) ${adjustedVolumePercent}%, var(--border-color) ${adjustedVolumePercent}%)`
    };

    if (!isOpen) return null;

    const handleBackgroundClick = (e: React.MouseEvent<HTMLDivElement>) => {
        // Only close if clicking the background itself, not child elements
        if (e.target === e.currentTarget) {
            onClose();
        }
    };

    return (
        <div
            className="fixed inset-0 bg-[var(--bg-primary)] z-50 animate-fadeIn cursor-pointer"
            onClick={handleBackgroundClick}
        >
            {/* Browser Audio Starting Message */}
            {!isBrowserAudioActive && (
                <div className="absolute inset-0 flex items-center justify-center z-10 bg-[var(--bg-primary)]/80 backdrop-blur-sm">
                    <div className="text-center max-w-md p-8 bg-[var(--bg-secondary)] rounded-2xl shadow-2xl">
                        <Icon name="spinner" spin className="text-6xl text-[var(--accent-color)] mb-4" />
                        <h2 className="text-2xl font-bold text-[var(--text-primary)] mb-4">
                            Starting Browser Audio
                        </h2>
                        <p className="text-[var(--text-secondary)] mb-6">
                            The visualizer is starting browser audio playback to analyze the audio stream.
                            This may take a moment...
                        </p>
                        <button
                            onClick={onClose}
                            className="px-6 py-3 bg-[var(--bg-secondary)] text-[var(--text-primary)] rounded-full hover:bg-[var(--bg-tertiary)] transition-all font-semibold"
                        >
                            Close Visualizer
                        </button>
                    </div>
                </div>
            )}

            {/* Visualizer Canvas - fills entire background */}
            <div className="absolute inset-0 z-0 pointer-events-none">
                <AmorphousBlob
                    getFrame={getSpectrum}
                    settings={{ ...visualizerSettings, enabled: true }}
                    albumArtUrl={albumArtUrl}
                    accentColor={accentColor}
                    themeSettings={settings.theme}
                    onColorChange={setVisualizerColor}
                />
            </div>

            {/* Progress Ring around album art - centered between top and play button */}
            {/* SVG matches album art size (15% of min dimension = 30vw/30vh diameter) */}
            <svg
                className="absolute left-1/2 pointer-events-none z-10"
                style={{
                    top: 'calc(50vh - 90px)',
                    width: 'min(30vw, 30vh)',
                    height: 'min(30vw, 30vh)',
                    transform: 'translate(-50%, -50%) rotate(-90deg)',
                }}
                viewBox="0 0 120 120"
                overflow="visible"
            >
                {/* Background ring - at album art outer edge */}
                <circle
                    cx="60"
                    cy="60"
                    r="60"
                    fill="none"
                    stroke={visualizerColor}
                    strokeWidth="2"
                    opacity="1"
                />
                {/* Progress ring - thicker filled portion along same path */}
                <circle
                    cx="60"
                    cy="60"
                    r="60"
                    fill="none"
                    stroke={visualizerColor}
                    strokeWidth="5"
                    strokeLinecap="round"
                    strokeDasharray={2 * Math.PI * 60}
                    strokeDashoffset={2 * Math.PI * 60 * (1 - progressPercent / 100)}
                    style={{
                        transition: 'stroke-dashoffset 0.3s ease',
                    }}
                    opacity="1"
                />
            </svg>

            {/* Top Left: Metadata */}
            {/* pr-16 reserves the close button's lane: the title is the one string here with no
                length limit, and `max-w-md` alone let it slide under the button on a narrow window. */}
            <div className="absolute top-4 sm:top-8 left-4 sm:left-8 right-4 sm:right-8 pr-16 text-left max-w-md space-y-3 z-10">
                {/* Metadata */}
                {currentTrack && (
                    <div>
                        <h1 className="text-2xl font-bold text-[var(--accent-color)] mb-1">
                            {currentTrack.title}
                        </h1>
                        <h2 className="text-lg font-bold text-[var(--text-primary)]">
                            {currentTrack.artist}
                        </h2>
                        <h3 className="text-sm text-[var(--text-secondary)] mt-1">
                            {currentTrack.album}
                        </h3>
                    </div>
                )}
            </div>

            {/* Top Right: Close Button */}
            <div className="absolute top-4 sm:top-8 right-4 sm:right-8 z-10">
                <button
                    onClick={onClose}
                    className="w-12 h-12 flex items-center justify-center rounded-full bg-[var(--bg-secondary)]/80 backdrop-blur-sm hover:bg-[var(--bg-tertiary)] transition-colors"
                    aria-label="Close visualizer"
                    title="Close visualizer (Esc)"
                >
                    <Icon name="xmark" className="w-6 h-6 text-[var(--accent-color)]" />
                </button>
            </div>

            {/* Bottom bar — ONE grid, not three independently-positioned islands.
                Previously the selector (bottom-8 left-8), the controls (bottom-8 left-1/2) and the
                icon cluster (bottom-8 right-8) were absolute siblings that knew nothing about each
                other, with a hard-coded 240px slider and a `calc(50% - 200px)` selector. Narrow the
                window and the volume slider ran UNDERNEATH the icon cluster — its right end was
                unreachable — and below 400px that calc goes negative. Observed on a tall window,
                2026-08-06.

                `1fr auto 1fr` keeps the controls optically centred (the outer tracks are forced
                equal) while making collision impossible, because they are now grid cells rather
                than overlapping layers. Below `sm` it stacks instead of squeezing. */}
            <div className="absolute inset-x-0 bottom-0 z-10 p-4 sm:p-8 grid gap-4 items-end
                            grid-cols-1 justify-items-center sm:grid-cols-[1fr_auto_1fr]">

            {/* Bottom Left: Stream Selector */}
            <div className="w-full min-w-0 max-w-xs sm:justify-self-start order-2 sm:order-none">
                <StreamSelector
                    streams={streams}
                    currentStreamId={stream?.id || null}
                    onSelectStream={onStreamChange}
                    federationEnabled={settings.federation.enabled}
                    openUpward={true}
                />
            </div>

            {/* Bottom Center: Media Controls & Volume */}
            <div className="flex flex-col items-center gap-4 sm:gap-6 min-w-0 max-w-full order-1 sm:order-none">
                {/* Media Control Buttons */}
                <div className="flex items-center gap-6">
                    <button
                        onClick={() => onSkip('previous')}
                        className="w-14 h-14 flex items-center justify-center rounded-full bg-[var(--bg-secondary)]/80 backdrop-blur-sm hover:bg-[var(--bg-tertiary)] transition-colors"
                        aria-label="Previous track"
                    >
                        <Icon name="backward-step" className="w-7 h-7 text-[var(--text-primary)]" />
                    </button>

                    <button
                        onClick={onPlayPause}
                        className="w-20 h-20 flex items-center justify-center rounded-full bg-[var(--accent-color)] hover:brightness-110 transition-all shadow-lg"
                        aria-label="Play/Pause"
                    >
                        <Icon
                            name={stream?.isPlaying ? 'pause' : 'play'}
                            className="w-10 h-10 accent-button-text"
                        />
                    </button>

                    <button
                        onClick={() => onSkip('next')}
                        className="w-14 h-14 flex items-center justify-center rounded-full bg-[var(--bg-secondary)]/80 backdrop-blur-sm hover:bg-[var(--bg-tertiary)] transition-colors"
                        aria-label="Next track"
                    >
                        <Icon name="forward-step" className="w-7 h-7 text-[var(--text-primary)]" />
                    </button>
                </div>

                {/* Volume Control - same width as media controls */}
                <div className="flex items-center gap-4 bg-[var(--bg-secondary)]/80 backdrop-blur-sm rounded-full px-4 sm:px-6 py-3 max-w-full">
                    <Icon name="volume-low" className="w-5 h-5 text-[var(--text-secondary)]" />
                    <input
                        type="range"
                        min="0"
                        max="100"
                        value={currentVolume}
                        onChange={(e) => onVolumeChange(parseInt(e.target.value))}
                        className="volume-slider rounded-lg min-w-0"
                        // A 240px BASIS that is allowed to shrink — not `w-full` with a 240px cap.
                        // `w-full` inside the shrink-to-fit middle grid column resolves against a
                        // min-content parent, so it collapsed to ~138px at every width: no overlap,
                        // but a visibly shorter slider than before. Measured in the running page
                        // across 360-1280px: this gives 240px wherever there is room and shrinks
                        // cleanly on a small phone, with no overlap anywhere.
                        style={{ ...volumeSliderStyle, width: '240px', maxWidth: '100%' }}
                        aria-label="Volume control"
                    />
                    <Icon name="volume-high" className="w-5 h-5 text-[var(--text-secondary)]" />
                </div>
            </div>

            {/* Bottom Right: Fullscreen, Visualizer Settings, Settings, Listen Button */}
            <div className="flex gap-2 sm:gap-3 shrink-0 sm:justify-self-end order-3 sm:order-none">

                <button
                    onClick={toggleFullscreen}
                    className="w-12 h-12 flex items-center justify-center rounded-full bg-[var(--bg-secondary)]/80 backdrop-blur-sm hover:bg-[var(--bg-tertiary)] transition-colors"
                    aria-label={isFullscreen ? 'Exit fullscreen' : 'Enter fullscreen'}
                    title={isFullscreen ? 'Exit fullscreen (Esc)' : 'Enter fullscreen'}
                >
                    <Icon
                        name="desktop"
                        className="w-6 h-6 text-[var(--text-primary)]"
                    />
                </button>

                <button
                    onClick={onOpenVisualizerSettings}
                    className="w-12 h-12 flex items-center justify-center rounded-full bg-[var(--bg-secondary)]/80 backdrop-blur-sm hover:bg-[var(--bg-tertiary)] transition-colors"
                    aria-label="Visualizer Settings"
                    title="Visualizer Settings"
                >
                    <Icon name="waveform" className="w-6 h-6 text-[var(--text-primary)]" />
                </button>

                <button
                    onClick={onOpenSettings}
                    className="w-12 h-12 flex items-center justify-center rounded-full bg-[var(--bg-secondary)]/80 backdrop-blur-sm hover:bg-[var(--bg-tertiary)] transition-colors"
                    aria-label="Settings"
                    title="Settings"
                >
                    <Icon name="gear" className="w-6 h-6 text-[var(--text-primary)]" />
                </button>
            </div>

            </div>
        </div>
    );
};
