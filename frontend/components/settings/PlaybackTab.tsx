import React, {useState, useEffect} from 'react';
import type {Settings as SettingsType} from '../../types';
import {Switch} from '../Switch';

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
    /** Which unit this one follows, if any — published so a follow CYCLE can be spotted. */
    follows_unit_id?: string | null;
}

export const PlaybackTab: React.FC<PlaybackTabProps> = ({settings, onSettingsChange}) => {
    const autoSwitch = settings.autoSwitch ?? DEFAULT_AUTO_SWITCH;

    const [units, setUnits] = useState<MeshUnit[]>([]);
    // Every unit including this one, plus our own id: choosing a master is only safe if it does not
    // close a follow loop, and answering that means walking the whole chain.
    const [allUnits, setAllUnits] = useState<MeshUnit[]>([]);
    const [localUnitId, setLocalUnitId] = useState<string | null>(null);
    // An ingest/routing-only unit has no speaker, so it has nothing to auto-switch OR to follow with.
    // Both toggles below drive routing of THIS unit's player; the backend refuses outright (its
    // FollowReconciler is never even started), so leaving them live would offer a setting that
    // silently does nothing.
    const [playerless, setPlayerless] = useState(false);

    // Other mesh units to follow, from the same aggregated view the rest of the GUI already
    // polls — no separate federation API (that surface is inert; the mesh discovers peers itself).
    useEffect(() => {
        let cancelled = false;
        const poll = () => {
            fetch('/api/mesh/view')
                .then(r => r.json())
                .then(d => {
                    if (cancelled) return;
                    const all = d.units ?? [];
                    const others = all.filter((u: MeshUnit & {unit_id: string}) => u.unit_id !== d.local_unit_id);
                    setUnits(others);
                    setAllUnits(all);
                    setLocalUnitId(d.local_unit_id ?? null);
                    const me = all.find((u: {unit_id: string}) => u.unit_id === d.local_unit_id);
                    // `!== false`, never `=== false`: a response without the field (an older image)
                    // must read as "has a speaker".
                    setPlayerless(me ? me.has_player === false : false);
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

    /**
     * Would following `candidate` close a loop back to us?
     *
     * Two units set to follow each other each route their own player onto the other's stream,
     * forever — the speaker changes stream about once a minute with nothing on screen to explain
     * it. The backend breaks such a cycle on its own (the lowest unit id stands down), but that is
     * a safety net for configurations made outside this page; far better not to offer the choice.
     * Handles chains, not just mutual follow: A -> B -> C -> A is the same bug.
     */
    const wouldLoop = (candidate: string): boolean => {
        if (!localUnitId) return false;              // we do not know who we are yet; do not block
        if (candidate === localUnitId) return true;  // following ourselves is a cycle of one
        const seen = new Set<string>([localUnitId]);
        let current: string | null | undefined = candidate;
        while (current) {
            if (current === localUnitId) return true;
            if (seen.has(current)) return false;     // a loop further along that we are not part of
            seen.add(current);
            current = allUnits.find(u => u.unit_id === current)?.follows_unit_id ?? null;
        }
        return false;
    };

    return (
        <div className="space-y-6">
            {/* Where audio comes FROM. Where it comes OUT is the Audio tab. */}
            <div>
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
                    checked={autoSwitch.localActivity && !playerless}
                    onChange={handleLocalActivityToggle}
                    icon="tower-broadcast"
                    disabled={playerless}
                />
                <p className="text-xs text-[var(--text-muted)] pl-8">
                    {playerless
                        ? 'This unit has no audio output, so it has nothing to switch. Other units can still follow it — they join whatever it is receiving.'
                        : 'When a source connects to this unit (AirPlay, Bluetooth, Spotify, etc.) and the output is idle, automatically switch to that stream.'}
                </p>
            </div>

            {/* Slave mode */}
            <div className="pt-4 border-t border-[var(--border-color)] space-y-2">
                <Switch
                    label="Follow another unit (slave mode)"
                    checked={autoSwitch.slave.enabled && !playerless}
                    onChange={handleSlaveToggle}
                    icon="network-wired"
                    disabled={playerless}
                />
                <p className="text-xs text-[var(--text-muted)] pl-8">
                    {playerless
                        ? 'This unit has no speaker to send to another unit\u2019s stream.'
                        : "When a master unit starts playing and this unit is idle, automatically join the master's stream. Local connections always take priority."}
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
                                    {units.map(({unit_id, name}) => {
                                        const loops = wouldLoop(unit_id);
                                        const selected = autoSwitch.slave.masterUnitId === unit_id;
                                        return (
                                            <button
                                                key={unit_id}
                                                onClick={() => !loops && handleSelectMaster(unit_id)}
                                                disabled={loops}
                                                title={loops
                                                    ? `${name} already follows this unit — following it back would leave both speakers switching stream every couple of minutes.`
                                                    : undefined}
                                                className={`px-3 py-1 rounded-full text-xs border transition ${
                                                    selected
                                                        ? 'bg-[var(--accent-color)] text-white border-[var(--accent-color)]'
                                                        : loops
                                                            ? 'text-[var(--text-muted)] border-[var(--border-color)] opacity-40 cursor-not-allowed'
                                                            : 'text-[var(--text-secondary)] border-[var(--border-color)] hover:border-[var(--accent-color)]'
                                                }`}
                                            >
                                                {name}{loops ? ' — follows this unit' : ''}
                                            </button>
                                        );
                                    })}
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
