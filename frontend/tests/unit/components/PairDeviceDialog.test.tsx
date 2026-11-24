import { describe, it, expect, afterEach, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { PairDeviceDialog } from '../../../components/PairDeviceDialog';
import { createMockClient } from '../../mocks/mockTypes';

type PollState = { state: string; error?: string };

const client = createMockClient({ id: 'speaker-1', name: 'Kitchen Speaker', pairingState: 'unpaired' });

function setup(overrides: Partial<React.ComponentProps<typeof PairDeviceDialog>> = {}) {
  const props = {
    client,
    onStart: vi.fn(async () => ({ ok: true })),
    onSubmitPin: vi.fn(async () => ({ ok: true })),
    onPoll: vi.fn(async (): Promise<PollState> => ({ state: 'running' })),
    onCancel: vi.fn(),
    onDone: vi.fn(),
    ...overrides,
  };
  const view = render(<PairDeviceDialog {...props} />);
  return { props, ...view };
}

/** Click Pair and let the start request resolve, so the dialog is in the `running` phase. */
async function startPairing() {
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Pair' }));
  });
  expect(screen.getByText(/Pairing with Kitchen Speaker/)).toBeInTheDocument();
}

const backdrop = () => screen.getByRole('dialog');

afterEach(() => {
  vi.useRealTimers();
});

describe('PairDeviceDialog as a modal dialog', () => {
  it('is a modal dialog with a name', () => {
    setup();
    const dialog = screen.getByRole('dialog', { name: 'Pair Kitchen Speaker' });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
  });

  it('moves focus into the dialog when it opens', () => {
    setup();
    expect(backdrop().contains(document.activeElement)).toBe(true);
    expect(document.activeElement).not.toBe(document.body);
  });

  it('returns focus to the element that opened it', () => {
    const opener = document.createElement('button');
    document.body.appendChild(opener);
    opener.focus();
    const { unmount } = setup();
    expect(document.activeElement).not.toBe(opener);
    unmount();
    expect(document.activeElement).toBe(opener);
    opener.remove();
  });

  it('keeps Tab inside the dialog', () => {
    setup();
    const buttons = screen.getAllByRole('button');
    const last = buttons[buttons.length - 1];
    const first = screen.getAllByRole('radio')[0];

    last.focus();
    fireEvent.keyDown(document, { key: 'Tab' });
    expect(document.activeElement).toBe(first);

    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true });
    expect(document.activeElement).toBe(last);
  });

  it('pulls focus back when it is outside the dialog', () => {
    setup();
    (document.activeElement as HTMLElement).blur();
    fireEvent.keyDown(document, { key: 'Tab' });
    expect(backdrop().contains(document.activeElement)).toBe(true);
  });

  it('cancels on Escape', () => {
    const { props } = setup();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(props.onCancel).toHaveBeenCalledTimes(1);
  });

  it('cancels on a backdrop click before pairing starts', () => {
    const { props } = setup();
    fireEvent.click(backdrop());
    expect(props.onCancel).toHaveBeenCalledTimes(1);
  });

  it('does not cancel on a click inside the panel', () => {
    const { props } = setup();
    fireEvent.click(screen.getByText('Pair Kitchen Speaker'));
    expect(props.onCancel).not.toHaveBeenCalled();
  });

  it('does not cancel on a backdrop click while pairing runs', async () => {
    const { props } = setup();
    await startPairing();
    fireEvent.click(backdrop());
    expect(props.onCancel).not.toHaveBeenCalled();
  });

  it('does not cancel on a backdrop click while it waits for a PIN', async () => {
    vi.useFakeTimers();
    const { props } = setup({ onPoll: vi.fn(async () => ({ state: 'awaiting_pin' })) });
    await startPairing();
    await act(async () => { await vi.advanceTimersByTimeAsync(1100); });
    expect(screen.getByLabelText(/Enter the PIN/)).toBeInTheDocument();
    fireEvent.click(backdrop());
    expect(props.onCancel).not.toHaveBeenCalled();
  });

  it('still cancels from the Cancel button and Escape while pairing runs', async () => {
    const { props } = setup();
    await startPairing();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(props.onCancel).toHaveBeenCalledTimes(2);
  });
});

