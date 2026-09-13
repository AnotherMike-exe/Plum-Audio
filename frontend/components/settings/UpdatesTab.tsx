import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Icon } from '../Icon';
import {
  updateService,
  deriveState,
  shortDigest,
  type UnitUpdateState,
  type UnitUpdateStatus,
} from '../../services/updateService';

/**
 * Settings -> Updates.
 *
 * Pulls a new image and recreates the container, per unit, through the host agent. The GUI never
 * touches Docker — it writes a request the agent consumes (see backend/scripts/updater.py).
 *
 * WHY A CHECKBOX LIST AND NOT ONE BUTTON. CLAUDE.md records that there is no rolling upgrade: a 9.x
 * client cannot reach a 6.0.5 server, so a fleet split across a protocol major cannot mesh at all.
 * That argues for updating everything at once. But testing a new image on one unit before committing
 * the house to it is the more common need, and the cost of getting that wrong is a room that goes
 * quiet. So the page offers both, defaults to THIS unit alone, and warns when a selection would
 * leave peers behind.
 */

interface MeshUnit {
  unit_id: string;
  name: string;
  host: string;
}

const STATE_LABEL: Record<UnitUpdateState, string> = {
  'unreachable': 'Unreachable',
  'no-agent': 'No agent',
  'unknown': 'Unknown',
  'up-to-date': 'Up to date',
  'update-available': 'Update available',
  'pulling': 'Pulling',
  'restarting': 'Restarting',
  'failed': 'Failed',
};

const STATE_CLASS: Record<UnitUpdateState, string> = {
  'unreachable': 'text-[var(--text-muted)]',
  'no-agent': 'text-amber-500',
  'unknown': 'text-[var(--text-muted)]',
  'up-to-date': 'text-green-500',
  'update-available': 'text-[var(--accent-color)]',
  'pulling': 'text-[var(--accent-color)]',
  'restarting': 'text-[var(--accent-color)]',
  'failed': 'text-red-500',
};

