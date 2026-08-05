import React from 'react';
import type {Client, Stream} from '../types';
import {GroupVolumeControl} from './GroupVolumeControl';
import {StreamPickerButton} from './StreamPickerButton';
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
    onStopBrowserAudio?: () => void;
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
    const volumePercentage = client.volume;
    const sliderStyle = {
        background: `linear-gradient(to right, var(--accent-color) ${volumePercentage}%, var(--border-color) ${volumePercentage}%)`
    };

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
            <StreamPickerButton
                streams={streams}
                currentStreamId={client.currentStreamId}
                onSelect={(streamId) => onStreamChange(client.id, streamId)}
                federationEnabled={federationEnabled}
                title={`Change ${client.name}'s stream`}
            />
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
                                                                onStopBrowserAudio,
                                                                browserAudioActive,
                                                                federationEnabled = false,
                                                            }) => {
    // "Listen in Browser" toggles to "Stop Listening" while this tab is a player. Rendered in both
    // the empty-state and the populated-list footer.
    const browserAudioButton =
        browserAudioActive && onStopBrowserAudio ? (
            <button
                onClick={onStopBrowserAudio}
                className="w-full bg-[var(--bg-tertiary)] text-[var(--text-primary)] font-bold py-3 px-4 rounded-lg hover:bg-[var(--bg-tertiary-hover)] transition-colors flex items-center justify-center gap-2"
            >
                <Icon name="headphones" style={{ color: 'inherit' }} />
                Stop Listening
            </button>
        ) : onStartBrowserAudio && !browserAudioActive ? (
            <button
                onClick={onStartBrowserAudio}
                className="w-full bg-[var(--accent-color)] accent-button-text font-bold py-3 px-4 rounded-lg hover:bg-[var(--accent-color-hover)] transition-colors flex items-center justify-center gap-2"
            >
                <Icon name="headphones" style={{ color: 'inherit' }} />
                Listen in Browser
            </button>
        ) : null;
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
                {browserAudioButton}
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
                                {/* A speaker claimed by a server outside our mesh (Music Assistant,
                                    any third-party Sendspin server) is not idle — say where it went
                                    and what it is playing. Join Stream still pulls it back. */}
                                <span className="truncate flex-1">
                                    <span className="font-semibold">{client.name}</span>
                                    {client.foreignServer && (
                                        <span className="block text-xs text-[var(--text-secondary)] truncate">
                                            <Icon name="tower-broadcast" className="mr-1 text-[var(--text-muted)]" />
                                            {client.foreignServer.name}
                                            {client.foreignServer.title ? ` · ${client.foreignServer.title}` : ''}
                                            {client.foreignServer.artist ? ` — ${client.foreignServer.artist}` : ''}
                                        </span>
                                    )}
                                </span>
                                <div className="flex items-center gap-2 flex-shrink-0">
                                    <button
                                        onClick={() => onStreamChange(client.id, myClientStreamId)}
                                        disabled={!myClientStreamId}
                                        className="text-sm bg-[var(--accent-color)] accent-button-text font-bold py-1 px-3 rounded-full hover:bg-[var(--accent-color-hover)] transition-colors disabled:bg-gray-500 disabled:cursor-not-allowed"
                                        title={myClientStreamId ? 'Join your current stream' : 'Select a stream first'}
                                    >
                                        <Icon name="plus" className="mr-1" />
                                        Join Stream
                                    </button>
                                    {/* Join Stream is the one-click case: bring it to what THIS page is
                                        playing. The picker is the general one — send an idle speaker to
                                        any source anyone is feeding, including one this unit isn't on.
                                        The mesh router already supports it: an idle player is in no
                                        unit's group, so it is reclaimed by the listener URL from its own
                                        unit's self-report (mesh/router.py route_player). Without this,
                                        an idle unit's GUI could route nothing at all. */}
                                    <StreamPickerButton
                                        streams={streams}
                                        currentStreamId={null}
                                        onSelect={(streamId) => onStreamChange(client.id, streamId)}
                                        federationEnabled={federationEnabled}
                                        title={`Send ${client.name} to a stream`}
                                    />
                                </div>
                            </div>
                        ))}
                    </div>
                </div>
            )}

            {browserAudioButton}
        </div>
    );
};