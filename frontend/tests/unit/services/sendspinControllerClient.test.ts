import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { SendspinControllerClient } from '../../../services/sendspinControllerClient';

/** A browser-like socket: it opens and closes only when the test says so. */
class FakeSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  static instances: FakeSocket[] = [];
  readyState = FakeSocket.CONNECTING;
  binaryType = '';
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  sent: Array<{ type: string }> = [];
  constructor(public url: string) {
    FakeSocket.instances.push(this);
  }
  send(raw: string): void {
    this.sent.push(JSON.parse(raw));
  }
  close(): void {
    this.readyState = FakeSocket.CLOSED;
  }
  deliverOpen(): void {
    this.readyState = FakeSocket.OPEN;
    this.onopen?.();
  }
  deliverClose(): void {
    this.readyState = FakeSocket.CLOSED;
    this.onclose?.();
  }
  deliver(type: string, payload: unknown = {}): void {
    this.onmessage?.({ data: JSON.stringify({ type, payload }) });
  }
  get types(): string[] {
    return this.sent.map((m) => m.type);
  }
}

const newClient = () => new SendspinControllerClient('unit-a', '192.0.2.10', () => {}, { clientId: 'ctrl:test' });

beforeEach(() => {
  FakeSocket.instances = [];
  vi.useFakeTimers();
  vi.stubGlobal('WebSocket', FakeSocket);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('SendspinControllerClient connection guard', () => {
  it('opens one socket when connect() is called twice', () => {
    const client = newClient();
    client.connect();
    client.connect();
    expect(FakeSocket.instances).toHaveLength(1);
    client.close();
  });

  it('opens one socket when connect() is called again on an open connection', () => {
    const client = newClient();
    client.connect();
    FakeSocket.instances[0].deliverOpen();
    client.connect();
    expect(FakeSocket.instances).toHaveLength(1);
    client.close();
  });

  it('does not open a second socket from a reconnect timer that connect() overtook', () => {
    const client = newClient();
    client.connect();
    FakeSocket.instances[0].deliverClose(); // dropped: a reconnect is now scheduled
    client.connect(); // the caller reconnects at once
    expect(FakeSocket.instances).toHaveLength(2);
    vi.advanceTimersByTime(60_000);
    expect(FakeSocket.instances).toHaveLength(2);
    client.close();
  });

  it('reconnects after a drop', () => {
    const client = newClient();
    client.connect();
    FakeSocket.instances[0].deliverClose();
    vi.advanceTimersByTime(1_500);
    expect(FakeSocket.instances).toHaveLength(2);
    client.close();
  });
});

describe('SendspinControllerClient handshake order', () => {
  it('sends only client/hello until server/hello arrives', () => {
    const client = newClient();
    client.connect();
    const ws = FakeSocket.instances[0];
    ws.deliverOpen();
    vi.advanceTimersByTime(5_000);
    expect(ws.types).toEqual(['client/hello']);
    ws.deliver('server/hello');
    expect(ws.types).toEqual(['client/hello', 'client/state', 'client/time']);
    client.close();
  });

  it('does not let the old clock timer send client/time on a new socket before server/hello', () => {
    const client = newClient();
    client.connect();
    const first = FakeSocket.instances[0];
    first.deliverOpen();
    first.deliver('server/hello'); // starts the clock loop: next client/time in 200 ms
    first.deliverClose();

    client.connect(); // the caller reconnects at once, before the 200 ms timer is due
    const second = FakeSocket.instances[1];
    second.deliverOpen();
    vi.advanceTimersByTime(250); // the first socket's clock timer is due here

    expect(second.types).toEqual(['client/hello']);
    client.close();
  });

  it('ignores a late close event from a socket it replaced', () => {
    const client = newClient();
    client.connect();
    const first = FakeSocket.instances[0];
    const lateClose = first.onclose!;
    first.deliverClose();
    client.connect();
    lateClose(); // delivered again, after the replacement exists
    vi.advanceTimersByTime(60_000);
    expect(FakeSocket.instances).toHaveLength(2);
    client.close();
  });
});
