import React from 'react';
import { Icon } from './Icon';

interface ActionErrorToastProps {
  /** The sentence to show, or null for nothing. */
  message: string | null;
  onDismiss: () => void;
}

/**
 * The app's one transient error message: a command the user gave did not apply, and why.
 *
 * The live region is ALWAYS in the page and only its content changes. A screen reader announces a
 * change inside a region it already knows; a region that is inserted together with its text is
 * often not announced at all. `polite`, not `assertive`: a refused volume change must not cut
 * across whatever is being read.
 *
 * Fixed to the bottom of the viewport, above the modals' z-index, so it also shows over Settings and
 * the visualizer. The container takes no pointer events — only the message itself does — so an empty
 * region can never cover a control.
 */
export const ActionErrorToast: React.FC<ActionErrorToastProps> = ({ message, onDismiss }) => (
  <div
    role="status"
    aria-live="polite"
    aria-atomic="true"
    className="fixed inset-x-0 bottom-4 z-[60] flex justify-center px-4 pointer-events-none"
  >
    {message && (
      <div className="pointer-events-auto flex items-start gap-3 max-w-xl w-full p-4 rounded-lg shadow-2xl bg-(--bg-secondary) border border-red-600/40">
        <Icon name="triangle-exclamation" className="text-red-400 mt-0.5 shrink-0" />
        <p className="flex-1 text-sm text-(--text-primary) break-words">{message}</p>
        <button
          type="button"
          onClick={onDismiss}
          className="shrink-0 text-sm font-semibold text-(--text-secondary) hover:text-(--text-primary) transition-colors"
        >
          Dismiss
        </button>
      </div>
    )}
  </div>
);
