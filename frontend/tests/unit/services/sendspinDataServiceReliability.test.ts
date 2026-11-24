import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { MeshRequestError, MeshView, Neighbourhood, SendspinDataService } from '../../../services/sendspinDataService';

// The service's private seams, named once so each test does not need its own cast.
interface Internals {
  poll(): Promise<void>;
  applyView(view: MeshView): void;
  lastView: MeshView;
  neighbourhood: Neighbourhood | null;
  controllers: Map<string, unknown>;
  consumeCtrl: { volume: number | null } | null;
}
const internals = (svc: SendspinDataService) => svc as unknown as Internals;

const PLAYER_URL = 'ws://192.0.2.10:8928/sendspin';
const VIEW: MeshView = {
  local_unit_id: 'unit-210',
  units: [
    {
      unit_id: 'unit-210',
      name: 'Pi4-02',
      host: '192.0.2.10',
      sources: [{ source_id: 'airplay', group_id: 'gA', group_name: 'AirPlay', streaming: true, player_ids: ['player-210'] }],
      players: [{ player_id: 'player-210', name: 'Player-210', connected: true, group_id: 'gA', url: PLAYER_URL, volume: 60 }],
    },
  ],
};
const viewNamed = (name: string): MeshView => ({ units: [{ ...VIEW.units[0], name }] });

/** A browser-like socket: `close()` does NOT deliver the close event. The test delivers it. */
class FakeSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  static instances: FakeSocket[] = [];
  readyState = FakeSocket.CONNECTING;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  send = vi.fn();
  constructor(public url: string) {
    FakeSocket.instances.push(this);
  }
  close(): void {
    this.readyState = FakeSocket.CLOSED;
  }
  deliverClose(): void {
    this.readyState = FakeSocket.CLOSED;
    this.onclose?.();
  }
}

interface Deferred {
  url: string;
  resolve: (body: unknown, status?: number) => void;
}

/** A fetch whose every call stays pending until the test answers it. */
function deferredFetch(): Deferred[] {
  const calls: Deferred[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string) => new Promise((resolve) => {
      calls.push({
        url,
        resolve: (body, status = 200) =>
          resolve({ ok: status >= 200 && status < 300, status, json: async () => body }),
      });
    })),
  );
  return calls;
}

const flush = async () => { for (let i = 0; i < 5; i++) await Promise.resolve(); };

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('a failed mesh command tells the caller why', () => {
  function serviceWithView() {
    const svc = new SendspinDataService();
    internals(svc).applyView(structuredClone(VIEW));
    return svc;
  }

  it('rejects with the reason the unit gave on a 400', async () => {
    const svc = serviceWithView();
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 400, json: async () => ({ error: 'unknown player' }) })));
    const err = await svc.routeClient('player-210', 'unit-210::airplay').catch((e) => e);
    expect(err).toBeInstanceOf(MeshRequestError);
    expect(err.message).toBe('unknown player');
    expect(err.status).toBe(400);
  });

  it('falls back to the status when the refusal has no JSON body', async () => {
    const svc = serviceWithView();
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 502, json: async () => { throw new Error('not json'); } })));
    const err = await svc.routeClient('player-210', 'unit-210::airplay').catch((e) => e);
    expect(err.message).toBe('Request failed (502)');
  });

  it('says the unit is unreachable on a network error, not "Failed to fetch"', async () => {
    const svc = serviceWithView();
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Failed to fetch'); }));
    const err = await svc.adoptSpeaker(PLAYER_URL, 'unit-210::airplay').catch((e) => e);
    expect(err).toBeInstanceOf(MeshRequestError);
    expect(err.message).toBe('Could not reach the unit');
    expect(err.status).toBeUndefined();
  });

  it('releases the held level when a volume change is refused', async () => {
    const svc = serviceWithView();
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 400, json: async () => ({ error: 'unknown player' }) })));
    await expect(svc.setVolume('player-210', 15)).rejects.toThrow('unknown player');
    // Not 15: the hold is gone, so the slider shows what the speaker really plays at.
    expect(svc.snapshot().clients.find((c) => c.id === 'player-210')!.volume).toBe(60);
  });
});

