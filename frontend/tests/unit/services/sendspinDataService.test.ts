import { describe, it, expect } from 'vitest';
import { mapViewToModel, streamId, parseStreamId, MeshView } from '../../../services/sendspinDataService';
import { NowPlaying, currentPositionMs, SendspinControllerClient } from '../../../services/sendspinControllerClient';

function np(partial: Partial<NowPlaying>): NowPlaying {
  return { groupId: null, groupName: null, playbackState: 'unknown', supportedCommands: [], ...partial };
}

const VIEW: MeshView = {
  units: [
    {
      unit_id: 'unit-133',
      name: 'Pi4-02',
      host: '192.0.2.10',
      sources: [{ source_id: 'airplay', group_id: 'gA', group_name: 'AirPlay', streaming: true, player_ids: ['player-133'] }],
      players: [{ player_id: 'player-133', name: 'Player-133', connected: true, group_id: 'gA', url: 'ws://192.0.2.10:8928/sendspin' }],
    },
    {
      unit_id: 'unit-113',
      name: 'PoE-Temp',
      host: '192.0.2.11',
      sources: [{ source_id: 'airplay', group_id: 'gB', group_name: 'AirPlay', streaming: true, player_ids: [] }],
      // player-113 has roamed to unit-133's group gA (appears under the unit it's connected to)
      players: [],
    },
  ],
};

describe('mapViewToModel', () => {
  it('maps units->servers, sources->streams, players->clients', () => {
    const m = mapViewToModel(VIEW, new Map(), new Map());
    expect(m.servers.map((s) => s.id)).toEqual(['unit-133', 'unit-113']);
    expect(m.streams.map((s) => s.id)).toEqual(['unit-133::airplay', 'unit-113::airplay']);
    expect(m.clients.map((c) => c.id)).toEqual(['player-133']);
  });

  it('derives currentStreamId from the player group_id matching a source group_id', () => {
    const m = mapViewToModel(VIEW, new Map(), new Map());
    expect(m.clients[0].currentStreamId).toBe('unit-133::airplay');
  });

  it('resolves a roamed player to a peer unit source group', () => {
    // player-113 connected to unit-133's server (group gA) though it lives on unit-113
    const view: MeshView = structuredClone(VIEW);
    view.units[0].players.push({ player_id: 'player-113', name: 'Player-113', connected: true, group_id: 'gA', url: 'x' });
    const m = mapViewToModel(view, new Map(), new Map());
    const roamed = m.clients.find((c) => c.id === 'player-113')!;
    expect(roamed.currentStreamId).toBe('unit-133::airplay');
    expect(roamed.serverId).toBe('unit-133');
  });

  it('null currentStreamId when the player is in no source group', () => {
    const view: MeshView = structuredClone(VIEW);
    view.units[0].players[0].group_id = 'orphan';
    const m = mapViewToModel(view, new Map(), new Map());
    expect(m.clients[0].currentStreamId).toBeNull();
  });

  it('treats playbackSpeed 0 as paused (the only out-of-band AirPlay pause signal)', () => {
    // group playback_state stays 'playing' through a source pause; speed 0 is what flips the UI.
    const npByGroup = new Map([['gA', np({ groupId: 'gA', playbackState: 'playing', title: 'Rebel Yell', playbackSpeed: 0, trackProgressMs: 137749, trackDurationMs: 288580 })]]);
    const m = mapViewToModel(VIEW, npByGroup, new Map());
    const s = m.streams.find((s) => s.id === 'unit-133::airplay')!;
    expect(s.isPlaying).toBe(false);
    expect(s.playback!.playback_status).toBe('paused');
    expect(s.currentTrack.title).toBe('Rebel Yell'); // metadata retained through the pause
  });

  it('merges now-playing metadata onto the matching stream', () => {
    const npByGroup = new Map([['gA', np({ groupId: 'gA', playbackState: 'playing', title: 'Redbone', artist: 'Childish Gambino', album: 'Awaken', artworkUrl: 'blob:x', trackDurationMs: 326000 })]]);
    const m = mapViewToModel(VIEW, npByGroup, new Map());
    const s = m.streams.find((s) => s.id === 'unit-133::airplay')!;
    expect(s.currentTrack.title).toBe('Redbone');
    expect(s.currentTrack.duration).toBe(326); // seconds
    expect(s.currentTrack.albumArtUrl).toBe('blob:x');
    expect(s.isPlaying).toBe(true);
    // the other stream has no now-playing -> structural fallback
    const other = m.streams.find((s) => s.id === 'unit-113::airplay')!;
    expect(other.currentTrack.title).toBe('');
    expect(other.isPlaying).toBe(true); // from streaming=true
    expect(other.playback!.is_stale).toBe(true);
  });
});

