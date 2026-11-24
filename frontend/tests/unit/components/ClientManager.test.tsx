import { describe, it, expect, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { ClientManager } from '../../../components/ClientManager';
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
