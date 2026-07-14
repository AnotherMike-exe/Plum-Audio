import React, {useEffect, useRef, useState} from 'react';
import type {Client, Stream} from '../types';
import {GroupVolumeControl} from './GroupVolumeControl';
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
    const [isSelectorOpen, setIsSelectorOpen] = useState(false);
    const wrapperRef = useRef<HTMLDivElement>(null);

    useEffect(() => {
        const handleClickOutside = (event: MouseEvent) => {
            if (wrapperRef.current && !wrapperRef.current.contains(event.target as Node)) {
                setIsSelectorOpen(false);
            }
        };
        document.addEventListener('mousedown', handleClickOutside);
        return () => {
            document.removeEventListener('mousedown', handleClickOutside);
        };
    }, [wrapperRef]);

    const handleSelectStream = (streamId: string | null) => {
        onStreamChange(client.id, streamId);
        setIsSelectorOpen(false);
    };

    // Find the none stream for this client's server
    const getNoneStreamForClient = (): string | null => {
        if (!client.serverId) return null;
        const noneStream = streams.find(s =>
            s.serverId === client.serverId && s.id.includes('none-')
        );
        return noneStream?.id || null;
    };

    const handleSelectNone = () => {
        const noneStreamId = getNoneStreamForClient();
        onStreamChange(client.id, noneStreamId);
        setIsSelectorOpen(false);
    };

    const volumePercentage = client.volume;
    const sliderStyle = {
        background: `linear-gradient(to right, var(--accent-color) ${volumePercentage}%, var(--border-color) ${volumePercentage}%)`
    };

    // Filter out "none" streams from dropdown options (they're used internally but shown via "None" button)
    const selectableStreams = React.useMemo(() => {
        return streams.filter(s => !s.id.includes('none-'));
    }, [streams]);

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
                <div ref={wrapperRef} className="relative">
                    <button
                        onClick={() => setIsSelectorOpen(!isSelectorOpen)}
                        className="w-8 h-8 flex items-center justify-center rounded-full text-[var(--text-secondary)] bg-[var(--border-color)] hover:bg-[var(--bg-secondary-hover)] transition-colors"
                        title="Change Stream"
                    >
                        <Icon name="tower-broadcast" style={{ color: 'inherit' }} />
                    </button>
                    {isSelectorOpen && (
                        <div
                            className="absolute z-10 bottom-full right-0 mb-2 w-48 bg-[var(--bg-secondary)] border border-[var(--border-color)] rounded-lg shadow-xl">
                            <ul className="py-1 text-sm text-[var(--text-primary)] max-h-40 overflow-auto">
                                <li role="option">
                                    <button
                                        onClick={handleSelectNone}
                                        className="block w-full text-left px-3 py-2 text-[var(--text-secondary)] hover:bg-[var(--bg-secondary-hover)]"
                                    >
                                        None
                                    </button>
                                </li>
                                {selectableStreams.map(s => (
                                    <li key={s.id} role="option" aria-selected={client.currentStreamId === s.id}>
                                        <button
                                            onClick={() => handleSelectStream(s.id)}
                                            className={`block w-full text-left px-3 py-2 hover:bg-[var(--bg-secondary-hover)] transition-colors truncate ${client.currentStreamId === s.id ? 'font-semibold text-[var(--accent-color)]' : ''}`}
                                        >
                                            {s.name}
                                        </button>
                                    </li>
                                ))}
                            </ul>
                        </div>
                    )}
                </div>
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