describe('PairDeviceDialog polling', () => {
  /** An onPoll whose every call stays pending until the test answers it. */
  function deferredPoll() {
    const pending: Array<(s: PollState) => void> = [];
    const onPoll = vi.fn(() => new Promise<PollState>((resolve) => { pending.push(resolve); }));
    return { onPoll, pending };
  }

  it('does not start a second poll while one is in flight', async () => {
    vi.useFakeTimers();
    const { onPoll } = deferredPoll();
    setup({ onPoll });
    await startPairing();
    await act(async () => { await vi.advanceTimersByTimeAsync(5500); });
    expect(onPoll).toHaveBeenCalledTimes(1);
  });

  it('keeps "paired" when a slower poll then answers "failed"', async () => {
    vi.useFakeTimers();
    const { onPoll, pending } = deferredPoll();
    setup({ onPoll });
    await startPairing();
    await act(async () => { await vi.advanceTimersByTimeAsync(2500); });

    await act(async () => { pending[0]({ state: 'paired' }); });
    expect(screen.getByText(/is paired and can now play/)).toBeInTheDocument();

    // Every other response that was requested before the outcome was known.
    await act(async () => { for (const answer of pending.slice(1)) answer({ state: 'failed', error: 'late' }); });
    expect(screen.getByText(/is paired and can now play/)).toBeInTheDocument();
    expect(screen.queryByText('late')).not.toBeInTheDocument();
  });

  it('stops polling when the attempt ends', async () => {
    vi.useFakeTimers();
    const onPoll = vi.fn(async (): Promise<PollState> => ({ state: 'failed', error: 'The device refused.' }));
    setup({ onPoll });
    await startPairing();
    await act(async () => { await vi.advanceTimersByTimeAsync(1100); });
    expect(screen.getByText('The device refused.')).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(onPoll).toHaveBeenCalledTimes(1);
  });

  it('polls again after a poll that threw', async () => {
    vi.useFakeTimers();
    const onPoll = vi
      .fn<() => Promise<PollState>>()
      .mockRejectedValueOnce(new Error('network'))
      .mockResolvedValue({ state: 'paired' });
    setup({ onPoll });
    await startPairing();
    await act(async () => { await vi.advanceTimersByTimeAsync(2200); });
    expect(screen.getByText(/is paired and can now play/)).toBeInTheDocument();
  });

  it('ignores a poll response that lands after the dialog closed', async () => {
    vi.useFakeTimers();
    const { onPoll, pending } = deferredPoll();
    const errors = vi.spyOn(console, 'error').mockImplementation(() => {});
    const { unmount } = setup({ onPoll });
    await startPairing();
    await act(async () => { await vi.advanceTimersByTimeAsync(1100); });
    unmount();
    await act(async () => { pending[0]({ state: 'failed', error: 'late' }); });
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(onPoll).toHaveBeenCalledTimes(1);
    expect(errors).not.toHaveBeenCalled();
    errors.mockRestore();
  });
});

describe('PairDeviceDialog while the start request is in flight', () => {
  type StartResult = { ok: boolean; message?: string };

  /** An onStart that stays pending until the test answers it. */
  function deferredStart() {
    let answer: (r: StartResult) => void = () => {};
    const onStart = vi.fn(() => new Promise<StartResult>((resolve) => { answer = resolve; }));
    return { onStart, answer: (r: StartResult) => answer(r) };
  }

  const clickPair = async () => {
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Pair' }));
    });
  };

  it('does not cancel on Escape', async () => {
    const { onStart } = deferredStart();
    const { props } = setup({ onStart });
    await clickPair();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(props.onCancel).not.toHaveBeenCalled();
  });

  it('does not cancel on a backdrop click', async () => {
    const { onStart } = deferredStart();
    const { props } = setup({ onStart });
    await clickPair();
    fireEvent.click(backdrop());
    expect(props.onCancel).not.toHaveBeenCalled();
  });

  it('cancels on Escape again when the unit accepts the attempt', async () => {
    const { onStart, answer } = deferredStart();
    const { props } = setup({ onStart });
    await clickPair();
    await act(async () => { answer({ ok: true }); });
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(props.onCancel).toHaveBeenCalledTimes(1);
  });

  it('cancels the attempt when the start request resolves after the dialog closed', async () => {
    const { onStart, answer } = deferredStart();
    const { props, unmount } = setup({ onStart });
    await clickPair();
    // The Cancel button stays live. Its cancel can reach the unit before the attempt exists.
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(props.onCancel).toHaveBeenCalledTimes(1);
    unmount();
    await act(async () => { answer({ ok: true }); });
    // The attempt exists now, and no dialog shows it. Cancel it a second time.
    expect(props.onCancel).toHaveBeenCalledTimes(2);
  });
});
