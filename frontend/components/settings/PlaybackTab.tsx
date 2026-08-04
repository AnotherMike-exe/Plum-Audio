import React, {useState, useEffect} from 'react';
import type {Settings as SettingsType} from '../../types';
import {Switch} from '../Switch';
import {OutputDeviceSection} from './OutputDeviceSection';

interface PlaybackTabProps {
    settings: SettingsType;
    onSettingsChange: (newSettings: SettingsType) => void;
}

const DEFAULT_AUTO_SWITCH = {
    localActivity: false,
    slave: {
        enabled: false,
        masterUnitId: null as string | null,
    },
};

interface MeshUnit {
    unit_id: string;
    name: string;
    host: string;
}

export const PlaybackTab: React.FC<PlaybackTabProps> = ({settings, onSettingsChange}) => {
    const autoSwitch = settings.autoSwitch ?? DEFAULT_AUTO_SWITCH;

    const [units, setUnits] = useState<MeshUnit[]>([]);

    // Other mesh units to follow, from the same aggregated view the rest of the GUI already
    // polls — no separate federation API (that surface is inert; the mesh discovers peers itself).
    useEffect(() => {
        let cancelled = false;
        const poll = () => {
            fetch('/api/mesh/view')
                .then(r => r.json())
                .then(d => {
                    if (cancelled) return;
                    const others = (d.units ?? []).filter((u: MeshUnit & {unit_id: string}) => u.unit_id !== d.local_unit_id);
                    setUnits(others);
                })
                .catch(() => {});
        };
        poll();
        const id = window.setInterval(poll, 5000);
        return () => { cancelled = true; window.clearInterval(id); };
    }, []);

    const updateAutoSwitch = (patch: Partial<typeof DEFAULT_AUTO_SWITCH>) => {
        const updated: SettingsType = {
            ...settings,
            autoSwitch: {
                ...autoSwitch,
                ...patch,
                slave: {
                    ...autoSwitch.slave,
                    ...(patch.slave ?? {}),
                },
            },
        };
        onSettingsChange(updated);
    };

    const handleLocalActivityToggle = (enabled: boolean) => {
        updateAutoSwitch({localActivity: enabled});
    };

    const handleSlaveToggle = (enabled: boolean) => {
        updateAutoSwitch({slave: {...autoSwitch.slave, enabled}});
    };

    const handleSelectMaster = (unitId: string) => {
        updateAutoSwitch({slave: {...autoSwitch.slave, masterUnitId: unitId}});
    };

    const followedUnit = units.find(u => u.unit_id === autoSwitch.slave.masterUnitId);

    return (
        <div className="space-y-6">
            {/* Where the audio comes OUT, before the rules about where it comes FROM. */}
            <OutputDeviceSection />

            <div className="pt-4 border-t border-[var(--border-color)]">
                <h3 className="text-base font-semibold text-[var(--text-primary)] mb-1">
                    Playback Routing
                </h3>
                <p className="text-sm text-[var(--text-muted)]">
                    Control how this unit responds to new audio sources and other units on the network.
                </p>
            </div>

            {/* Local activity */}
            <div className="space-y-2">
                <Switch
                    label="Auto-switch on local activity"
                    checked={autoSwitch.localActivity}
                    onChange={handleLocalActivityToggle}
                    icon="tower-broadcast"
                />
                <p className="text-xs text-[var(--text-muted)] pl-8">
                    When a source connects to this unit (AirPlay, Bluetooth, Spotify, etc.) and the
                    output is idle, automatically switch to that stream.
                </p>
            </div>

            {/* Slave mode */}
            <div className="pt-4 border-t border-[var(--border-color)] space-y-2">
                <Switch
                    label="Follow another unit (slave mode)"
                    checked={autoSwitch.slave.enabled}
                    onChange={handleSlaveToggle}
                    icon="network-wired"
                />
                <p className="text-xs text-[var(--text-muted)] pl-8">
                    When a master unit starts playing and this unit is idle, automatically join
                    the master's stream. Local connections always take priority.
                </p>

                {autoSwitch.slave.enabled && (
                    <div className="pl-8 pt-3 space-y-4">
                        <div>
                            <p className="text-xs text-[var(--text-muted)] mb-2">
                                Unit to follow:
                            </p>
                            {units.length === 0 ? (
                                <p className="text-xs text-[var(--text-muted)]">
                                    No other units visible on the mesh yet.
                                </p>
                            ) : (
                                <div className="flex flex-wrap gap-2">
                                    {units.map(({unit_id, name}) => (
                                        <button
                                            key={unit_id}
                                            onClick={() => handleSelectMaster(unit_id)}
                                            className={`px-3 py-1 rounded-full text-xs border transition ${
                                                autoSwitch.slave.masterUnitId === unit_id
                                                    ? 'bg-[var(--accent-color)] text-white border-[var(--accent-color)]'
                                                    : 'text-[var(--text-secondary)] border-[var(--border-color)] hover:border-[var(--accent-color)]'
                                            }`}
                                        >
                                            {name}
                                        </button>
                                    ))}
                                </div>
                            )}
                        </div>

                        {autoSwitch.slave.masterUnitId && (
                            <p className="text-xs text-[var(--text-muted)]">
                                Following:{' '}
                                <span className="text-[var(--text-primary)]">
                                    {followedUnit?.name ?? autoSwitch.slave.masterUnitId}
                                </span>
                            </p>
                        )}
                    </div>
                )}
            </div>
        </div>
    );
};
