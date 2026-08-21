import React, {useCallback, useEffect, useMemo, useRef, useState} from 'react';
import {Icon} from '../Icon';
import {Switch} from '../Switch';
import {calibrationService, type CalibrationDraft, type CalibrationEndpoint} from '../../services/calibrationService';
import type {EndpointCalibration} from '../../types';

/**
 * Measure one endpoint's loudness curve.
 *
 * ONE SCREEN, not a stepped wizard. The measurement is inherently a table — a few volumes, a dB
 * reading against each — and a user walking between a speaker and a phone meter needs to re-measure
 * a row out of order far more often than they need to be marched through a sequence. The
 * predecessor's three-step flow also hard-coded exactly two points at 38% and 80%; here any row's
 * volume is editable and there may be two to five of them.
 *
 * PLAY ROUTES THE ENDPOINT. Pressing play pulls this speaker off whatever it was playing onto a
 * transient calibration source, and stopping puts it back. That is not a side effect to hide: it is
 * the only way the tone passes through the endpoint's own gain stage, which is the thing being
 * measured. The banner says so, because a user who does not expect it will think something broke.
 *
 * The fit is NOT computed here. Everything derived — the curve, whether it is trustworthy, the
 * resolved ceiling — comes back from the save, so there is exactly one implementation of the maths
 * and the GUI cannot disagree with the matcher about how loud a room is.
 */

interface CalibrationWizardProps {
  endpoint: CalibrationEndpoint;
  existing?: EndpointCalibration;
  suggestedVolumes: number[];
  minSamples: number;
  maxSamples: number;
  onSaved: (saved: EndpointCalibration) => void;
  onClose: () => void;
}

interface Row {
  volume: number;
  db: string; // free text while typing; parsed on save
}

const TONE_SECONDS = 180;

