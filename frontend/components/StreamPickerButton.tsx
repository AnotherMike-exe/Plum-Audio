import React, {useEffect, useRef, useState} from 'react';
import type {Stream} from '../types';
import {Icon} from './Icon';

/**
 * "Send this endpoint to…" — the round broadcast button + its dropdown.
 *
 * One component for every per-device picker (synced devices, other devices, idle devices). It was
 * three near-identical copies; the idle list had none at all, which is why an idle speaker could
 * only ever be pushed onto whatever THIS page happened to be playing.
 *
 * The `streams` handed in are the routable ones — sources a sender is actually feeding. An idle
 * source is a configured endpoint nobody is playing to, and offering it here is what surfaced as
 * "ghost sources": Bluetooth/Spotify/AirPlay entries in a device's picker long after the phone
 * disconnected. Routing to one is legal and does nothing audible, so the list is the filter (see
 * MeshApp's `stableRoutable`); this component does not second-guess it.
 *
 * "None" unroutes. It passes null rather than hunting for a `none-<server>` stream id: that was a
 * Snapcast idea, and in the mesh model a stream id is always `<unitId>::<sourceId>` — the lookup
 * could only ever resolve to null anyway.
 */

interface StreamPickerButtonProps {
    streams: Stream[];
    currentStreamId: string | null;
    onSelect: (streamId: string | null) => void;
    /** Group the list under server headings (multi-unit views). */
    federationEnabled?: boolean;
    /** Accessible name; also the tooltip. */
    title?: string;
}

export const StreamPickerButton: React.FC<StreamPickerButtonProps> = ({
    streams,
    currentStreamId,
    onSelect,
    federationEnabled = false,
    title = 'Change Stream',
}) => {
    const [isOpen, setIsOpen] = useState(false);
    const wrapperRef = useRef<HTMLDivElement>(null);

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
        onSelect(streamId);
        setIsOpen(false);
    };

    const groupedStreams = React.useMemo(() => {
        if (!federationEnabled) {
            return {ungrouped: streams};
        }
        const groups: {[serverName: string]: Stream[]} = {};
        streams.forEach(stream => {
            const serverName = stream.serverName || 'Unknown Server';
            if (!groups[serverName]) {
                groups[serverName] = [];
            }
            groups[serverName].push(stream);
        });
        return groups;
    }, [streams, federationEnabled]);

    const streamButton = (s: Stream) => (
        <li key={s.id} role="option" aria-selected={currentStreamId === s.id}>
            <button
                onClick={() => handleSelect(s.id)}
                className={`block w-full text-left px-3 py-2 hover:bg-[var(--bg-secondary-hover)] transition-colors truncate ${currentStreamId === s.id ? 'font-semibold text-[var(--accent-color)]' : ''}`}
            >
                {s.name}
            </button>
        </li>
    );

    return (
        <div ref={wrapperRef} className="relative">
            <button
                onClick={() => setIsOpen(!isOpen)}
                className="w-8 h-8 flex items-center justify-center rounded-full text-[var(--text-secondary)] bg-[var(--border-color)] hover:bg-[var(--bg-secondary-hover)] transition-colors"
                title={title}
                aria-label={title}
                aria-haspopup="listbox"
                aria-expanded={isOpen}
            >
                <Icon name="tower-broadcast" style={{color: 'inherit'}} />
            </button>
            {isOpen && (
                <div
                    className="absolute z-10 bottom-full right-0 mb-2 w-48 bg-[var(--bg-secondary)] border border-[var(--border-color)] rounded-lg shadow-xl"
                    role="listbox"
                >
                    <ul className="py-1 text-sm text-[var(--text-primary)] max-h-40 overflow-auto">
                        <li role="option" aria-selected={currentStreamId === null}>
                            <button
                                onClick={() => handleSelect(null)}
                                className={`block w-full text-left px-3 py-2 text-[var(--text-secondary)] hover:bg-[var(--bg-secondary-hover)] ${currentStreamId === null ? 'font-semibold' : ''}`}
                            >
                                None
                            </button>
                        </li>
                        {streams.length === 0 && (
                            <li className="px-3 py-2 text-xs text-[var(--text-muted)]">
                                No source is playing.
                            </li>
                        )}
                        {federationEnabled
                            ? Object.entries(groupedStreams).map(([serverName, serverStreams]) => (
                                  <React.Fragment key={serverName}>
                                      <li className="px-3 py-1 text-xs font-semibold text-[var(--text-secondary)] uppercase tracking-wider border-t border-[var(--border-color)] mt-1 first:mt-0 first:border-0">
                                          {serverName}
                                      </li>
                                      {serverStreams.map(streamButton)}
                                  </React.Fragment>
                              ))
                            : streams.map(streamButton)}
                    </ul>
                </div>
            )}
        </div>
    );
};
