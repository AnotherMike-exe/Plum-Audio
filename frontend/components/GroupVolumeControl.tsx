import React from 'react';
import { Icon } from './Icon';

interface GroupVolumeControlProps {
    onAdjust: (direction: 'up' | 'down') => void;
    onMute: () => void;
}

export const GroupVolumeControl: React.FC<GroupVolumeControlProps> = ({onAdjust, onMute}) => {
    return (
        <div className="mt-4 pt-4 border-t border-(--border-color)">
            <h4 className="font-semibold mb-3 text-center text-(--text-secondary)">Group Volume</h4>
            <div className="flex items-center justify-center gap-4">
                <button
                    onClick={() => onAdjust('down')}
                    aria-label="Decrease group volume"
                    className="w-12 h-12 flex items-center justify-center rounded-full text-(--text-secondary) bg-(--border-color) hover:bg-(--bg-secondary-hover) transition-colors duration-200"
                >
                    <Icon name="volume-low" style={{ color: 'inherit' }} />
                </button>
                <button
                    onClick={onMute}
                    aria-label="Mute group"
                    className="w-12 h-12 flex items-center justify-center rounded-full accent-button-text bg-(--accent-color) hover:bg-(--accent-color-hover) transition-colors duration-200"
                >
                    <Icon name="volume-xmark" style={{ color: 'inherit' }} />
                </button>
                <button
                    onClick={() => onAdjust('up')}
                    aria-label="Increase group volume"
                    className="w-12 h-12 flex items-center justify-center rounded-full text-(--text-secondary) bg-(--border-color) hover:bg-(--bg-secondary-hover) transition-colors duration-200"
                >
                    <Icon name="volume-high" style={{ color: 'inherit' }} />
                </button>
            </div>
        </div>
    );
};