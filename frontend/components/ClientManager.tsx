import React, {useEffect, useRef, useState} from 'react';
import type {Client, Stream} from '../types';
import {GroupVolumeControl} from './GroupVolumeControl';
import { Icon } from './Icon';

interface ClientManagerProps {
    clients: Client[];
    streams: Stream[];
    myClientStreamId: string | null;
    onVolumeChange: (clientId: string, volume: number) => void;
    onStreamChange: (clientId: string, streamId: string | null) => void;
    onGroupVolumeAdjust: (streamId: string, direction: 'up' | 'down') => void;
    onGroupMute: (streamId: string) => void;
    onStartBrowserAudio?: () => void;
    browserAudioActive?: boolean;
    federationEnabled?: boolean;
}

const ClientDevice: React.FC<{
    client: Client;
    streams: Stream[];
    onVolumeChange: (clientId: string, volume: number) => void;
    onStreamChange: (clientId: string, streamId: string | null) => void;
    federationEnabled?: boolean;
}> = ({client, streams, onVolumeChange, onStreamChange, federationEnabled = false}) => {
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

    const groupedStreams = React.useMemo(() => {
        if (!federationEnabled) {
            return { ungrouped: selectableStreams };
        }

        const groups: { [serverName: string]: Stream[] } = {};
        selectableStreams.forEach(stream => {
            const serverName = stream.serverName || 'Unknown Server';
            if (!groups[serverName]) {
                groups[serverName] = [];
            }
            groups[serverName].push(stream);
        });
        return groups;
    }, [selectableStreams, federationEnabled]);

    return (
        <div className="flex items-center gap-3">
            <span className="flex-1 truncate font-semibold">{client.name}</span>
            <div className="flex items-center gap-2 w-40">
                <Icon name="volume-high" className="w-4 text-[var(--text-secondary)]" style={{ color: 'inherit' }} />
                <input
                    type="range"
                    min="0"
                    max="100"
                    value={client.volume}
                    onChange={(e) => onVolumeChange(client.id, Number(e.target.value))}
                    className="w-full h-2 rounded-lg appearance-none cursor-pointer volume-slider"
                    style={sliderStyle}
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
                            {federationEnabled ? (
                                Object.entries(groupedStreams).map(([serverName, serverStreams]) => (
                                    <React.Fragment key={serverName}>
                                        <li className="px-3 py-1 text-xs font-semibold text-[var(--text-secondary)] uppercase tracking-wider border-t border-[var(--border-color)] mt-1 first:mt-0 first:border-0">
                                            {serverName}
                                        </li>
                                        {serverStreams.map(s => (
                                            <li key={s.id} role="option" aria-selected={client.currentStreamId === s.id}>
                                                <button
                                                    onClick={() => handleSelectStream(s.id)}
                                                    className={`block w-full text-left px-3 py-2 hover:bg-[var(--bg-secondary-hover)] transition-colors truncate ${client.currentStreamId === s.id ? 'font-semibold text-[var(--accent-color)]' : ''}`}
                                                >
                                                    {s.name}
                                                </button>
                                            </li>
                                        ))}
                                    </React.Fragment>
                                ))
                            ) : (
                                selectableStreams.map(s => (
                                    <li key={s.id} role="option" aria-selected={client.currentStreamId === s.id}>
                                        <button
                                            onClick={() => handleSelectStream(s.id)}
                                            className={`block w-full text-left px-3 py-2 hover:bg-[var(--bg-secondary-hover)] transition-colors truncate ${client.currentStreamId === s.id ? 'font-semibold text-[var(--accent-color)]' : ''}`}
                                        >
                                            {s.name}
                                        </button>
                                    </li>
                                ))
                            )}
                        </ul>
                    </div>
                )}
            </div>
        </div>
    );
};