describe('streamId helpers', () => {
  it('round-trips unit/source even when source has a colon-ish name', () => {
    const id = streamId('unit-113', 'airplay');
    expect(parseStreamId(id)).toEqual({ unitId: 'unit-113', sourceId: 'airplay' });
  });
});

describe('unit identity + source state', () => {
  it('marks the responder\'s own player local BY LISTENER HOST, even after it roams', () => {
    // player-113 lives on unit-113 but is currently connected to unit-133's server (a roam):
    // it must still count as unit-113's own player when unit-113 serves the page.
    const view: MeshView = structuredClone(VIEW);
    view.local_unit_id = 'unit-113';
    view.units[0].players.push({
      player_id: 'player-113', name: 'Player-113', connected: true, group_id: 'gA',
      url: 'ws://192.0.2.11:8928/sendspin',
    });
    const m = mapViewToModel(view, new Map(), new Map());
    expect(m.localUnitId).toBe('unit-113');
    expect(m.localPlayerIds).toEqual(['player-113']);
    expect(m.clients.find((c) => c.id === 'player-133')!.isLocal).toBe(false);
  });

  it('claims no players when the view does not say who answered', () => {
    const m = mapViewToModel(VIEW, new Map(), new Map());
    expect(m.localPlayerIds).toEqual([]);
    expect(m.clients.every((c) => !c.isLocal)).toBe(true);
  });

  it('labels a stream with the endpoint device name and carries `active` through', () => {
    const view: MeshView = structuredClone(VIEW);
    view.units[0].sources[0].name = 'Kitchen';
    view.units[0].sources[0].active = true;
    const m = mapViewToModel(view, new Map(), new Map());
    expect(m.streams[0].name).toBe('Kitchen');
    expect(m.streams[0].active).toBe(true);
    expect(m.streams[1].active).toBe(false); // absent in the wire form = idle
  });
});

describe('currentPositionMs extrapolation', () => {
  const base = np({ playbackState: 'playing', trackProgressMs: 10000, trackDurationMs: 300000, playbackSpeed: 1000, timestampUs: 5_000_000 });

  it('advances by wall time at 1x when playing (offset aligns server/local clocks)', () => {
    // choose nowMs so nowMs*1000 + offset = timestampUs + 2s
    const offset = base.timestampUs! - 1000 * 1000; // pretend local clock at t=1s == server timestamp
    const pos = currentPositionMs(base, offset, 3000); // 2s of local time after the anchor
    expect(Math.round(pos)).toBe(12000); // 10s + 2s
  });

  it('halts at track_progress when paused (playbackSpeed 0)', () => {
    const paused = { ...base, playbackSpeed: 0, playbackState: 'paused' as const };
    expect(currentPositionMs(paused, 0, 999999999)).toBe(10000);
  });

  it('clamps to duration', () => {
    const offset = base.timestampUs! - 1000 * 1000;
    const pos = currentPositionMs(base, offset, 1000 + 1_000_000); // absurdly far future
    expect(pos).toBe(300000);
  });

  it('returns 0 with no progress info', () => {
    expect(currentPositionMs(np({ playbackState: 'playing' }), 0)).toBe(0);
  });
});
