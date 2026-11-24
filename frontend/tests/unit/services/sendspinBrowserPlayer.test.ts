import { describe, it, expect, beforeEach, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';

// A stand-in for the SDK player whose unlock() and connect() resolve only when the test says so.
const sdk = vi.hoisted(() => {
  interface Gate { promise: Promise<void>; open: () => void }
  const gate = (): Gate => {
    let open!: () => void;
    const promise = new Promise<void>((resolve) => { open = resolve; });
    return { promise, open };
  };
  class FakeSdkPlayer {
    static instances: FakeSdkPlayer[] = [];
    unlockGate = gate();
    connectGate = gate();
    connectCalls = 0;
    disconnects: string[] = [];
    isConnected = false;
    constructor(public config: { playerId: string }) {
      FakeSdkPlayer.instances.push(this);
    }
    unlock(): Promise<void> {
      return this.unlockGate.promise;
    }
    connect(): Promise<void> {
      this.connectCalls++;
      return this.connectGate.promise.then(() => { this.isConnected = true; });
    }
    disconnect(reason: string): void {
      this.disconnects.push(reason);
      this.isConnected = false;
    }
  }
  return { FakeSdkPlayer };
});

vi.mock('@sendspin/sendspin-js', () => ({ SendspinPlayer: sdk.FakeSdkPlayer }));

import { SendspinBrowserPlayer } from '../../../services/sendspinBrowserPlayer';
import { useBrowserPlayer } from '../../../hooks/useBrowserPlayer';

const flush = async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); };

beforeEach(() => {
  sdk.FakeSdkPlayer.instances = [];
});

describe('SendspinBrowserPlayer: stop() during start()', () => {
  it('constructs no SDK player when stopped before the SDK module loads', async () => {
    const player = new SendspinBrowserPlayer('192.0.2.10');
    const started = player.start();
    player.stop(); // before the dynamic import resolves
    await started;
    expect(sdk.FakeSdkPlayer.instances).toHaveLength(0);
  });

  it('does not connect when stopped during unlock()', async () => {
    const player = new SendspinBrowserPlayer('192.0.2.10');
    const started = player.start();
    await flush();
    const inner = sdk.FakeSdkPlayer.instances[0];
    player.stop();
    inner.unlockGate.open();
    await started;
    expect(inner.connectCalls).toBe(0);
    expect(inner.disconnects.length).toBeGreaterThan(0);
  });

  it('disconnects the SDK player when stopped during connect()', async () => {
    const player = new SendspinBrowserPlayer('192.0.2.10');
    const started = player.start();
    await flush();
    const inner = sdk.FakeSdkPlayer.instances[0];
    inner.unlockGate.open();
    await flush();
    expect(inner.connectCalls).toBe(1);
    player.stop();
    inner.connectGate.open(); // the connection lands after the stop
    await started;
    expect(inner.isConnected).toBe(false);
    expect(player.isConnected).toBe(false);
  });

  it('leaves no unload listener behind', async () => {
    const remove = vi.spyOn(window, 'removeEventListener');
    const add = vi.spyOn(window, 'addEventListener');
    const player = new SendspinBrowserPlayer('192.0.2.10');
    const started = player.start();
    await flush();
    player.stop();
    sdk.FakeSdkPlayer.instances[0].unlockGate.open();
    await started;
    const added = add.mock.calls.filter(([type]) => type === 'pagehide').length;
    const removed = remove.mock.calls.filter(([type]) => type === 'pagehide').length;
    expect(removed).toBe(added);
    add.mockRestore();
    remove.mockRestore();
  });

  it('connects as before when nothing stops it', async () => {
    const player = new SendspinBrowserPlayer('192.0.2.10');
    const started = player.start();
    await flush();
    const inner = sdk.FakeSdkPlayer.instances[0];
    inner.unlockGate.open();
    await flush();
    inner.connectGate.open();
    await started;
    expect(player.isConnected).toBe(true);
    expect(inner.config.playerId).toBe(player.playerId);
    player.stop();
  });
});

describe('useBrowserPlayer: stop() during start()', () => {
  it('stays inactive when the start it stopped then completes', async () => {
    const { result } = renderHook(() => useBrowserPlayer());
    let started!: Promise<string | null>;
    await act(async () => {
      started = result.current.start('192.0.2.10');
      await flush();
    });
    const inner = sdk.FakeSdkPlayer.instances[0];
    await act(async () => {
      result.current.stop();
      inner.unlockGate.open();
      inner.connectGate.open();
      await started;
    });
    expect(await started).toBeNull();
    expect(result.current.active).toBe(false);
    expect(result.current.playerId).toBeNull();
    expect(result.current.starting).toBe(false);
  });

  it('keeps a second start alive when the first one it replaced completes late', async () => {
    const { result } = renderHook(() => useBrowserPlayer());
    let first!: Promise<string | null>;
    let second!: Promise<string | null>;
    await act(async () => {
      first = result.current.start('192.0.2.10');
      await flush();
      result.current.stop();
      second = result.current.start('192.0.2.11');
      await flush();
    });
    const [firstInner, secondInner] = sdk.FakeSdkPlayer.instances;
    await act(async () => {
      firstInner.unlockGate.open();
      await first;
    });
    expect(result.current.playerId).toBe(secondInner.config.playerId);
    expect(result.current.starting).toBe(true);
    await act(async () => {
      secondInner.unlockGate.open();
      await flush();
      secondInner.connectGate.open();
      await second;
    });
    expect(result.current.active).toBe(true);
  });
});