export const ClientManager: React.FC<ClientManagerProps> = ({
                                                                clients,
                                                                streams,
                                                                myClientStreamId,
                                                                onVolumeChange,
                                                                onStreamChange,
                                                                onGroupVolumeAdjust,
                                                                onGroupMute,
                                                                onStartBrowserAudio,
                                                                browserAudioActive,
                                                                federationEnabled = false,
                                                            }) => {
    const groupedClients = clients.reduce((acc, client) => {
        // Treat none streams as idle (no stream selected)
        const streamId = (client.currentStreamId?.includes('none-')) ? 'idle' : (client.currentStreamId ?? 'idle');
        if (!acc[streamId]) {
            acc[streamId] = [];
        }
        acc[streamId].push(client);
        return acc;
    }, {} as Record<string, Client[]>);

    const {idle: idleClients, ...streamedClients} = groupedClients;
    const streamGroups = Object.entries(streamedClients);

    // Show listen button if available, even when no other clients
    if (clients.length === 0) {
        return (
            <div className="space-y-6">
                <div className="text-center py-4">
                    <Icon name="desktop" className="text-4xl text-[var(--icon-muted)] mb-3" />
                    <p className="text-[var(--text-secondary)]">No other active devices.</p>
                </div>
                {onStartBrowserAudio && !browserAudioActive && (
                    <button
                        onClick={onStartBrowserAudio}
                        className="w-full bg-[var(--accent-color)] accent-button-text font-bold py-3 px-4 rounded-lg hover:bg-[var(--accent-color-hover)] transition-colors flex items-center justify-center gap-2"
                    >
                        <Icon name="headphones" style={{ color: 'inherit' }} />
                        Listen in Browser
                    </button>
                )}
            </div>
        );
    }

    return (
        <div className="space-y-6">
            {streamGroups.map(([streamId, clientsInGroup]) => {
                const stream = streams.find(s => s.id === streamId);
                if (!stream) return null;

                // Type assertion to ensure TypeScript knows clientsInGroup is Client[]
                const typedClientsInGroup = clientsInGroup as Client[];

                return (
                    <div key={streamId} className="bg-[var(--bg-tertiary)] p-4 rounded-lg">
                        <div className="border-b border-[var(--border-color)] pb-3 mb-3">
                            <h3 className="font-bold text-lg truncate text-[var(--text-primary)]">{stream.name}</h3>
                            <p className="text-sm text-[var(--text-secondary)] truncate">
                                <Icon name="music" className="mr-2 text-[var(--text-muted)]" />
                                {stream.currentTrack.title}
                            </p>
                        </div>
                        <div className="space-y-3">
                            {typedClientsInGroup.map(client => (
                                <ClientDevice key={client.id} client={client} streams={streams}
                                              onVolumeChange={onVolumeChange} onStreamChange={onStreamChange}
                                              federationEnabled={federationEnabled}/>
                            ))}
                        </div>
                        {typedClientsInGroup.length > 1 && (
                            <GroupVolumeControl
                                onAdjust={(dir) => onGroupVolumeAdjust(streamId, dir)}
                                onMute={() => onGroupMute(streamId)}
                            />
                        )}
                    </div>
                );
            })}

            {idleClients && idleClients.length > 0 && (
                <div className="bg-[var(--bg-tertiary)] p-4 rounded-lg">
                    <h3 className="font-bold text-lg text-[var(--text-primary)] border-b border-[var(--border-color)] pb-3 mb-3">Idle
                        Devices</h3>
                    <div className="space-y-2">
                        {idleClients.map(client => (
                            <div key={client.id}
                                 className="flex items-center justify-between gap-3 p-2 rounded-lg hover:bg-[var(--bg-tertiary-hover)]">
                                <span className="font-semibold truncate flex-1">{client.name}</span>
                                <button
                                    onClick={() => onStreamChange(client.id, myClientStreamId)}
                                    disabled={!myClientStreamId}
                                    className="text-sm bg-[var(--accent-color)] accent-button-text font-bold py-1 px-3 rounded-full hover:bg-[var(--accent-color-hover)] transition-colors disabled:bg-gray-500 disabled:cursor-not-allowed flex-shrink-0"
                                    title={myClientStreamId ? 'Join your current stream' : 'Select a stream first'}
                                >
                                    <Icon name="plus" className="mr-1" />
                                    Join Stream
                                </button>
                            </div>
                        ))}
                    </div>
                </div>
            )}

            {onStartBrowserAudio && !browserAudioActive && (
                <button
                    onClick={onStartBrowserAudio}
                    className="w-full bg-[var(--accent-color)] accent-button-text font-bold py-3 px-4 rounded-lg hover:bg-[var(--accent-color-hover)] transition-colors flex items-center justify-center gap-2"
                >
                    <Icon name="headphones" style={{ color: 'inherit' }} />
                    Listen in Browser
                </button>
            )}
        </div>
    );
};