import React, {useState, useEffect} from 'react';
import type {Settings as SettingsType} from '../../types';
import {Switch} from '../Switch';
import {federationService} from '../../services/federationService';

interface PlaybackTabProps {
    settings: SettingsType;
    onSettingsChange: (newSettings: SettingsType) => void;
}

const DEFAULT_AUTO_SWITCH = {
    localActivity: true,
    slave: {
        enabled: false,
        masterHost: '',
        masterWsPort: 1780,
        masterStreamPort: 1704,
    },
};

export const PlaybackTab: React.FC<PlaybackTabProps> = ({settings, onSettingsChange}) => {
    const autoSwitch = settings.autoSwitch ?? DEFAULT_AUTO_SWITCH;

    const [masterHostInput, setMasterHostInput] = useState(autoSwitch.slave.masterHost);
    const [isSavingHost, setIsSavingHost] = useState(false);
    const [hostSaved, setHostSaved] = useState(false);
    const [federatedHosts, setFederatedHosts] = useState<{name: string; host: string}[]>([]);

    useEffect(() => {
        setMasterHostInput(autoSwitch.slave.masterHost);
    }, [autoSwitch.slave.masterHost]);

    useEffect(() => {
        if (!settings.federation?.enabled) return;
        let cancelled = false;
        federationService.getServers().then(servers => {
            if (!cancelled) {
                setFederatedHosts(
                    servers
                        .filter(s => s.connected)
                        .map(s => ({name: s.name || s.host, host: s.host}))
                );
            }
        }).catch(() => {});
        return () => { cancelled = true; };
    }, [settings.federation?.enabled]);

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

    const handleMasterHostSave = () => {
        setIsSavingHost(true);
        setHostSaved(false);
        updateAutoSwitch({slave: {...autoSwitch.slave, masterHost: masterHostInput.trim()}});
        setHostSaved(true);
        setIsSavingHost(false);
        setTimeout(() => setHostSaved(false), 2000);
    };

    const handleSelectFederatedHost = (host: string) => {
        setMasterHostInput(host);
        updateAutoSwitch({slave: {...autoSwitch.slave, masterHost: host}});
        setHostSaved(true);
        setTimeout(() => setHostSaved(false), 2000);
    };

    return (
        <div className="space-y-6">
            <div>
                <h3 className="text-base font-semibold text-[var(--text-primary)] mb-1">
                    Playback Routing
                </h3>
                <p className="text-sm text-[var(--text-muted)] mb-6">
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
                            <label className="block text-sm text-[var(--text-secondary)] mb-1">
                                Master unit hostname or IP
                            </label>
                            <div className="flex gap-2">
                                <input
                                    type="text"
                                    value={masterHostInput}
                                    onChange={e => setMasterHostInput(e.target.value)}
                                    placeholder="e.g. 192.168.1.50 or living-room.local"
                                    className="flex-1 px-3 py-2 rounded-lg bg-[var(--bg-tertiary)] border border-[var(--border-color)] text-sm text-[var(--text-primary)] placeholder-[var(--text-muted)] focus:outline-none focus:border-[var(--accent-color)]"
                                    onKeyDown={e => e.key === 'Enter' && handleMasterHostSave()}
                                />
                                <button
                                    onClick={handleMasterHostSave}
                                    disabled={isSavingHost || !masterHostInput.trim()}
                                    className="px-3 py-2 rounded-lg bg-[var(--accent-color)] text-white text-sm font-medium disabled:opacity-40"
                                >
                                    {hostSaved ? '✓' : 'Save'}
                                </button>
                            </div>
                        </div>

                        {federatedHosts.length > 0 && (
                            <div>
                                <p className="text-xs text-[var(--text-muted)] mb-2">
                                    Connected units (click to select):
                                </p>
                                <div className="flex flex-wrap gap-2">
                                    {federatedHosts.map(({name, host}) => (
                                        <button
                                            key={host}
                                            onClick={() => handleSelectFederatedHost(host)}
                                            className={`px-3 py-1 rounded-full text-xs border transition ${
                                                autoSwitch.slave.masterHost === host
                                                    ? 'bg-[var(--accent-color)] text-white border-[var(--accent-color)]'
                                                    : 'text-[var(--text-secondary)] border-[var(--border-color)] hover:border-[var(--accent-color)]'
                                            }`}
                                        >
                                            {name} ({host})
                                        </button>
                                    ))}
                                </div>
                            </div>
                        )}

                        {autoSwitch.slave.masterHost && (
                            <p className="text-xs text-[var(--text-muted)]">
                                Following: <span className="text-[var(--text-primary)]">{autoSwitch.slave.masterHost}</span>
                            </p>
                        )}
                    </div>
                )}
            </div>
        </div>
    );
};
