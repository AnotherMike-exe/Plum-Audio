import React from 'react';

interface IncomingPairPromptProps {
  /** null when no attempt is running. `pin` set = show it; `gesture` = confirm-on-device. */
  prompt: { pin: string | null; gesture: boolean } | null;
  /** This unit's display name, so the operator knows WHICH speaker is being claimed. */
  unitName?: string;
}

/**
 * Shown when a FOREIGN server (Music Assistant) is pairing with THIS unit's speaker.
 *
 * This is the inbound direction, and it is the mirror of PairDeviceDialog: there we are the server
 * pairing a device, and we collect a PIN. Here the protocol makes the *client* display the PIN and
 * the operator types it into the other server — so all this component does is show it, clearly
 * enough to read across a room, until the exchange ends.
 *
 * It exists because the whole flow silently failed without it. Our player derived the PIN and
 * emitted it to the consume relay exactly as designed; nothing rendered it; the operator waited for
 * a prompt that could never appear, and MA's attempt expired as `user_cancelled`. The backend was
 * complete and the feature was still unusable — which is why the receiver is not optional.
 *
 * Deliberately NOT dismissable: there is nothing for the operator to decide here, and a stray click
 * that hid the PIN would strand a pairing that is already on a timer. It clears itself when the
 * player reports the exchange has ended (`pin: null`), success or failure.
 */
export const IncomingPairPrompt: React.FC<IncomingPairPromptProps> = ({ prompt, unitName }) => {
  if (!prompt || (!prompt.pin && !prompt.gesture)) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4">
      <div className="w-full max-w-md rounded-2xl bg-[var(--bg-secondary)] p-6 shadow-2xl border border-[var(--border-color)]">
        <h3 className="text-lg font-semibold text-[var(--text-primary)] mb-1">
          Pairing request{unitName ? ` for ${unitName}` : ''}
        </h3>
        <p className="text-sm text-[var(--text-secondary)] mb-5">
          Another Sendspin server wants to use this speaker.{' '}
          {prompt.pin ? 'Enter this code there to allow it.' : 'Confirm on this device to allow it.'}
        </p>

        {prompt.pin && (
          <div
            className="rounded-xl bg-[var(--bg-primary)] py-6 text-center font-mono text-4xl tracking-[0.35em] text-[var(--text-primary)] select-all"
            aria-label={`Pairing code ${prompt.pin.split('').join(' ')}`}
          >
            {prompt.pin}
          </div>
        )}

        <p className="mt-5 text-xs text-[var(--text-secondary)]">
          This closes on its own once the other server finishes — or when the attempt times out.
        </p>
      </div>
    </div>
  );
};
