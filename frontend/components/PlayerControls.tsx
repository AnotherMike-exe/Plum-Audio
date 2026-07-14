import React from 'react';
import type {Stream} from '../types';
import { Icon, type IconName } from './Icon';

interface PlayerControlsProps {
    stream: Stream;
    volume: number;                              // Hardware/endpoint volume (Snapcast client)
    onVolumeChange: (volume: number) => void;
    sourceVolume?: number;                       // Source/integration volume (AirPlay, Spotify, etc.)
    onSourceVolumeChange?: (volume: number) => void;
    onPlayPause: () => void;
    onSkip: (direction: 'next' | 'prev') => void;
}

const ControlButton: React.FC<{ onClick?: () => void; icon: IconName; size?: 'sm' | 'md' | 'lg' }> = ({
                                                                                                        onClick,
                                                                                                        icon,
                                                                                                        size = 'md'
                                                                                                    }) => {
    const sizeClasses = {
        sm: 'w-10 h-10 text-base',
        md: 'w-12 h-12 text-lg',
        lg: 'w-16 h-16 text-2xl',
    };
    return (
        <button
            onClick={onClick}
            className={`flex items-center justify-center rounded-full text-[var(--control-icon-color)] bg-[var(--border-color)] hover:bg-[var(--bg-secondary-hover)] transition-colors duration-200 ${sizeClasses[size]}`}
            aria-label={icon.includes('play') ? 'Play' : icon.includes('pause') ? 'Pause' : icon.includes('backward') ? 'Previous track' : 'Next track'}
        >
            <Icon name={icon} style={{ color: 'inherit' }} />
        </button>
    );
};

export const PlayerControls: React.FC<PlayerControlsProps> = ({
                                                                  stream,
                                                                  volume,
                                                                  onVolumeChange,
                                                                  sourceVolume,
                                                                  onSourceVolumeChange,
                                                                  onPlayPause,
                                                                  onSkip
                                                              }) => {
    const volumePercentage = volume;
    const sliderStyle = {
        background: `linear-gradient(to right, var(--accent-color) ${volumePercentage}%, var(--border-color) ${volumePercentage}%)`
    };

    // Source volume slider style (uses a different color to distinguish)
    const hasSourceVolume = sourceVolume !== undefined && onSourceVolumeChange !== undefined;
    const sourceVolumePercentage = sourceVolume ?? 100;
    const sourceSliderStyle = {
        background: `linear-gradient(to right, var(--text-secondary) ${sourceVolumePercentage}%, var(--border-color) ${sourceVolumePercentage}%)`
    };

    return (
        <div className="flex flex-col md:flex-row items-center gap-6 px-4">
            {/* Mobile: Volume on top, Controls below */}
            {/* Desktop: Controls on left (aligned with artwork), Volume on right (aligned with text) */}

            {/* Media Controls - order-2 on mobile, order-1 on desktop */}
            {/* On desktop: flex-shrink-0 w-56 to match album artwork width (14rem = 224px) */}
            <div className="flex items-center gap-4 order-2 md:order-1 md:flex-shrink-0 md:w-56 justify-center">
                <ControlButton icon="backward-step" onClick={() => onSkip('prev')}/>
                <ControlButton icon={stream.isPlaying ? 'pause' : 'play'} onClick={onPlayPause} size="lg"/>
                <ControlButton icon="forward-step" onClick={() => onSkip('next')}/>
            </div>

            {/* Volume Controls - order-1 on mobile, order-2 on desktop */}
            {/* On desktop: flex-1 to fill remaining space (same as text area above) */}
            <div className="flex flex-col gap-2 w-full max-w-xs order-1 md:order-2 md:flex-1 md:max-w-none">
                {/* Source Volume (controls integration - AirPlay, Spotify, etc.) */}
                {hasSourceVolume && (
                    <div className="flex items-center gap-3 w-full">
                        <Icon name="tower-broadcast" className="text-[var(--text-secondary)] w-6 text-center flex-shrink-0" style={{ color: 'inherit' }} aria-hidden />
                        <input
                            type="range"
                            min="0"
                            max="100"
                            value={sourceVolume}
                            onChange={(e) => onSourceVolumeChange(Number(e.target.value))}
                            className="w-full h-2 rounded-lg appearance-none cursor-pointer volume-slider"
                            style={sourceSliderStyle}
                            aria-label="Source volume control"
                        />
                        <span className="text-xs text-[var(--text-secondary)] w-8 text-right flex-shrink-0">{sourceVolume}%</span>
                    </div>
                )}

                {/* Hardware Volume (controls Snapcast endpoint output) */}
                <div className="flex items-center gap-3 w-full">
                    <Icon name="volume-low" className="text-[var(--text-secondary)] w-6 text-center flex-shrink-0" style={{ color: 'inherit' }} aria-hidden />
                    <input
                        type="range"
                        min="0"
                        max="100"
                        value={volume}
                        onChange={(e) => onVolumeChange(Number(e.target.value))}
                        className="w-full h-2 rounded-lg appearance-none cursor-pointer volume-slider"
                        style={sliderStyle}
                        aria-label="Hardware volume control"
                    />
                    <Icon name="volume-high" className="text-[var(--text-secondary)] w-6 text-center flex-shrink-0" style={{ color: 'inherit' }} aria-hidden />
                </div>
            </div>
        </div>
    );
};