import React, { useState } from 'react';
import { Icon } from '../Icon';

interface PairingTabProps {
  /** Ask every reachable unit to open its own speaker's pairing window. Absent = mesh unavailable. */
  onOpenPairingWindows?: () => Promise<{ opened: number; total: number; failed: string[] }>;
}

/**
 * Pairing — the fleet-wide actions, as opposed to pairing one device.
 *
 * Pairing an individual speaker lives where you route it (the Pair button on its row), because that
 * is where you discover it cannot play. What belongs *here* is the thing that is about the whole
 * mesh: opening every unit up so a NEW unit can join.
 *
 * Why a window at all, when units with a shared fleet secret pair automatically: the fleet secret
 * covers units deployed together. A unit added later, or a third-party speaker that needs a PIN,
 * has to be admitted deliberately — and the protocol's own answer is the `management` role, where a
 * server already paired with a device stands in for the physical gesture. Each unit does that for
 * its OWN speaker; this button just asks them all to, at once.
 *
 * The window's shape comes from aiosendspin, not from us: it lasts 300 s and admits exactly ONE
 * attempt, claimed and consumed by the first device to pair. Both halves belong in the copy —
 * "open for five minutes" alone would imply you can add three speakers on one click, and the second
 * and third would fail for a reason nothing on screen explained.
 */
export const PairingTab: React.FC<PairingTabProps> = ({ onOpenPairingWindows }) => {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string | null>(null);

  const openEverywhere = async () => {
    setBusy(true);
    setResult(null);
    const { opened, total, failed } = (await onOpenPairingWindows?.()) ?? { opened: 0, total: 0, failed: [] };
    setBusy(false);
    if (total === 0) {
      setResult('No units are reachable right now.');
    } else if (failed.length === 0) {
      setResult(`${opened} unit${opened === 1 ? '' : 's'} open for the next five minutes.`);
    } else {
      // A partial result is normal — a unit may be down — and is more useful than a bare failure,
      // because the operator can see whether the one they care about is ready.
      setResult(`${opened} of ${total} opened. Not reachable: ${failed.join(', ')}.`);
    }
  };

  return (
    <div className="space-y-6">
      <div>
        <h3 className="text-lg font-semibold text-[var(--text-primary)] mb-1">Add a unit or speaker</h3>
        <p className="text-sm text-[var(--text-secondary)]">
          Opens every Plum unit for five minutes to accept <strong>one</strong> new device each, so a
          newly deployed unit can join the mesh. Units deployed together already pair automatically —
          use this when you add one later. Adding two? Run it again for the second.
        </p>
      </div>

      <button
        onClick={() => void openEverywhere()}
        disabled={busy || !onOpenPairingWindows}
        className="w-full bg-[var(--accent-color)] accent-button-text font-bold py-3 px-4 rounded-lg hover:bg-[var(--accent-color-hover)] transition-colors disabled:opacity-50 flex items-center justify-center gap-2"
      >
        <Icon name="network-wired" style={{ color: 'inherit' }} />
        {busy ? 'Opening…' : 'Open the mesh for pairing'}
      </button>

      {result && <p className="text-sm text-[var(--text-secondary)]">{result}</p>}

      <div className="border-t border-[var(--border-color)] pt-4 text-xs text-[var(--text-secondary)] space-y-2">
        <p>
          <strong className="text-[var(--text-primary)]">Pairing one speaker</strong> is done where you
          route it: a device that has not been paired shows <em>Pair</em> instead of the stream
          controls, because routing it would silently play nothing.
        </p>
        <p>
          Speakers that connect without encryption — most ESP32 devices, and Music Assistant — never
          need pairing and are unaffected by anything on this page.
        </p>
      </div>
    </div>
  );
};
