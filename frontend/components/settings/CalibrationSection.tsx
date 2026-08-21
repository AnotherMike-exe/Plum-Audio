import React, {useCallback, useEffect, useMemo, useState} from 'react';
import {Icon} from '../Icon';
import {CalibrationWizard} from './CalibrationWizard';
import {calibrationService, type CalibrationEndpoint} from '../../services/calibrationService';
import type {
  CalibrationSnapshot,
  EndpointCalibration,
  LoudnessMatchMode,
  LoudnessMatchSet,
} from '../../types';

/**
 * Volume calibration — measure each endpoint's loudness, then hold grouped rooms level with each
 * other.
 *
 * TWO SEPARATE QUESTIONS, deliberately laid out as two blocks. A curve says how loud ONE endpoint
 * is; the scope says WHICH endpoints are locked to each other. Conflating them would make the
 * kitchen/living-room case the only case — adding an office to the same stream would silently drag
 * it into a match nobody asked for.
 *
 * Records are read MERGED across the mesh but written to this unit. A record can only be saved to
 * the unit serving this page (a peer's config API is not reachable cross-origin), yet the endpoint
 * it describes may be grouped on another unit entirely — so the list shows everyone's records and
 * the matcher merges the same way. Which page you opened must not change what you see.
 */

const SCOPE_LABELS: Record<LoudnessMatchMode, {title: string; blurb: string}> = {
  off: {
    title: 'Off',
    blurb: 'Curves are kept and shown, but no endpoint volume is ever driven automatically.',
  },
  follow: {
    title: 'Rooms that follow each other',
    blurb:
      'Only units already slaved together under Playback → Follow. Acts exactly where you have '
      + 'declared two rooms locked, and nowhere else. Recommended.',
  },
  stream: {
    title: 'Everything sharing a stream',
    blurb:
      'Any calibrated endpoints playing the same source track each other, however they got there.',
  },
  sets: {
    title: 'Chosen groups',
    blurb:
      'Endpoints you group by hand below. Use this when rooms should track each other without one '
      + 'following the other’s source.',
  },
};

/**
 * Group endpoints that should track each other without one following the other's source.
 *
 * An endpoint may sit in several groups only by mistake, so the backend claims it for the FIRST
 * group that holds it and ignores the rest — a misconfiguration cannot produce two conflicting
 * targets for one speaker. The checkbox list is per group rather than a single assignment so that
 * rule stays visible rather than hidden behind a UI that pretends it cannot happen.
 */
const MatchSetEditor: React.FC<{
  sets: LoudnessMatchSet[];
  endpoints: CalibrationEndpoint[];
  busy: boolean;
  onChange: (sets: LoudnessMatchSet[]) => void;
}> = ({sets, endpoints, busy, onChange}) => {
  const addSet = () => {
    // Ids only have to be unique and stable within this list; the name is what the user reads.
    const id = `set-${Date.now().toString(36)}`;
    onChange([...sets, {id, name: `Group ${sets.length + 1}`, members: []}]);
  };

  const patch = (id: string, next: Partial<LoudnessMatchSet>) =>
    onChange(sets.map((s) => (s.id === id ? {...s, ...next} : s)));

  const toggle = (set: LoudnessMatchSet, playerId: string) =>
    patch(set.id, {
      members: set.members.includes(playerId)
        ? set.members.filter((m) => m !== playerId)
        : [...set.members, playerId],
    });

  return (
    <div className="mt-3 space-y-3">
      {sets.length === 0 && (
        <p className="rounded-md bg-amber-500/10 p-2 text-xs text-amber-200">
          No groups yet, so nothing is being matched. Add one and tick the rooms that belong together.
        </p>
      )}

      {sets.map((set) => (
        <div key={set.id} className="rounded-lg border border-white/10 p-3">
          <div className="flex items-center gap-2">
            <input
              value={set.name}
              onChange={(e) => patch(set.id, {name: e.target.value})}
              placeholder="Group name"
              className="flex-1 rounded-md bg-[var(--bg-secondary)] px-2 py-1 text-sm text-[var(--text-primary)]"
            />
            <button
              onClick={() => onChange(sets.filter((s) => s.id !== set.id))}
              disabled={busy}
              className="text-[var(--text-secondary)] hover:text-red-400 disabled:opacity-40"
              aria-label={`Remove ${set.name}`}
            >
              <Icon name="trash" className="h-4 w-4" />
            </button>
          </div>

          <div className="mt-2 space-y-1">
            {endpoints.map((endpoint) => (
              <label key={endpoint.playerId}
                     className="flex items-center gap-2 text-xs text-[var(--text-secondary)]">
                <input
                  type="checkbox"
                  checked={set.members.includes(endpoint.playerId)}
                  onChange={() => toggle(set, endpoint.playerId)}
                  disabled={busy}
                />
                {endpoint.name}
                <span className="text-[var(--text-secondary)]/60">({endpoint.unitName})</span>
              </label>
            ))}
          </div>

          {set.members.length === 1 && (
            <p className="mt-2 text-xs text-amber-300">
              A group needs at least two endpoints to match anything.
            </p>
          )}
        </div>
      ))}

      <button onClick={addSet} disabled={busy} className="text-sm text-[var(--accent-color)] disabled:opacity-40">
        + Add a group
      </button>
    </div>
  );
};

