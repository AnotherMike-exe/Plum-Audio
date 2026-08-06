import React from 'react';
import type {Stream} from '../types';
import { Icon, type IconName } from './Icon';

interface PlayerControlsProps {
    stream: Stream;
    // Group/endpoint volume: the speakers rendering this source. One Sendspin controller command;
    // the server redistributes it across them, preserving their relative levels.
    volume: number;
    onVolumeChange: (volume: number) => void;
    /**
     * Hide the endpoint slider entirely. True on an ingest/routing-only unit: there is no local
     * speaker, so there is no level to show — the slider rendered a phantom 100% whose onChange
     * found no client and silently did nothing, then snapped back on the next poll.
     */
    hideEndpointVolume?: boolean;
    // Source volume: the level on the SENDING device (AirPlay/Bluetooth phone, Spotify Connect).
    // Undefined when this source has never reported one — only then is the row hidden.
    sourceVolume?: number;
    onSourceVolumeChange?: (volume: number) => void;
    // The sender is not currently reachable, so the level is a last-known value and cannot be
    // driven. The row STAYS, disabled: a control that vanishes mid-session reads as a bug, and an
    // AirPlay pause long enough for iOS to drop the RAOP session does exactly that. See MeshApp.
    sourceVolumeUnavailable?: boolean;
    onPlayPause: () => void;
    onSkip: (direction: 'next' | 'prev') => void;
    // Repeat/shuffle are shown ONLY when the source advertises them (canShuffle/canRepeat, derived
    // from the Sendspin controller supported_commands) — so Spotify shows them and AirPlay doesn't.
    canShuffle?: boolean;
    canRepeat?: boolean;
    shuffle?: boolean;
    repeat?: 'off' | 'one' | 'all';
    onToggleShuffle?: () => void;
    onCycleRepeat?: () => void;
}