describe('poll responses apply in the order they were SENT', () => {
  it('drops a slow response that lands after a newer one', async () => {
    const calls = deferredFetch();
    const svc = new SendspinDataService();
    const older = internals(svc).poll();
    const newer = internals(svc).poll();
    const [olderView, newerView] = calls.filter((c) => c.url.endsWith('/view'));

    newerView.resolve(viewNamed('newer'));
    await flush();
    olderView.resolve(viewNamed('older')); // the slow one, arriving last
    await flush();

    expect(internals(svc).lastView.units[0].name).toBe('newer');
    for (const c of calls.filter((x) => x.url.endsWith('/neighbourhood'))) c.resolve({ players: [], servers: [] });
    await Promise.all([older, newer]);
    svc.stop();
  });

  it('applies the same rule to the neighbourhood', async () => {
    const calls = deferredFetch();
    const svc = new SendspinDataService();
    void internals(svc).poll();
    void internals(svc).poll();
    for (const c of calls.filter((x) => x.url.endsWith('/view'))) c.resolve(viewNamed('any'));
    await flush();
    const [olderHood, newerHood] = calls.filter((c) => c.url.endsWith('/neighbourhood'));
    const hood = (name: string): Neighbourhood => ({
      players: [{ name, friendly_name: name, url: 'ws://198.51.100.9:8927/sendspin', host: '198.51.100.9', port: 8927, is_own: false }],
      servers: [],
    });

    newerHood.resolve(hood('newer'));
    await flush();
    olderHood.resolve(hood('older'));
    await flush();

    expect(internals(svc).neighbourhood!.players[0].name).toBe('newer');
    svc.stop();
  });

  it('drops a response that lands after stop(), so no controller socket reopens', async () => {
    const calls = deferredFetch();
    const svc = new SendspinDataService();
    void internals(svc).poll();
    svc.stop();
    calls[0].resolve(structuredClone(VIEW));
    await flush();
    expect(internals(svc).controllers.size).toBe(0);
    expect(internals(svc).lastView.units).toEqual([]);
  });
});

describe('the consume relay socket', () => {
  beforeEach(() => {
    FakeSocket.instances = [];
    vi.useFakeTimers();
    vi.stubGlobal('WebSocket', FakeSocket);
    vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {}))); // polls stay pending
  });

  it('does not reconnect after stop(), even with a reconnect already scheduled', () => {
    const svc = new SendspinDataService();
    svc.start();
    FakeSocket.instances[0].deliverClose(); // the relay drops: a reconnect is now pending
    svc.stop();
    vi.advanceTimersByTime(10_000);
    expect(FakeSocket.instances).toHaveLength(1);
  });

  it('reconnects once after a drop while running', () => {
    const svc = new SendspinDataService();
    svc.start();
    FakeSocket.instances[0].deliverClose();
    vi.advanceTimersByTime(2_500);
    expect(FakeSocket.instances).toHaveLength(2);
    svc.stop();
  });

  it('ignores a stale socket under start → stop → start (React StrictMode)', () => {
    const svc = new SendspinDataService();
    svc.start();
    svc.stop();
    svc.start();
    expect(FakeSocket.instances).toHaveLength(2);
    const [stale, live] = FakeSocket.instances;

    live.onmessage?.({ data: JSON.stringify({ t: 'ctrl', commands: ['play'], volume: 42 }) });
    expect(internals(svc).consumeCtrl?.volume).toBe(42);

    stale.deliverClose(); // the browser delivers the first socket's close late
    expect(internals(svc).consumeCtrl?.volume).toBe(42); // the live relay state survives
    vi.advanceTimersByTime(10_000);
    expect(FakeSocket.instances).toHaveLength(2); // and no third socket opens

    stale.onmessage?.({ data: JSON.stringify({ t: 'ctrl', commands: [], volume: 7 }) });
    expect(internals(svc).consumeCtrl?.volume).toBe(42);
    svc.stop();
  });
});

describe('a refusal whose `error` is not a sentence', () => {
  it('falls back to the status and does not show "[object Object]"', async () => {
    const svc = new SendspinDataService();
    internals(svc).applyView(structuredClone(VIEW));
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: false,
      status: 422,
      json: async () => ({ error: { code: 'bad_request', detail: 'player_id' } }),
    })));
    const err = await svc.routeClient('player-210', 'unit-210::airplay').catch((e) => e);
    expect(err).toBeInstanceOf(MeshRequestError);
    expect(err.message).toBe('Request failed (422)');
  });

  it('does the same for a pairing request', async () => {
    const svc = new SendspinDataService();
    internals(svc).applyView(structuredClone(VIEW));
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 422, json: async () => ({ error: { code: 'bad' } }) })));
    const res = await svc.pairDevice('unit-210', 'player-210', 'dynamic_pin');
    expect(res).toEqual({ ok: false, message: 'request failed (422)' });
  });
});
