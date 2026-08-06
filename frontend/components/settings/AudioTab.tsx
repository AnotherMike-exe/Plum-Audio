import React from 'react';
import type {Settings as SettingsType} from '../../types';
import {OutputDeviceSection} from './OutputDeviceSection';

interface AudioTabProps {
    settings: SettingsType;
    onSettingsChange: (newSettings: SettingsType) => void;
}

/**
 * Audio — this unit's own hardware: what it plays THROUGH.
 *
 * Its own tab, as in Plum-Snapcast, and deliberately separate from Playback (which is about where
 * audio comes FROM: auto-switch and follow rules). The output picker briefly lived under Playback;
 * that was a misread of the original layout, not a design decision.
 *
 * Plum-Snapcast's Audio tab also carried Audio Input and Volume Calibration. Neither has a backend
 * in Plum-Audio yet — they belong here when they land, not on another tab.
 */
export const AudioTab: React.FC<AudioTabProps> = () => {
    return (
        <div className="space-y-6">
            <OutputDeviceSection />
        </div>
    );
};
