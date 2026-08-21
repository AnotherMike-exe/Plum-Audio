import React from 'react';
import type {Settings as SettingsType} from '../../types';
import {CalibrationSection} from './CalibrationSection';
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
 * Plum-Snapcast's Audio tab also carried Audio Input and Volume Calibration. Volume Calibration has
 * landed and lives here; Audio Input still has no backend (it is a line-in SOURCE, which means a
 * source manager and a feeder, not a checkbox — see apis/audio_api.py).
 */
export const AudioTab: React.FC<AudioTabProps> = () => {
    return (
        <div className="space-y-8">
            <OutputDeviceSection />
            <CalibrationSection />
        </div>
    );
};
