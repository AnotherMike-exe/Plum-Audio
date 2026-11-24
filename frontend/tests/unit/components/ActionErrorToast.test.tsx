import React from 'react';
import { describe, it, expect, afterEach, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { ActionErrorToast } from '../../../components/ActionErrorToast';
import { ACTION_ERROR_MS, describeActionError, useActionError } from '../../../hooks/useActionError';
import { MeshRequestError } from '../../../services/sendspinDataService';

/** The wiring MeshApp uses: a guarded command, and the toast that shows its failure. */
const Harness: React.FC<{ run: () => Promise<void> }> = ({ run }) => {
  const { error, guard, dismiss } = useActionError();
  return (
    <>
      <button onClick={() => guard(run(), 'Could not change the volume')}>act</button>
      <ActionErrorToast message={error} onDismiss={dismiss} />
    </>
  );
};

const clickAct = async () => {
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'act' }));
  });
};

afterEach(() => {
  vi.useRealTimers();
});

describe('describeActionError', () => {
  it('joins the action and the reason the unit gave', () => {
    const err = new MeshRequestError('unknown player', '/volume', 400);
    expect(describeActionError('Could not change the volume', err)).toBe('Could not change the volume: unknown player');
  });

  it('gives the action alone when there is no usable reason', () => {
    expect(describeActionError('Could not change the volume', undefined)).toBe('Could not change the volume.');
    expect(describeActionError('Could not change the volume', new Error(''))).toBe('Could not change the volume.');
  });
});

describe('ActionErrorToast', () => {
  it('keeps a polite live region in the page before there is an error', () => {
    render(<ActionErrorToast message={null} onDismiss={() => {}} />);
    const region = screen.getByRole('status');
    expect(region).toHaveAttribute('aria-live', 'polite');
    expect(region).toBeEmptyDOMElement();
  });

  it('shows the message inside the live region, with a Dismiss button', () => {
    const onDismiss = vi.fn();
    render(<ActionErrorToast message="Could not move Kitchen: unknown player" onDismiss={onDismiss} />);
    expect(screen.getByRole('status')).toHaveTextContent('Could not move Kitchen: unknown player');
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });
});

describe('a failed command reaches the user', () => {
  it('shows the reason when the unit refuses (400)', async () => {
    render(<Harness run={() => Promise.reject(new MeshRequestError('unknown player', '/volume', 400))} />);
    await clickAct();
    expect(screen.getByRole('status')).toHaveTextContent('Could not change the volume: unknown player');
  });

  it('shows that the unit is unreachable on a network error', async () => {
    render(<Harness run={() => Promise.reject(new MeshRequestError('Could not reach the unit', '/volume'))} />);
    await clickAct();
    expect(screen.getByRole('status')).toHaveTextContent('Could not change the volume: Could not reach the unit');
  });

  it('leaves no unhandled rejection', async () => {
    const unhandled = vi.fn();
    process.on('unhandledRejection', unhandled);
    try {
      render(<Harness run={() => Promise.reject(new Error('boom'))} />);
      await clickAct();
      await new Promise((resolve) => setTimeout(resolve, 0));
      expect(unhandled).not.toHaveBeenCalled();
    } finally {
      process.off('unhandledRejection', unhandled);
    }
  });

  it('shows nothing when the command succeeds', async () => {
    render(<Harness run={() => Promise.resolve()} />);
    await clickAct();
    expect(screen.getByRole('status')).toBeEmptyDOMElement();
  });

  it('clears the message on Dismiss', async () => {
    render(<Harness run={() => Promise.reject(new Error('boom'))} />);
    await clickAct();
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
    expect(screen.getByRole('status')).toBeEmptyDOMElement();
  });

  it('clears the message by itself after the display time', async () => {
    vi.useFakeTimers();
    render(<Harness run={() => Promise.reject(new Error('boom'))} />);
    await clickAct();
    expect(screen.getByRole('status')).toHaveTextContent('boom');
    await act(async () => { await vi.advanceTimersByTimeAsync(ACTION_ERROR_MS + 100); });
    expect(screen.getByRole('status')).toBeEmptyDOMElement();
  });
});

describe('useActionError after unmount', () => {
  it('starts no timer for a rejection that lands late', async () => {
    vi.useFakeTimers();
    let reject: (err: Error) => void = () => {};
    const run = () => new Promise<void>((_resolve, rej) => { reject = rej; });
    const { unmount } = render(<Harness run={run} />);
    await clickAct();
    unmount();
    await act(async () => { reject(new Error('unknown player')); });
    expect(vi.getTimerCount()).toBe(0);
  });
});