// Icon-only toggle (shuffle/repeat): no filled pill like the transport buttons, just an icon that
// lights to the accent color when active. `badge` overlays a small glyph (repeat-one's "1").
const ToggleButton: React.FC<{
    onClick?: () => void; icon: IconName; active: boolean; label: string; badge?: string;
}> = ({ onClick, icon, active, label, badge }) => (
    <button
        onClick={onClick}
        aria-label={label}
        aria-pressed={active}
        className="relative flex items-center justify-center w-9 h-9 text-base transition-colors duration-200"
        style={{ color: active ? 'var(--accent-color)' : 'var(--text-secondary)' }}
    >
        {/* Wrap the icon in a box sized to the glyph so the badge anchors to the ICON's corner,
            not the (larger) button. The "1" sits just above the icon's top-right, clear of the loop. */}
        <span className="relative inline-flex items-center justify-center">
            <Icon name={icon} style={{ color: 'inherit' }} />
            {badge && (
                <span
                    className="absolute font-bold pointer-events-none leading-none"
                    style={{ fontSize: '7px', top: '-3px', right: '-4px', color: 'inherit' }}
                >
                    {badge}
                </span>
            )}
        </span>
    </button>
);

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
                                                                  hideEndpointVolume = false,
                                                                  sourceVolume,
                                                                  onSourceVolumeChange,
                                                                  sourceVolumeUnavailable,
                                                                  onPlayPause,
                                                                  onSkip,
                                                                  canShuffle,
                                                                  canRepeat,
                                                                  shuffle,
                                                                  repeat,
                                                                  onToggleShuffle,
                                                                  onCycleRepeat
                                                              }) => {
    const volumePercentage = volume;
    const sliderStyle = {
        background: `linear-gradient(to right, var(--accent-color) ${volumePercentage}%, var(--border-color) ${volumePercentage}%)`
    };

    // Source volume slider style (uses a different color to distinguish). The row is shown whenever
    // a level is KNOWN — `sourceVolumeUnavailable` only greys it out, so a sender that goes away
    // mid-session leaves the control in place instead of collapsing the layout under the user.
    const hasSourceVolume = sourceVolume !== undefined && onSourceVolumeChange !== undefined;
    const sourceVolumePercentage = sourceVolume ?? 100;
    const sourceTrackColor = sourceVolumeUnavailable ? 'var(--border-color)' : 'var(--text-secondary)';
    const sourceSliderStyle = {
        background: `linear-gradient(to right, ${sourceTrackColor} ${sourceVolumePercentage}%, var(--border-color) ${sourceVolumePercentage}%)`
    };

    return (
        <div className="flex flex-col md:flex-row items-center gap-6 px-4">
            {/* Mobile: Volume on top, Controls below */}
            {/* Desktop: Controls on left (aligned with artwork), Volume on right (aligned with text) */}

            {/* Media Controls - order-2 on mobile, order-1 on desktop */}
            {/* On desktop: flex-shrink-0 w-56 to match album artwork width (14rem = 224px) */}
            <div className="flex items-center gap-3 order-2 md:order-1 md:flex-shrink-0 md:w-56 justify-center">
                {canShuffle && (
                    <ToggleButton
                        icon="shuffle"
                        active={!!shuffle}
                        onClick={onToggleShuffle}
                        label={shuffle ? 'Shuffle on' : 'Shuffle off'}
                    />
                )}
                <ControlButton icon="backward-step" onClick={() => onSkip('prev')}/>
                <ControlButton icon={stream.isPlaying ? 'pause' : 'play'} onClick={onPlayPause} size="lg"/>
                <ControlButton icon="forward-step" onClick={() => onSkip('next')}/>
                {canRepeat && (
                    <ToggleButton
                        icon="repeat"
                        active={repeat != null && repeat !== 'off'}
                        onClick={onCycleRepeat}
                        label={`Repeat ${repeat ?? 'off'}`}
                        badge={repeat === 'one' ? '1' : undefined}
                    />
                )}
            </div>

            {/* Volume Controls - order-1 on mobile, order-2 on desktop */}
            {/* On desktop: flex-1 to fill remaining space (same as text area above) */}
            <div className="flex flex-col gap-2 w-full max-w-xs order-1 md:order-2 md:flex-1 md:max-w-none">
                {/* Source volume — the sending device's own level (AirPlay/Bluetooth phone, Spotify
                    Connect). Hidden only when this source has NEVER reported one; once a level is
                    known the row stays put and merely greys out while the sender is away. */}
                {hasSourceVolume && (
                    <div
                        className={`flex items-center gap-3 w-full transition-opacity ${sourceVolumeUnavailable ? 'opacity-40' : ''}`}
                        title={sourceVolumeUnavailable ? 'The sending device is not connected — last known level' : undefined}
                    >
                        <Icon name="tower-broadcast" className="text-[var(--text-secondary)] w-6 text-center flex-shrink-0" style={{ color: 'inherit' }} aria-hidden />
                        <input
                            type="range"
                            min="0"
                            max="100"
                            value={sourceVolume}
                            disabled={sourceVolumeUnavailable}
                            // Guarded as well as disabled: `disabled` stops a user dragging it, but
                            // it does not stop a change event reaching the handler, and driving a
                            // level at a sender that is not there would be silently discarded.
                            onChange={(e) => {
                                if (sourceVolumeUnavailable) return;
                                onSourceVolumeChange(Number(e.target.value));
                            }}
                            className={`w-full h-2 rounded-lg appearance-none volume-slider ${sourceVolumeUnavailable ? 'cursor-not-allowed' : 'cursor-pointer'}`}
                            style={sourceSliderStyle}
                            aria-label="Source volume control"
                            aria-disabled={sourceVolumeUnavailable}
                        />
                        <span className="text-xs text-[var(--text-secondary)] w-8 text-right flex-shrink-0">{sourceVolume}%</span>
                    </div>
                )}

                {/* Group volume — the output level of every endpoint rendering this source */}
                {!hideEndpointVolume && (
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
                        aria-label="Group volume control"
                    />
                    <Icon name="volume-high" className="text-[var(--text-secondary)] w-6 text-center flex-shrink-0" style={{ color: 'inherit' }} aria-hidden />
                </div>
                )}
            </div>
        </div>
    );
};