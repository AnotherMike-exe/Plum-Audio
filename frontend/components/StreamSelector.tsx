import React, {useEffect, useRef, useState} from 'react';
import type {Stream} from '../types';
import { Icon } from './Icon';

interface StreamSelectorProps {
    streams: Stream[];
    currentStreamId: string | null;
    onSelectStream: (streamId: string | null) => void;
    federationEnabled?: boolean;
    localServerId?: string;  // Local server ID for finding the correct none stream
    openUpward?: boolean;
}

/**
 * Helper function to get display name for a stream
 * Converts none-* streams to "None" for cleaner UI
 * In multi-server mode, none stream IDs are like "server-192-168-201-133-none-snapserver"
 */
const getStreamDisplayName = (stream: Stream, isMainTitle: boolean = false): string => {
    if (stream.id.includes('none-')) {
        // In main title, show "Select a Source", in dropdown show "None"
        return isMainTitle ? 'Select a Source' : 'None';
    }
    return stream.name;
};

export const StreamSelector: React.FC<StreamSelectorProps> = ({streams, currentStreamId, onSelectStream, federationEnabled = false, localServerId, openUpward = false}) => {
    const [isOpen, setIsOpen] = useState(false);
    const wrapperRef = useRef<HTMLDivElement>(null);
    const currentStream = streams.find(s => s.id === currentStreamId);

    useEffect(() => {
        const handleClickOutside = (event: MouseEvent) => {
            if (wrapperRef.current && !wrapperRef.current.contains(event.target as Node)) {
                setIsOpen(false);
            }
        };
        document.addEventListener('mousedown', handleClickOutside);
        return () => {
            document.removeEventListener('mousedown', handleClickOutside);
        };
    }, [wrapperRef]);

    const handleSelect = (streamId: string | null) => {
        onSelectStream(streamId);
        setIsOpen(false);
    };

    // Find the local none stream for the "None" option
    const getLocalNoneStream = (): string | null => {
        if (!localServerId) return null;
        const noneStream = streams.find(s =>
            s.serverId === localServerId && s.id.includes('none-')
        );
        return noneStream?.id || null;
    };

    const handleSelectNone = () => {
        const noneStreamId = getLocalNoneStream();
        onSelectStream(noneStreamId);
        setIsOpen(false);
    };

    // Check if current stream is a none stream
    const isCurrentStreamNone = currentStreamId?.includes('none-') || false;

    const groupedStreams = React.useMemo(() => {
        // Filter out none-* streams from dropdown (they're used internally but not selectable here)
        const selectableStreams = streams.filter(s => !s.id.includes('none-'));

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
    }, [streams, federationEnabled]);

    return (
        <div ref={wrapperRef} className="relative">
            <button
                onClick={() => setIsOpen(!isOpen)}
                className="w-full flex justify-between items-center text-left p-1 -m-1"
                aria-haspopup="listbox"
                aria-expanded={isOpen}
                aria-label="Select audio stream source"
            >
        <span className="text-3xl font-bold text-[var(--accent-color)] truncate pr-4">
          {isCurrentStreamNone ? 'Select a Source' : (currentStream ? getStreamDisplayName(currentStream, true) : 'Select a Source')}
        </span>
                <Icon name="chevron-down" className={`text-[var(--text-secondary)] transition-transform duration-200 ${isOpen ? 'rotate-180' : ''}`} style={{ color: 'inherit' }} aria-hidden />
            </button>

            {isOpen && (
                <div
                    className={`absolute z-10 w-full bg-[var(--bg-secondary)] border border-[var(--border-color)] rounded-lg shadow-xl ${
                        openUpward ? 'bottom-full mb-2' : 'top-full mt-2'
                    }`}
                    role="listbox"
                >
                    <ul className="py-2 text-base text-[var(--text-primary)] max-h-60 overflow-auto">
                        <li role="option">
                            <button
                                onClick={handleSelectNone}
                                className={`block w-full text-left px-4 py-2 text-[var(--text-secondary)] hover:bg-[var(--bg-secondary-hover)] ${isCurrentStreamNone ? 'font-semibold' : ''}`}
                            >
                                None
                            </button>
                        </li>
                        {federationEnabled ? (
                            Object.entries(groupedStreams).map(([serverName, serverStreams]) => (
                                <React.Fragment key={serverName}>
                                    <li className="px-4 py-2 text-xs font-semibold text-[var(--text-secondary)] uppercase tracking-wider border-t border-[var(--border-color)] mt-2 first:mt-0 first:border-0">
                                        {serverName}
                                    </li>
                                    {serverStreams.map(s => (
                                        <li key={s.id} role="option" aria-selected={currentStreamId === s.id}>
                                            <button
                                                onClick={() => handleSelect(s.id)}
                                                className={`block w-full text-left px-4 py-2 hover:bg-[var(--bg-secondary-hover)] transition-colors ${currentStreamId === s.id ? 'font-semibold text-[var(--accent-color)]' : ''}`}
                                            >
                                                {getStreamDisplayName(s)}
                                            </button>
                                        </li>
                                    ))}
                                </React.Fragment>
                            ))
                        ) : (
                            Object.entries(groupedStreams).map(([_, serverStreams]) =>
                                serverStreams.map(s => (
                                    <li key={s.id} role="option" aria-selected={currentStreamId === s.id}>
                                        <button
                                            onClick={() => handleSelect(s.id)}
                                            className={`block w-full text-left px-4 py-2 hover:bg-[var(--bg-secondary-hover)] transition-colors ${currentStreamId === s.id ? 'font-semibold text-[var(--accent-color)]' : ''}`}
                                        >
                                            {getStreamDisplayName(s)}
                                        </button>
                                    </li>
                                ))
                            )
                        )}
                    </ul>
                </div>
            )}
        </div>
    );
};