export const CalibrationSection: React.FC = () => {
  const [snapshot, setSnapshot] = useState<CalibrationSnapshot | null>(null);
  const [merged, setMerged] = useState<Record<string, EndpointCalibration>>({});
  const [endpoints, setEndpoints] = useState<CalibrationEndpoint[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<CalibrationEndpoint | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [snap, mergedRecords, list] = await Promise.all([
        calibrationService.getSnapshot(),
        calibrationService.getMerged().catch(() => ({})),
        calibrationService.getEndpoints().catch(() => []),
      ]);
      setSnapshot(snap);
      // The local snapshot wins for anything it holds: it is what a save just wrote, and the merged
      // read is a poll behind it.
      setMerged({...mergedRecords, ...snap.calibrations});
      setEndpoints(list);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not read calibration settings');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  const setMode = async (mode: LoudnessMatchMode) => {
    if (!snapshot) return;
    setBusy('policy');
    try {
      setSnapshot(await calibrationService.setPolicy({...snapshot.policy, mode}));
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not change the matching scope');
    } finally {
      setBusy(null);
    }
  };

  const savePolicySets = async (sets: LoudnessMatchSet[]) => {
    if (!snapshot) return;
    setBusy('policy');
    try {
      setSnapshot(await calibrationService.setPolicy({...snapshot.policy, sets}));
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not save the groups');
    } finally {
      setBusy(null);
    }
  };

  const handleDelete = async (endpoint: CalibrationEndpoint) => {
    setBusy(endpoint.playerId);
    try {
      await calibrationService.remove(endpoint.playerId);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not clear the calibration');
    } finally {
      setBusy(null);
    }
  };

  const calibratedCount = useMemo(
    () => endpoints.filter((e) => merged[e.playerId]?.calibrated).length,
    [endpoints, merged],
  );

  if (loading) {
    return <p className="text-sm text-[var(--text-secondary)]">Loading calibration…</p>;
  }

  return (
    <div className="space-y-5">
      <div>
        <h3 className="text-base font-semibold text-[var(--text-primary)]">Volume Calibration</h3>
        <p className="text-sm text-[var(--text-secondary)]">
          Measure how loud each endpoint actually is in its room, then hold grouped rooms at the same
          loudness. Two endpoints at the same percentage are rarely the same volume in the air.
        </p>
      </div>

      {error && <div className="rounded-lg bg-red-500/10 p-3 text-sm text-red-300">{error}</div>}

      {/* Scope — a separate question from any individual curve. */}
      <div className="rounded-lg bg-[var(--bg-primary)] p-4">
        <p className="text-sm font-medium text-[var(--text-primary)]">Which endpoints track each other</p>
        <div className="mt-3 space-y-2">
          {(snapshot?.modes ?? []).map((mode) => {
            const copy = SCOPE_LABELS[mode];
            const active = snapshot?.policy.mode === mode;
            return (
              <button
                key={mode}
                onClick={() => void setMode(mode)}
                disabled={busy === 'policy'}
                className={`w-full rounded-lg border p-3 text-left transition ${
                  active
                    ? 'border-[var(--accent-color)] bg-[var(--accent-color)]/10'
                    : 'border-white/10 hover:border-white/20'
                }`}
              >
                <span className="flex items-center gap-2 text-sm font-medium text-[var(--text-primary)]">
                  <span className={`h-2 w-2 shrink-0 rounded-full ${
                    active ? 'bg-[var(--accent-color)]' : 'bg-white/20'
                  }`} />
                  {copy.title}
                </span>
                <span className="mt-0.5 block text-xs text-[var(--text-secondary)]">{copy.blurb}</span>
              </button>
            );
          })}
        </div>

        {snapshot?.policy.mode === 'sets' && (
          <MatchSetEditor
            sets={snapshot.policy.sets}
            endpoints={endpoints}
            busy={busy === 'policy'}
            onChange={(sets) => void savePolicySets(sets)}
          />
        )}

        {snapshot?.policy.mode !== 'off' && calibratedCount < 2 && (
          <p className="mt-3 text-xs text-[var(--text-secondary)]">
            Matching needs at least two calibrated endpoints in the same group. {calibratedCount} so far.
          </p>
        )}
      </div>

      {/* Per-endpoint curves. */}
      <div className="space-y-2">
        {endpoints.length === 0 && (
          <p className="rounded-lg bg-amber-500/10 p-3 text-sm text-amber-200">
            No endpoints found. They appear here once a speaker is connected to the mesh.
          </p>
        )}

        {endpoints.map((endpoint) => {
          const cal = merged[endpoint.playerId];
          const atLimit = calibrationService.isAtLimit(cal, endpoint.volume);
          return (
            <div key={endpoint.playerId}
                 className="flex items-center gap-3 rounded-lg bg-[var(--bg-primary)] p-3">
              <Icon
                name="volume-high"
                className={`h-5 w-5 shrink-0 ${
                  endpoint.connected ? 'text-[var(--accent-color)]' : 'text-[var(--text-secondary)]'
                }`}
              />
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="truncate text-sm font-medium text-[var(--text-primary)]">
                    {endpoint.name}
                  </span>
                  {cal?.calibrated ? (
                    <span className="rounded-full bg-green-500/20 px-2 py-0.5 text-xs text-green-300">
                      Calibrated
                    </span>
                  ) : (
                    <span className="rounded-full bg-white/10 px-2 py-0.5 text-xs text-[var(--text-secondary)]">
                      Not calibrated
                    </span>
                  )}
                  {cal?.curve?.suspect && (
                    <span className="rounded-full bg-amber-500/20 px-2 py-0.5 text-xs text-amber-300"
                          title={`Measurements scatter by ${cal.curve.rmsError.toFixed(1)} dB around the fit`}>
                      Check measurements
                    </span>
                  )}
                  {atLimit && (
                    <span className="rounded-full bg-orange-500/20 px-2 py-0.5 text-xs text-orange-300"
                          title="This endpoint is at its ceiling and cannot get louder to match the group">
                      At limit
                    </span>
                  )}
                </div>
                <p className="truncate text-xs text-[var(--text-secondary)]">
                  {endpoint.unitName} · {calibrationService.summarize(cal)}
                </p>
              </div>

              <button
                onClick={() => setEditing(endpoint)}
                className="rounded-md bg-[var(--accent-color)] px-3 py-1.5 text-sm font-medium text-white"
              >
                {cal?.calibrated ? 'Edit' : 'Calibrate'}
              </button>
              {cal && (
                <button
                  onClick={() => void handleDelete(endpoint)}
                  disabled={busy === endpoint.playerId}
                  className="text-[var(--text-secondary)] hover:text-red-400 disabled:opacity-40"
                  aria-label={`Clear calibration for ${endpoint.name}`}
                >
                  <Icon name="trash" className="h-4 w-4" />
                </button>
              )}
            </div>
          );
        })}
      </div>

      {editing && snapshot && (
        <CalibrationWizard
          endpoint={editing}
          existing={merged[editing.playerId]}
          suggestedVolumes={snapshot.suggestedVolumes}
          minSamples={snapshot.minSamples}
          maxSamples={snapshot.maxSamples}
          onSaved={() => { setEditing(null); void refresh(); }}
          onClose={() => setEditing(null)}
        />
      )}
    </div>
  );
};
