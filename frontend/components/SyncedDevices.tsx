import React from 'react';
import type {Client, Stream} from '../types';
import {GroupVolumeControl} from './GroupVolumeControl';
import {StreamPickerButton} from './StreamPickerButton';
import { Icon } from './Icon';

interface SyncedDevicesProps {
    clients: Client[];
    streams: Stream[];
    onVolumeChange: (clientId: string, volume: number) => void;
    onStreamChange: (clientId: string, streamId: string | null) => void;
    onGroupVolumeAdjust: (direction: 'up' | 'down') => void;
    onGroupMute: () => void;
}

const SyncedDevice: React.FC<{
    client: Client;
    streams: Stream[];
    onVolumeChange: (clientId: string, volume: number) => void;
    onStreamChange: (clientId: string, streamId: string | null) => void;
}> = ({client, streams, onVolumeChange, onStreamChange}) => {
    const volumePercentage = client.volume;
    const sliderStyle = {
        background: `linear-gradient(to right, var(--accent-color) ${volumePercentage}%, var(--border-color) ${volumePercentage}%)`
    };

    return (
        <div className="flex items-center justify-between gap-4 p-2 rounded-lg hover:bg-[var(--bg-tertiary)]">
            <span className="font-semibold truncate">{client.name}</span>
            <div className="flex items-center gap-3">
                <div className="flex items-center gap-3 w-full max-w-[180px]">
                    <Icon name="volume-high" className="w-4 text-[var(--text-secondary)]" style={{ color: 'inherit' }} />
                    <input
                        type="range"
                        min="0"
                        max="100"
                        value={client.volume}
                        onChange={(e) => onVolumeChange(client.id, Number(e.target.value))}
                        className="w-full h-2 rounded-lg appearance-none cursor-pointer volume-slider"
                        style={sliderStyle}
                        aria-label={`${client.name} volume control`}
                    />
                </div>
                <StreamPickerButton
                    streams={streams}
                    currentStreamId={client.currentStreamId}
                    onSelect={(streamId) => onStreamChange(client.id, streamId)}
                    title={`Change ${client.name}'s stream`}
                />
            </div>
        </div>
    );
};


export const SyncedDevices: React.FC<SyncedDevicesProps> = ({
                                                                clients,
                                                                streams,
                                                                onVolumeChange,
                                                                onStreamChange,
                                                                onGroupVolumeAdjust,
                                                                onGroupMute
                                                            }) => {
    if (clients.length === 0) {
        return null;
    }

    return (
        <div className="mt-6 pt-6 border-t border-[var(--border-color)]">
            <h3 className="text-xl font-bold text-[var(--text-secondary)] mb-4">Synced Devices</h3>
            <div className="space-y-2">
                {clients.map(client => (
                    <SyncedDevice key={client.id} client={client} streams={streams} onVolumeChange={onVolumeChange}
                                  onStreamChange={onStreamChange}/>
                ))}
            </div>
            <GroupVolumeControl onAdjust={onGroupVolumeAdjust} onMute={onGroupMute}/>
        </div>
    );
};