export const CalibrationWizard: React.FC<CalibrationWizardProps> = ({
  endpoint,
  existing,
  suggestedVolumes,
  minSamples,
  maxSamples,
  onSaved,
  onClose,
}) => {
  const [rows, setRows] = useState<Row[]>(() => {
    if (existing?.samples?.length) {
      return existing.samples.map((s) => ({volume: s.volume, db: String(s.db)}));
    }
    return suggestedVolumes.slice(0, Math.max(minSamples, 3)).map((v) => ({volume: v, db: ''}));
  });
  const [toneType, setToneType] = useState<'pink' | 'sine'>('pink');
  const [playingRow, setPlayingRow] = useState<number | null>(null);
  const [maxMode, setMaxMode] = useState<'percentage' | 'decibel'>(existing?.maxLimit?.mode ?? 'percentage');
  const [maxValue, setMaxValue] = useState<number>(existing?.maxLimit?.value ?? 100);
  const [trimDb, setTrimDb] = useState<number>(existing?.trimDb ?? 0);
  const [enabled, setEnabled] = useState<boolean>(existing?.enabled ?? true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /**
   * For an idle third-party speaker `endpoint.playerId` is only its listener URL — mDNS gives
   * nothing else. Starting the tone ADOPTS it, and the reply carries the id its handshake gave
   * (generally a MAC). That is what the record must be keyed on: a URL is IP-derived and moves with
   * DHCP, so a curve saved against one would be orphaned by the next lease.
   */
  const [resolvedId, setResolvedId] = useState<string | null>(null);

  // The tone lives on the server and expires on its own, but a wizard that unmounts while it is
  // playing would leave a speaker making noise until that timeout. Stop it on the way out.
  const playingRef = useRef<number | null>(null);
  playingRef.current = playingRow;
  useEffect(() => () => {
    if (playingRef.current !== null) void calibrationService.toneStop();
  }, []);

  const stopTone = useCallback(async () => {
    setPlayingRow(null);
    try {
      await calibrationService.toneStop();
    } catch {
      /* stopping is best-effort: the tone expires on its own regardless */
    }
  }, []);

  const playRow = useCallback(async (index: number) => {
    setError(null);
    if (playingRow === index) {
      await stopTone();
      return;
    }
    try {
      // Re-level rather than restart when a tone is already up: restarting gaps the noise while
      // the meter is still integrating, which is exactly when a reading goes wrong.
      if (playingRow !== null) {
        await calibrationService.toneVolume(rows[index].volume);
      } else {
        const state = await calibrationService.toneStart(endpoint.playerId, rows[index].volume, {
          type: toneType,
          seconds: TONE_SECONDS,
          url: endpoint.idle ? endpoint.url : undefined,
        });
        if (state.playerId && state.playerId !== endpoint.playerId) setResolvedId(state.playerId);
      }
      setPlayingRow(index);
    } catch (e) {
      setPlayingRow(null);
      setError(e instanceof Error ? e.message : 'Could not play the calibration tone');
    }
  }, [endpoint.playerId, playingRow, rows, stopTone, toneType]);

  const updateRow = (index: number, patch: Partial<Row>) => {
    setRows((prev) => prev.map((row, i) => (i === index ? {...row, ...patch} : row)));
  };

  const addRow = () => {
    if (rows.length >= maxSamples) return;
    const used = new Set(rows.map((r) => r.volume));
    const next = suggestedVolumes.find((v) => !used.has(v))
      ?? Math.min(95, Math.max(...rows.map((r) => r.volume)) + 15);
    setRows((prev) => [...prev, {volume: next, db: ''}]);
  };

  const removeRow = async (index: number) => {
    if (rows.length <= minSamples) return;
    if (playingRow === index) await stopTone();
    setRows((prev) => prev.filter((_, i) => i !== index));
  };

  const parsed = useMemo(
    () => rows
      .map((row) => ({volume: row.volume, db: Number.parseFloat(row.db)}))
      .filter((row) => Number.isFinite(row.db)),
    [rows],
  );
  const complete = parsed.length >= minSamples && parsed.length === rows.length;
  // An idle third-party speaker has no real id until the tone has adopted it once, and a record
  // keyed on its URL would be orphaned by the next DHCP lease. Playing any row resolves it.
  const needsAdoption = endpoint.idle && resolvedId === null;

  const handleSave = async () => {
    setSaving(true);
    setError(null);
    if (playingRow !== null) await stopTone();
    const draft: CalibrationDraft = {
      name: endpoint.name,
      url: endpoint.url,
      enabled,
      samples: parsed,
      maxLimit: {mode: maxMode, value: maxValue},
      trimDb,
    };
    try {
      onSaved(await calibrationService.save(resolvedId ?? endpoint.playerId, draft));
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not save the calibration');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <div className="w-full max-w-lg max-h-[90vh] overflow-y-auto rounded-xl bg-[var(--bg-secondary)] p-6 shadow-xl">
        <div className="mb-1 flex items-start justify-between gap-4">
          <div>
            <h3 className="text-lg font-semibold text-[var(--text-primary)]">Calibrate {endpoint.name}</h3>
            <p className="text-sm text-[var(--text-secondary)]">{endpoint.unitName}</p>
          </div>
          <button onClick={onClose} className="text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
                  aria-label="Close">
            <Icon name="xmark" className="h-5 w-5" />
          </button>
        </div>

        <div className="my-4 rounded-lg bg-blue-500/10 p-3 text-sm text-blue-200">
          <p className="font-medium">Before you start</p>
          <ul className="mt-1 list-disc space-y-0.5 pl-4 text-blue-200/80">
            <li>Stop other music and keep the room quiet — the meter hears everything.</li>
            <li>Measure from where you actually listen, in the same spot every time.</li>
            <li>Playing the tone moves this speaker off whatever it was playing. It goes back when you stop.</li>
            {endpoint.foreign && (
              <li>
                This is not a Plum speaker. We can command its volume but cannot read it back, so we
                will not restore its level afterwards — set it where you want it when you are done.
                If you hear nothing at all, check it plays from its own server first.
              </li>
            )}
          </ul>
        </div>

        <label className="mb-4 flex items-center gap-3 text-sm text-[var(--text-secondary)]">
          Tone
          <select
            value={toneType}
            onChange={(e) => setToneType(e.target.value as 'pink' | 'sine')}
            disabled={playingRow !== null}
            className="rounded-md bg-[var(--bg-primary)] px-2 py-1 text-[var(--text-primary)] disabled:opacity-50"
          >
            <option value="pink">Pink noise (recommended)</option>
            <option value="sine">1 kHz sine</option>
          </select>
        </label>

        <div className="space-y-2">
          {rows.map((row, index) => (
            <div key={index} className="flex items-center gap-2 rounded-lg bg-[var(--bg-primary)] p-2">
              <button
                onClick={() => void playRow(index)}
                className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-full ${
                  playingRow === index ? 'bg-red-500 text-white' : 'bg-[var(--accent-color)] text-white'
                }`}
                aria-label={playingRow === index ? 'Stop tone' : 'Play tone'}
              >
                <Icon name={playingRow === index ? 'stop' : 'play'} className="h-4 w-4" />
              </button>

              <label className="flex items-center gap-1 text-sm text-[var(--text-secondary)]">
                <input
                  type="number"
                  min={1}
                  max={100}
                  value={row.volume}
                  onChange={(e) => updateRow(index, {volume: Number(e.target.value)})}
                  className="w-16 rounded-md bg-[var(--bg-secondary)] px-2 py-1 text-right text-[var(--text-primary)]"
                />
                %
              </label>

              <label className="ml-auto flex items-center gap-1 text-sm text-[var(--text-secondary)]">
                <input
                  type="number"
                  step="0.1"
                  placeholder="e.g. 62"
                  value={row.db}
                  onChange={(e) => updateRow(index, {db: e.target.value})}
                  className="w-24 rounded-md bg-[var(--bg-secondary)] px-2 py-1 text-right text-[var(--text-primary)]"
                />
                dB
              </label>

              <button
                onClick={() => void removeRow(index)}
                disabled={rows.length <= minSamples}
                className="text-[var(--text-secondary)] hover:text-red-400 disabled:opacity-30"
                aria-label="Remove measurement"
              >
                <Icon name="trash" className="h-4 w-4" />
              </button>
            </div>
          ))}
        </div>

        <button
          onClick={addRow}
          disabled={rows.length >= maxSamples}
          className="mt-2 text-sm text-[var(--accent-color)] disabled:opacity-40"
        >
          + Add a measurement ({rows.length}/{maxSamples})
        </button>

        <div className="mt-5 space-y-4 border-t border-white/10 pt-4">
          <div>
            <p className="text-sm font-medium text-[var(--text-primary)]">Maximum output</p>
            <p className="mb-2 text-xs text-[var(--text-secondary)]">
              How far matching may push this endpoint. In dB mode the limit is resolved through this
              speaker&apos;s own curve, so one number means the same loudness in every room.
            </p>
            <div className="flex items-center gap-2">
              <div className="flex overflow-hidden rounded-md">
                {(['percentage', 'decibel'] as const).map((mode) => (
                  <button
                    key={mode}
                    onClick={() => {
                      setMaxMode(mode);
                      setMaxValue(mode === 'percentage' ? 100 : 75);
                    }}
                    className={`px-3 py-1 text-sm ${
                      maxMode === mode
                        ? 'bg-[var(--accent-color)] text-white'
                        : 'bg-[var(--bg-primary)] text-[var(--text-secondary)]'
                    }`}
                  >
                    {mode === 'percentage' ? '%' : 'dB'}
                  </button>
                ))}
              </div>
              <input
                type="number"
                value={maxValue}
                onChange={(e) => setMaxValue(Number(e.target.value))}
                className="w-24 rounded-md bg-[var(--bg-primary)] px-2 py-1 text-right text-[var(--text-primary)]"
              />
              <span className="text-sm text-[var(--text-secondary)]">{maxMode === 'percentage' ? '%' : 'dB'}</span>
            </div>
          </div>

          <div>
            <p className="text-sm font-medium text-[var(--text-primary)]">Level offset</p>
            <p className="mb-2 text-xs text-[var(--text-secondary)]">
              Persistent per-room taste, kept through every re-level — &ldquo;the kitchen is always a
              little quieter&rdquo;. Dragging a volume slider sets the whole group&apos;s target instead.
            </p>
            <div className="flex items-center gap-3">
              <input
                type="range"
                min={-12}
                max={12}
                step={1}
                value={trimDb}
                onChange={(e) => setTrimDb(Number(e.target.value))}
                className="flex-1"
              />
              <span className="w-16 text-right text-sm text-[var(--text-primary)]">
                {trimDb > 0 ? '+' : ''}{trimDb} dB
              </span>
            </div>
          </div>

          <Switch
            checked={enabled}
            onChange={setEnabled}
            label="Include in loudness matching"
            description="Off keeps the curve but leaves this endpoint's volume alone."
          />
        </div>

        {error && (
          <div className="mt-4 rounded-lg bg-red-500/10 p-3 text-sm text-red-300">{error}</div>
        )}

        <div className="mt-6 flex items-center justify-end gap-3">
          <button onClick={onClose} className="text-sm text-[var(--text-secondary)] hover:text-[var(--text-primary)]">
            Cancel
          </button>
          <button
            onClick={() => void handleSave()}
            disabled={!complete || saving || needsAdoption}
            className="rounded-md bg-[var(--accent-color)] px-4 py-2 text-sm font-medium text-white disabled:opacity-40"
          >
            {saving ? 'Saving…' : 'Save calibration'}
          </button>
        </div>
        {!complete && (
          <p className="mt-2 text-right text-xs text-[var(--text-secondary)]">
            Enter a dB reading for every row ({minSamples}–{maxSamples} measurements).
          </p>
        )}
        {complete && needsAdoption && (
          <p className="mt-2 text-right text-xs text-amber-300">
            Play the tone once first — this speaker has to identify itself before its calibration
            can be stored.
          </p>
        )}
      </div>
    </div>
  );
};
