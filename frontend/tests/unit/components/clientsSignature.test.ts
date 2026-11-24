/**
 * Unit tests for MeshApp's `clientsSignature`.
 *
 * The device lists are memoised on this string. A field the lists render or filter on, and that is
 * missing here, gives a row that stays stale until some OTHER field changes — with no error.
 */

import { describe, it, expect } from 'vitest';
import { clientsSignature } from '../../../MeshApp';
import type { Client } from '../../../types';

const base: Client = {
  id: 'player-1',
  name: 'Kitchen',
  currentStreamId: null,
  volume: 40,
  connected: true,
  isLocal: false,
  url: 'ws://192.0.2.10:8928/sendspin',
  pairingState: 'cleartext',
};

const changes = (patch: Partial<Client>) => clientsSignature([base]) !== clientsSignature([{ ...base, ...patch }]);

describe('clientsSignature', () => {
  it('is stable for an equal list in a new array', () => {
    expect(clientsSignature([{ ...base }])).toBe(clientsSignature([{ ...base }]));
  });

  it.each([
    ['name', { name: 'Kitchen Speaker' }],
    ['currentStreamId', { currentStreamId: 'unit-a::airplay' }],
    ['volume', { volume: 41 }],
    ['connected', { connected: false }],
    ['pairingState', { pairingState: 'unpaired' as const }],
  ])('changes with %s (already covered)', (_field, patch) => {
    expect(changes(patch)).toBe(true);
  });

  it.each([
    ['url', { url: 'ws://192.0.2.99:8928/sendspin' }],
    ['isForeign', { isForeign: true }],
    ['isLocal', { isLocal: true }],
    ['foreignServer appearing', { foreignServer: { name: 'Music Assistant' } }],
  ])('changes with %s', (_field, patch) => {
    expect(changes(patch)).toBe(true);
  });

  it('changes when the foreign server plays the next track', () => {
    const on = (title: string, artist: string) =>
      clientsSignature([{ ...base, foreignServer: { name: 'Music Assistant', title, artist } }]);
    expect(on('Track One', 'Artist A')).not.toBe(on('Track Two', 'Artist A'));
    expect(on('Track One', 'Artist A')).not.toBe(on('Track One', 'Artist B'));
  });

  it('changes when the foreign server itself changes', () => {
    const held = (name: string) => clientsSignature([{ ...base, foreignServer: { name } }]);
    expect(held('Music Assistant')).not.toBe(held('Other Server'));
  });

  it('does not let one field read as another', () => {
    // A separator inside a value must not make two different lists equal.
    const a = clientsSignature([{ ...base, name: 'A|B', url: 'C' }]);
    const b = clientsSignature([{ ...base, name: 'A', url: 'B|C' }]);
    expect(a).not.toBe(b);
  });
});
