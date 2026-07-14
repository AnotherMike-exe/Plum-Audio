import React, {useEffect, useRef, useState} from 'react';
import type {Stream} from '../types';
import {formatTime} from '../utils/time';

interface NowPlayingProps {
    stream: Stream;
    canSeek?: boolean;
    onSeek?: (position: number) => void;
    onAlbumArtClick?: () => void;
}

const ScrollingText: React.FC<{ text: string; className: string }> = ({text, className}) => {
    const containerRef = useRef<HTMLDivElement>(null);
    const textRef = useRef<HTMLDivElement>(null);
    const [shouldScroll, setShouldScroll] = useState(false);

    useEffect(() => {
        if (containerRef.current && textRef.current) {
            const isOverflowing = textRef.current.scrollWidth > containerRef.current.clientWidth;
            setShouldScroll(isOverflowing);
        }
    }, [text]);

    return (
        <div ref={containerRef} className={`overflow-hidden ${className}`}>
            <div
                ref={textRef}
                className={shouldScroll ? 'scrolling-text' : ''}
                style={shouldScroll ? {'--scroll-width': `${textRef.current?.scrollWidth || 0}px`} as React.CSSProperties : undefined}
            >
                {text}
            </div>
        </div>
    );
};

export const NowPlaying: React.FC<NowPlayingProps> = ({stream, canSeek = false, onSeek, onAlbumArtClick}) => {
    const {currentTrack, progress} = stream;
    const progressPercent = currentTrack.duration > 0
        ? Math.min(100, Math.max(0, (progress / currentTrack.duration) * 100))
        : 0;
    const progressBarRef = useRef<HTMLDivElement>(null);

    const handleProgressBarClick = (e: React.MouseEvent<HTMLDivElement>) => {
        if (!canSeek || !onSeek || !progressBarRef.current || currentTrack.duration === 0) {
            return;
        }

        const rect = progressBarRef.current.getBoundingClientRect();
        const clickX = e.clientX - rect.left;
        const percentClicked = Math.max(0, Math.min(1, clickX / rect.width));
        const newPosition = Math.floor(percentClicked * currentTrack.duration);

        onSeek(newPosition);
    };

    return (
        <div className="flex flex-col md:flex-row items-center gap-6 p-4">
            <div className="flex-shrink-0">
                <img
                    src={currentTrack.albumArtUrl}
                    alt={`Album art for ${currentTrack.album}`}
                    className={`w-48 h-48 md:w-56 md:h-56 rounded-lg shadow-lg object-cover transition-transform duration-300 hover:scale-105 ${
                        onAlbumArtClick ? 'cursor-pointer' : ''
                    }`}
                    onClick={onAlbumArtClick}
                    title={onAlbumArtClick ? 'Click to open visualizer' : undefined}
                />
            </div>
            <div className="flex-1 text-center md:text-left min-w-0">
                <ScrollingText text={currentTrack.title} className="text-3xl font-bold text-[var(--accent-color)]" />
                <ScrollingText text={currentTrack.artist} className="text-lg text-[var(--text-primary)] mt-1" />
                <ScrollingText text={currentTrack.album} className="text-md text-[var(--text-secondary)] mt-1" />

                <div className="mt-6">
                    <div
                        ref={progressBarRef}
                        className={`bg-[var(--border-color)] rounded-full h-2 w-full overflow-hidden ${canSeek ? 'cursor-pointer hover:h-3 transition-all' : ''}`}
                        onClick={handleProgressBarClick}
                        title={canSeek ? 'Click to seek' : undefined}
                    >
                        <div
                            className="bg-[var(--accent-color)] h-full rounded-full transition-all duration-1000 ease-linear max-w-full"
                            style={{width: `${progressPercent}%`}}
                        ></div>
                    </div>
                    <div className="flex justify-between text-xs text-[var(--text-secondary)] mt-2">
                        <span>{formatTime(progress)}</span>
                        <span>{formatTime(currentTrack.duration)}</span>
                    </div>
                </div>
            </div>
        </div>
    );
};