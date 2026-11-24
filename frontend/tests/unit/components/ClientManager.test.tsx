import { describe, it, expect, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { ClientManager } from '../../../components/ClientManager';
import { SyncedDevices } from '../../../components/SyncedDevices';
import { createMockClient, createMockStream } from '../../mocks/mockTypes';

describe('ClientManager device volume slider', () => {
  const stream = createMockStream({ id: 'unit-a::airplay', name: 'Kitchen AirPlay' });
  const client = createMockClient({ id: 'player-1', name: 'Bedroom', currentStreamId: stream.id, volume: 40 });

  function renderManager(onVolumeChange = vi.fn()) {
    render(
      <ClientManager
        clients={[client]}
        streams={[stream]}
        myClientStreamId={null}
        onVolumeChange={onVolumeChange}
        onStreamChange={vi.fn()}
        onGroupVolumeAdjust={vi.fn()}
        onGroupMute={vi.fn()}
      />,
    );
    return onVolumeChange;
  }

  it('names the slider for the device, the same way SyncedDevices does', () => {
    renderManager();
    const slider = screen.getByRole('slider', { name: 'Bedroom volume control' });
    expect(slider).toHaveValue('40');
  });

  it('reports a change for that device', () => {
    const onVolumeChange = renderManager();
    fireEvent.change(screen.getByRole('slider', { name: 'Bedroom volume control' }), { target: { value: '55' } });
    expect(onVolumeChange).toHaveBeenCalledWith('player-1', 55);
  });
});

describe('the Offline badge on a device row', () => {
  const stream = createMockStream({ id: 'unit-a::airplay', name: 'Kitchen AirPlay' });
  const manager = (clients: ReturnType<typeof createMockClient>[]) =>
    render(
      <ClientManager
        clients={clients}
        streams={[stream]}
        myClientStreamId={stream.id}
        onVolumeChange={vi.fn()}
        onStreamChange={vi.fn()}
        onGroupVolumeAdjust={vi.fn()}
        onGroupMute={vi.fn()}
      />,
    );
  const synced = (clients: ReturnType<typeof createMockClient>[]) =>
    render(
      <SyncedDevices
        clients={clients}
        streams={[stream]}
        onVolumeChange={vi.fn()}
        onStreamChange={vi.fn()}
        onGroupVolumeAdjust={vi.fn()}
        onGroupMute={vi.fn()}
      />,
    );
  const playing = (patch = {}) =>
    createMockClient({ id: 'player-1', name: 'Bedroom', currentStreamId: stream.id, volume: 40, ...patch });
  const idle = (patch = {}) =>
    createMockClient({ id: 'player-2', name: 'Garage', currentStreamId: null, volume: 40, ...patch });

  it('shows on a ClientManager row that plays a stream, and keeps the controls live', () => {
    manager([playing({ offline: true })]);
    expect(screen.getByText('Offline')).toBeInTheDocument();
    expect(screen.getByText('Bedroom is offline')).toBeInTheDocument();
    expect(screen.getByRole('slider', { name: 'Bedroom volume control' })).toBeEnabled();
  });

  it('shows on an idle ClientManager row, and keeps Join Stream live', () => {
    manager([idle({ offline: true })]);
    expect(screen.getByText('Offline')).toBeInTheDocument();
    expect(screen.getByText('Garage is offline')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Join Stream/ })).toBeEnabled();
  });

  it('shows on a SyncedDevices row, and keeps the controls live', () => {
    synced([playing({ offline: true })]);
    expect(screen.getByText('Offline')).toBeInTheDocument();
    expect(screen.getByRole('slider', { name: 'Bedroom volume control' })).toBeEnabled();
  });

  it('does not show when the field is false or missing', () => {
    manager([playing({ offline: false }), idle()]);
    synced([playing()]);
    expect(screen.queryByText('Offline')).not.toBeInTheDocument();
  });
});
