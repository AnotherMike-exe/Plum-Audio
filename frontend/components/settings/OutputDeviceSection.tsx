import React, {useCallback, useEffect, useState} from 'react';
import {Icon} from '../Icon';
import {audioService, DeviceType, type AudioDevice, type CurrentOutput} from '../../services/audioService';

/**
 * Which speaker this unit renders to.
 *
 * The one thing this component must not do is claim success early. Choosing a device only WRITES
 * settings.json; the player applies it on its own poll (~5 s) and echoes back what it actually
 * opened. So after a save we keep polling until the echo matches, and show the row as "Switching…"
 * until it does. If the device turns out not to open, the player restores the previous one and the
 * echo never moves — that state is drawn as a warning naming the device still in use, rather than
 * as a completed change. A picker that goes green while the audio is somewhere else is the exact
 * failure this slice was built to remove.
 *
 * There are TWO kinds of pending and they must not look alike. A device-to-device switch applies
 * live and resolves on its own, so it gets the spinner. Crossing to or from "No output" does not:
 * the player is a PROCESS that either exists or does not, decided once at container start, so it
 * waits for a restart and gets a static warning. A spinner there would promise something that is
 * never going to happen on its own.
 */

const TYPE_BADGE: Record<DeviceType, string> = {
  [DeviceType.BUILTIN_HEADPHONES]: 'bg-blue-500/20 text-blue-300',
  [DeviceType.BUILTIN_HDMI]: 'bg-purple-500/20 text-purple-300',
  [DeviceType.USB]: 'bg-green-500/20 text-green-300',
  [DeviceType.HAT]: 'bg-orange-500/20 text-orange-300',
  [DeviceType.OTHER]: 'bg-gray-500/20 text-gray-300',
  [DeviceType.NONE]: 'bg-slate-500/20 text-slate-300',
};

// While a switch is in flight the echo is the only thing that moves, so poll a little faster than
// the player's own 5 s settings poll — otherwise the UI lags a change that already happened.
const PENDING_POLL_MS = 1500;
const IDLE_POLL_MS = 10000;

