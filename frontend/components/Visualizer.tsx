import React, { useEffect, useState } from 'react';
import type { Settings, Stream, VisualizerSettings } from '../types';
import { AmorphousBlob } from './AmorphousBlob';
import { StreamSelector } from './StreamSelector';
import { Icon } from './Icon';
import { formatTime } from '../utils/time';

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
    browserAudioSnapStream: any; // SnapStream type
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
    browserAudioSnapStream,
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

    // Auto-start browser audio when visualizer opens if not already active
    useEffect(() => {
        if (isOpen && !browserAudioSnapStream) {
            onStartBrowserAudio();
        }
    }, [isOpen]); // Run when visualizer opens

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
            theme: 'smart',
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
    const isBrowserAudioActive = browserAudioSnapStream !== null;

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
                    snapStream={browserAudioSnapStream}
                    settings={visualizerSettings}
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
            <div className="absolute top-8 left-8 text-left max-w-md space-y-3 z-10">
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
            <div className="absolute top-8 right-8 z-10">
                <button
                    onClick={onClose}
                    className="w-12 h-12 flex items-center justify-center rounded-full bg-[var(--bg-secondary)]/80 backdrop-blur-sm hover:bg-[var(--bg-tertiary)] transition-colors"
                    aria-label="Close visualizer"
                    title="Close visualizer (Esc)"
                >
                    <Icon name="xmark" className="w-6 h-6 text-[var(--accent-color)]" />
                </button>
            </div>

            {/* Bottom Left: Stream Selector */}
            <div className="absolute bottom-8 left-8 z-10 w-full" style={{ maxWidth: 'calc(50% - 200px)' }}>
                <StreamSelector
                    streams={streams}
                    currentStreamId={stream?.id || null}
                    onSelectStream={onStreamChange}
                    federationEnabled={settings.federation.enabled}
                    openUpward={true}
                />
            </div>

            {/* Bottom Center: Media Controls & Volume */}
            <div className="absolute bottom-8 left-1/2 transform -translate-x-1/2 flex flex-col items-center gap-6 z-10">
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
                <div className="flex items-center gap-4 bg-[var(--bg-secondary)]/80 backdrop-blur-sm rounded-full px-6 py-3">
                    <Icon name="volume-low" className="w-5 h-5 text-[var(--text-secondary)]" />
                    <input
                        type="range"
                        min="0"
                        max="100"
                        value={currentVolume}
                        onChange={(e) => onVolumeChange(parseInt(e.target.value))}
                        className="volume-slider rounded-lg"
                        style={{ ...volumeSliderStyle, width: '240px' }}
                        aria-label="Volume control"
                    />
                    <Icon name="volume-high" className="w-5 h-5 text-[var(--text-secondary)]" />
                </div>
            </div>

            {/* Bottom Right: Fullscreen, Visualizer Settings, Settings, Listen Button */}
            <div className="absolute bottom-8 right-8 flex gap-3 z-10">
                <button
                    onClick={onToggleBrowserAudioMute}
                    className={`w-12 h-12 flex items-center justify-center rounded-full transition-colors ${
                        browserAudioMuted
                            ? 'bg-[var(--bg-secondary)]/80 backdrop-blur-sm hover:bg-[var(--bg-tertiary)]'
                            : 'bg-[var(--accent-color)] hover:brightness-110'
                    }`}
                    aria-label={browserAudioMuted ? 'Unmute browser audio' : 'Mute browser audio'}
                    title={browserAudioMuted ? 'Listen in Browser' : 'Mute Browser Audio'}
                >
                    <Icon
                        name={browserAudioMuted ? 'headphones' : 'headphones'}
                        className={`w-6 h-6 ${browserAudioMuted ? 'text-[var(--text-primary)]' : 'accent-button-text'}`}
                    />
                </button>

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
    );
};
