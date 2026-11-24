import { describe, it, expect, beforeEach } from 'vitest';
import { MeshView, Neighbourhood, SendspinDataService, backendFriendlyNames } from '../../../services/sendspinDataService';

interface Internals {
  applyView(view: MeshView): void;
  applyNeighbourhood(n: Neighbourhood): void;
}
const internals = (svc: SendspinDataService) => svc as unknown as Internals;

const UNIT = {
  unit_id: 'unit-210',
  name: 'Pi4-02',
  host: '192.0.2.10',
  sources: [{ source_id: 'airplay', group_id: 'gA', group_name: 'AirPlay', streaming: true, player_ids: [] }],
  players: [],
};

describe('a name the unit supplies beats the name this browser remembered', () => {
  const SPEAKER_URL = 'ws://198.51.100.30:8927/sendspin';
  const MEMO_KEY = 'plum.speakerNames';
  const TECHNICAL = 'home-assistant-voice-a1b2c3';
  const IDLE: MeshView = { local_unit_id: 'unit-210', units: [UNIT] };
  const hood = (friendly: string): Neighbourhood => ({
    players: [{ name: TECHNICAL, friendly_name: friendly, url: SPEAKER_URL, host: '198.51.100.30', port: 8927, is_own: false }],
    servers: [],
  });
  const nameOf = (svc: SendspinDataService) => svc.snapshot().clients.find((c) => c.url === SPEAKER_URL)!.name;

  beforeEach(() => window.localStorage.clear());

  it('shows the friendly name over a technical name in the memo', () => {
    window.localStorage.setItem(MEMO_KEY, JSON.stringify([[SPEAKER_URL, TECHNICAL]]));
    const svc = new SendspinDataService();
    internals(svc).applyView(IDLE);
    internals(svc).applyNeighbourhood(hood('Kitchen Speaker'));
    expect(nameOf(svc)).toBe('Kitchen Speaker');
  });

  it('shows the friendly name over an older remembered name', () => {
    window.localStorage.setItem(MEMO_KEY, JSON.stringify([[SPEAKER_URL, 'Old Name']]));
    const svc = new SendspinDataService();
    internals(svc).applyView(IDLE);
    internals(svc).applyNeighbourhood(hood('Kitchen Speaker'));
    expect(nameOf(svc)).toBe('Kitchen Speaker');
  });

  it('writes the friendly name over the memo, so the old name cannot return', () => {
    window.localStorage.setItem(MEMO_KEY, JSON.stringify([[SPEAKER_URL, TECHNICAL]]));
    const svc = new SendspinDataService();
    internals(svc).applyView(IDLE);
    internals(svc).applyNeighbourhood(hood('Kitchen Speaker'));
    expect(JSON.parse(window.localStorage.getItem(MEMO_KEY)!)).toEqual([[SPEAKER_URL, 'Kitchen Speaker']]);

    // The unit falls back to the bare instance name (an older image, a cleared map). The memo now
    // holds the friendly name, not the technical one it started with.
    internals(svc).applyNeighbourhood(hood(TECHNICAL));
    expect(nameOf(svc)).toBe('Kitchen Speaker');
  });

  it('keeps the memo as the fallback when the unit supplies only the technical name', () => {
    window.localStorage.setItem(MEMO_KEY, JSON.stringify([[SPEAKER_URL, 'Home Assistant Voice PE - 01']]));
    const svc = new SendspinDataService();
    internals(svc).applyView(IDLE);
    internals(svc).applyNeighbourhood(hood(TECHNICAL));
    expect(nameOf(svc)).toBe('Home Assistant Voice PE - 01');
    expect(JSON.parse(window.localStorage.getItem(MEMO_KEY)!)).toEqual([[SPEAKER_URL, 'Home Assistant Voice PE - 01']]);
  });

  it('still lets a live self-report win, because a rename reaches it first', () => {
    const renamed: MeshView = {
      local_unit_id: 'unit-210',
      units: [{ ...UNIT, local_player: { player_id: 'p', name: 'Renamed', url: SPEAKER_URL } }],
    };
    const svc = new SendspinDataService();
    internals(svc).applyView(renamed);
    internals(svc).applyNeighbourhood(hood('Name Before Rename'));
    expect(nameOf(svc)).toBe('Renamed');
    expect(JSON.parse(window.localStorage.getItem(MEMO_KEY)!)).toEqual([[SPEAKER_URL, 'Renamed']]);
  });
});

describe('backendFriendlyNames', () => {
  it('counts only a name that differs from the mDNS instance name', () => {
    const map = backendFriendlyNames({
      players: [
        { name: 'esp-1', friendly_name: 'Kitchen', url: 'ws://a', host: 'a', port: 1, is_own: false },
        { name: 'esp-2', friendly_name: 'esp-2', url: 'ws://b', host: 'b', port: 1, is_own: false },
        { name: 'esp-3', friendly_name: '', url: 'ws://c', host: 'c', port: 1, is_own: false },
      ],
      servers: [],
    });
    expect([...map]).toEqual([['ws://a', 'Kitchen']]);
    expect(backendFriendlyNames(null).size).toBe(0);
  });
});