export const OutputDeviceSection: React.FC = () => {
  const [devices, setDevices] = useState<AudioDevice[]>([]);
  const [current, setCurrent] = useState<CurrentOutput | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [testing, setTesting] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [list, currentOutput] = await Promise.all([
        audioService.getOutputDevices(),
        audioService.getCurrentOutput(),
      ]);
      setDevices(list);
      setCurrent(currentOutput);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not read audio devices');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  // Poll faster while a switch is outstanding, then settle back down.
  useEffect(() => {
    const id = window.setInterval(() => { void refresh(); },
      current?.pending ? PENDING_POLL_MS : IDLE_POLL_MS);
    return () => window.clearInterval(id);
  }, [current?.pending, refresh]);

  const handleSelect = async (device: AudioDevice) => {
    if (!device.isAvailable || busyId) return;
    setBusyId(device.id);
    setNotice(null);
    try {
      await audioService.setOutputDevice(device.id);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not set the output device');
    } finally {
      setBusyId(null);
    }
  };

  const handleTest = async (device: AudioDevice, event: React.MouseEvent) => {
    event.stopPropagation();  // the row is a button; testing must not also select
    setTesting(device.id);
    setNotice(null);
    try {
      const result = await audioService.testOutputDevice(device.id);
      setNotice(result.message);
    } catch (e) {
      setNotice(e instanceof Error ? e.message : 'Test failed');
    } finally {
      setTesting(null);
    }
  };

  const playingOn = current?.playingOn ?? null;
  const configured = current?.configured ?? null;

  return (
    <div className="space-y-3">
      <div>
        <h3 className="text-base font-semibold text-[var(--text-primary)] mb-1">Audio Output</h3>
        <p className="text-sm text-[var(--text-muted)]">
          The speaker this unit plays through. Switching takes a few seconds and briefly interrupts
          any audio already playing. Choosing <span className="font-medium">No output</span> — or
          moving away from it — needs this unit to be restarted.
        </p>
      </div>

      {loading && (
        <p className="text-xs text-[var(--text-muted)]">Looking for audio devices…</p>
      )}

      {error && (
        <div className="p-3 bg-red-500/10 border border-red-500/20 rounded-lg flex items-start gap-2">
          <Icon name="circle-exclamation" className="text-red-400 mt-0.5 flex-shrink-0" />
          <div className="min-w-0">
            <p className="text-sm text-red-300">{error}</p>
            <button onClick={() => { void refresh(); }} className="mt-1 text-xs text-red-300 underline hover:text-red-200">
              Try again
            </button>
          </div>
        </div>
      )}

      {/* Saved, but it needs a restart to take effect. Static icon, not the spinner: nothing is in
          flight and nothing will resolve this on its own. */}
      {current?.pending && current.restartRequired && (
        <div className="p-3 bg-amber-500/10 border border-amber-500/20 rounded-lg flex items-start gap-2">
          <Icon name="triangle-exclamation" className="text-amber-400 mt-0.5 flex-shrink-0" />
          <p className="text-sm text-amber-300">
            Saved. Restart this unit to switch to <span className="font-medium">{current.friendlyName}</span> —
            the player is started or skipped when the unit boots. Until then it is{' '}
            {playingOn ? <>still playing through <span className="font-medium">{playingOn}</span></> : 'producing no output'}.
          </p>
        </div>
      )}

      {/* The configured device is not the one playing: either mid-switch, or the switch failed and
          the player fell back. Both must be visible — neither is success. */}
      {current?.pending && !current.restartRequired && (
        <div className="p-3 bg-yellow-500/10 border border-yellow-500/20 rounded-lg flex items-start gap-2">
          <Icon name="spinner" className="text-yellow-400 mt-0.5 flex-shrink-0 animate-spin" />
          <p className="text-sm text-yellow-300">
            Switching to <span className="font-medium">{current.friendlyName}</span> — still playing
            through <span className="font-medium">{playingOn}</span>. If this does not clear, that
            device could not be opened and playback was left where it was.
          </p>
        </div>
      )}

      {current && !current.resolved && configured && (
        <div className="p-3 bg-red-500/10 border border-red-500/20 rounded-lg flex items-start gap-2">
          <Icon name="triangle-exclamation" className="text-red-400 mt-0.5 flex-shrink-0" />
          <p className="text-sm text-red-300">
            The configured output <span className="font-mono">{configured}</span> is not attached to
            this unit. Pick another device below.
          </p>
        </div>
      )}

      {/* The "No output" row always comes back, so an EMPTY list no longer happens — "only that row"
          is what a host with no sound card looks like. Say what the unit can still do, because it is
          a great deal more than "no devices found" implies. */}
      {!loading && !error && devices.length > 0 && devices.every(d => d.type === DeviceType.NONE) && (
        <p className="text-xs text-[var(--text-muted)]">
          No playback hardware was detected on this unit. It can still receive AirPlay, Spotify and
          Bluetooth, and send them to other rooms.
        </p>
      )}

      <div className="space-y-2">
        {devices.map(device => {
          const isPlaying = playingOn === device.id;
          const isChosen = configured === device.id;
          const disabled = !device.isAvailable || busyId !== null;

          return (
            <div
              key={device.id}
              className={`rounded-lg border transition-all ${
                isChosen
                  ? 'bg-[var(--accent-color)]/10 border-[var(--accent-color)]'
                  : 'bg-[var(--bg-tertiary)] border-[var(--border-color)]'
              } ${device.isAvailable ? '' : 'opacity-60'}`}
            >
              <button
                type="button"
                onClick={() => { void handleSelect(device); }}
                disabled={disabled}
                aria-current={isPlaying}
                className={`w-full flex items-center gap-3 p-3 text-left ${
                  disabled ? 'cursor-not-allowed' : 'cursor-pointer hover:border-[var(--text-secondary)]'
                }`}
              >
                <Icon
                  name={audioService.getDeviceTypeIcon(device.type)}
                  className="text-[var(--text-secondary)] flex-shrink-0"
                />
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-sm font-medium text-[var(--text-primary)] truncate">
                      {device.friendlyName}
                    </span>
                    <span className={`px-2 py-0.5 rounded text-xs font-medium ${TYPE_BADGE[device.type]}`}>
                      {audioService.getDeviceTypeLabel(device.type)}
                    </span>
                    {isPlaying && (
                      <span className="text-xs text-green-400 font-medium">Playing</span>
                    )}
                    {isChosen && !isPlaying && (
                      current?.restartRequired
                        ? <span className="text-xs text-amber-400 font-medium">Restart to apply</span>
                        : <span className="text-xs text-yellow-400 font-medium">Switching…</span>
                    )}
                  </div>
                  {/* hw address is informational: it changes when cards renumber. The "No output"
                      row has none, and an empty mono line reads as a rendering bug. */}
                  {device.hwId && (
                    <span className="text-xs text-[var(--text-muted)] font-mono">{device.hwId}</span>
                  )}
                  {device.unavailableReason && (
                    <p className="text-xs text-[var(--text-muted)] mt-1">{device.unavailableReason}</p>
                  )}
                </div>
                {busyId === device.id && <Icon name="spinner" className="animate-spin text-[var(--text-secondary)]" />}
              </button>

              {/* Testing the device already in use returns EBUSY, so the backend refuses it with an
                  explanation — do not offer the button there at all. */}
              {device.isAvailable && !device.inUse && !isPlaying && device.type !== DeviceType.NONE && (
                <div className="px-3 pb-3 -mt-1">
                  <button
                    type="button"
                    onClick={e => { void handleTest(device, e); }}
                    disabled={testing !== null}
                    className="text-xs text-[var(--text-secondary)] underline hover:text-[var(--text-primary)] disabled:opacity-50"
                  >
                    {testing === device.id ? 'Playing test tone…' : 'Play test tone'}
                  </button>
                </div>
              )}
            </div>
          );
        })}
      </div>

      {notice && <p className="text-xs text-[var(--text-muted)]">{notice}</p>}
    </div>
  );
};