export const UpdatesTab: React.FC = () => {
  const [units, setUnits] = useState<MeshUnit[]>([]);
  const [localUnitId, setLocalUnitId] = useState<string | null>(null);
  const [statuses, setStatuses] = useState<Map<string, UnitUpdateStatus | null>>(new Map());
  // Overrides the derived state while a run is in flight, so a row can read "Restarting" during the
  // window where the unit answers nothing at all.
  const [liveState, setLiveState] = useState<Map<string, UnitUpdateState>>(new Map());
  const [messages, setMessages] = useState<Map<string, string>>(new Map());
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [channel, setChannel] = useState('dev');
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  // The unit list comes from the aggregated mesh view the rest of the GUI already polls. Every unit
  // carries the host this page needs to address it directly on :5001.
  useEffect(() => {
    let cancelled = false;
    fetch(updateService.viewUrl())
      .then((r) => r.json())
      .then((d) => {
        if (cancelled) return;
        const all: MeshUnit[] = d.units ?? [];
        setUnits(all);
        setLocalUnitId(d.local_unit_id ?? null);
        updateService.setUnits(all, d.local_unit_id ?? null);
        // Default to THIS unit alone. Testing an image on one room before the house follows is the
        // safer habit, and it is the one the operator asked for.
        if (d.local_unit_id) setSelected(new Set([d.local_unit_id]));
      })
      .catch(() => {})
      .finally(() => !cancelled && setLoaded(true));
    return () => {
      cancelled = true;
    };
  }, []);

  const refresh = useCallback(async () => {
    if (units.length === 0) return;
    const next = await updateService.statusAll(units.map((u) => u.unit_id));
    setStatuses(next);
  }, [units]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Stop any in-flight polling if the tab unmounts — the overlay closes mid-update often enough
  // that a leaked poll loop would keep fetching a peer forever.
  useEffect(() => () => abortRef.current?.abort(), []);

  const stateOf = (unitId: string): UnitUpdateState =>
    liveState.get(unitId) ?? deriveState(statuses.get(unitId) ?? null);

  const setRow = (unitId: string, state: UnitUpdateState, message?: string) => {
    setLiveState((prev) => new Map(prev).set(unitId, state));
    if (message !== undefined) setMessages((prev) => new Map(prev).set(unitId, message));
  };

  const toggle = (unitId: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(unitId)) next.delete(unitId);
      else next.add(unitId);
      return next;
    });
  };

  const selectAll = () => setSelected(new Set(units.map((u) => u.unit_id)));
  const selectNone = () => setSelected(new Set());

  /** Units left behind by this selection that are NOT already on the same image as the selection. */
  const strandedWarning = useMemo(() => {
    if (selected.size === 0 || selected.size === units.length) return null;
    const left = units.filter((u) => !selected.has(u.unit_id));
    const reachable = left.filter((u) => {
      const s = statuses.get(u.unit_id);
      return s != null && s.agent.installed;
    });
    if (reachable.length === 0) return null;
    return `${reachable.length} other unit${reachable.length === 1 ? '' : 's'} will stay on the current image. A mesh split across an aiosendspin major cannot sync at all.`;
  }, [selected, units, statuses]);

  const run = async (checkOnly: boolean) => {
    if (selected.size === 0 || busy) return;
    setBusy(true);
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setMessages(new Map());

    const ids = units.filter((u) => selected.has(u.unit_id)).map((u) => u.unit_id);

    // Serially, not in parallel. Updating a whole mesh at once means every room goes quiet
    // together, and a failure part-way leaves no reference unit still serving a page to look at.
    for (const unitId of ids) {
      if (controller.signal.aborted) break;
      setRow(unitId, checkOnly ? 'pulling' : 'restarting', '');
      const sent = await updateService.apply(unitId, channel, { checkOnly });
      if (!sent.ok) {
        setRow(unitId, 'failed', sent.message ?? 'request refused');
        continue;
      }
      if (checkOnly) {
        // A check never restarts anything, so the unit stays up and one settle pass is enough.
        await new Promise((r) => setTimeout(r, 2500));
        const status = await updateService.status(unitId);
        setStatuses((prev) => new Map(prev).set(unitId, status));
        setLiveState((prev) => {
          const next = new Map(prev);
          next.delete(unitId);
          return next;
        });
        continue;
      }
      const final = await updateService.pollUntilSettled(
        unitId,
        (state, status) => {
          setRow(unitId, state);
          if (status) setStatuses((prev) => new Map(prev).set(unitId, status));
        },
        { signal: controller.signal },
      );
      const status = await updateService.status(unitId);
      setStatuses((prev) => new Map(prev).set(unitId, status));
      const note = status?.lastUpdate?.message;
      if (note) setMessages((prev) => new Map(prev).set(unitId, note));
      if (final !== 'failed' && final !== 'unreachable') {
        setLiveState((prev) => {
          const next = new Map(prev);
          next.delete(unitId);
          return next;
        });
      }
    }
    setBusy(false);
    void refresh();
  };

  const anyAgentMissing = units.some((u) => statuses.get(u.unit_id)?.agent.installed === false);

  return (
    <div className="space-y-6">
      <div>
        <h3 className="text-base font-semibold text-[var(--text-primary)] mb-1">Updates</h3>
        <p className="text-sm text-[var(--text-muted)]">
          Pull a new image and restart the selected units. A unit that is already current is left
          playing — nothing restarts unless there is something new to install.
        </p>
      </div>

      {/* Channel */}
      <div className="p-4 bg-[var(--bg-tertiary)] rounded-lg border border-[var(--border-color)]">
        <label className="block text-sm font-medium text-[var(--text-primary)] mb-2">Channel</label>
        <div className="flex gap-2">
          {['dev', 'latest'].map((c) => (
            <button
              key={c}
              onClick={() => setChannel(c)}
              disabled={busy}
              className={`px-3 py-1.5 rounded-md text-sm font-medium transition-opacity disabled:opacity-50 ${
                channel === c
                  ? 'bg-[var(--accent-color)] accent-button-text'
                  : 'bg-[var(--bg-secondary)] text-[var(--text-primary)] border border-[var(--border-color)]'
              }`}
            >
              {c}
            </button>
          ))}
        </div>
        <p className="mt-2 text-xs text-[var(--text-muted)]">
          <span className="font-mono">dev</span> tracks every push to the dev branch.{' '}
          <span className="font-mono">latest</span> is the last release.
        </p>
      </div>

      {/* Units */}
      <div className="p-4 bg-[var(--bg-tertiary)] rounded-lg border border-[var(--border-color)]">
        <div className="flex items-center justify-between mb-3">
          <h4 className="text-sm font-semibold text-[var(--text-primary)]">
            Units ({selected.size} of {units.length} selected)
          </h4>
          <div className="flex gap-2">
            <button
              onClick={selectAll}
              disabled={busy}
              className="px-2 py-1 text-xs rounded border border-[var(--border-color)] text-[var(--text-primary)] hover:opacity-80 disabled:opacity-50"
            >
              All
            </button>
            <button
              onClick={selectNone}
              disabled={busy}
              className="px-2 py-1 text-xs rounded border border-[var(--border-color)] text-[var(--text-primary)] hover:opacity-80 disabled:opacity-50"
            >
              None
            </button>
          </div>
        </div>

        {!loaded && <p className="text-sm text-[var(--text-muted)]">Loading units…</p>}
        {loaded && units.length === 0 && (
          <p className="text-sm text-[var(--text-muted)]">No units in the mesh view.</p>
        )}

        <div className="space-y-2">
          {units.map((u) => {
            const status = statuses.get(u.unit_id) ?? null;
            const state = stateOf(u.unit_id);
            const message = messages.get(u.unit_id);
            const isLocal = u.unit_id === localUnitId;
            return (
              <div
                key={u.unit_id}
                className="flex items-start gap-3 p-2 rounded-md bg-[var(--bg-secondary)] border border-[var(--border-color)]"
              >
                <input
                  type="checkbox"
                  checked={selected.has(u.unit_id)}
                  onChange={() => toggle(u.unit_id)}
                  disabled={busy}
                  aria-label={`Select ${u.name}`}
                  className="mt-1 accent-[var(--accent-color)]"
                />
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-sm font-medium text-[var(--text-primary)] truncate">
                      {u.name}
                    </span>
                    {isLocal && (
                      <span className="text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded bg-[var(--bg-tertiary)] text-[var(--text-muted)]">
                        this unit
                      </span>
                    )}
                    <span className={`text-xs font-medium ${STATE_CLASS[state]}`}>
                      {STATE_LABEL[state]}
                    </span>
                  </div>
                  <div className="text-xs text-[var(--text-muted)] font-mono truncate">
                    {u.host}
                    {status?.running?.version ? ` · v${status.running.version}` : ''}
                    {` · ${shortDigest(status?.digest)}`}
                  </div>
                  {message && (
                    <p
                      className={`text-xs mt-1 ${state === 'failed' ? 'text-red-500' : 'text-[var(--text-muted)]'}`}
                    >
                      {message}
                    </p>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {anyAgentMissing && (
        <div className="p-3 rounded-lg border border-amber-500/40 bg-amber-500/10">
          <p className="text-xs text-amber-500">
            <Icon name="circle-info" className="inline mr-1" />
            A unit reports no update agent. Its host needs{' '}
            <span className="font-mono">scripts/host-setup/provision.sh</span>, which is once per Pi
            image. That unit still updates by hand with{' '}
            <span className="font-mono">docker compose pull &amp;&amp; up -d</span>.
          </p>
        </div>
      )}

      {strandedWarning && (
        <div className="p-3 rounded-lg border border-amber-500/40 bg-amber-500/10">
          <p className="text-xs text-amber-500">
            <Icon name="circle-info" className="inline mr-1" />
            {strandedWarning}
          </p>
        </div>
      )}

      <div className="flex items-center gap-3">
        <button
          onClick={() => run(true)}
          disabled={busy || selected.size === 0}
          className="px-4 py-2 rounded-md text-sm font-medium border border-[var(--border-color)] text-[var(--text-primary)] hover:opacity-80 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          Check for updates
        </button>
        <button
          onClick={() => run(false)}
          disabled={busy || selected.size === 0}
          className="px-4 py-2 bg-[var(--accent-color)] accent-button-text rounded-md text-sm font-medium hover:opacity-90 disabled:opacity-50 disabled:cursor-not-allowed transition-opacity"
        >
          {busy ? 'Working…' : `Update ${selected.size} unit${selected.size === 1 ? '' : 's'}`}
        </button>
      </div>

      <p className="text-xs text-[var(--text-muted)]">
        Units update one at a time, so a failure leaves the rest of the house playing. A unit is
        unreachable for about ten seconds while its container restarts — that is the update working,
        not a fault.
      </p>
    </div>
  );
};
