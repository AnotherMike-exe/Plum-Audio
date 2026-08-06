import { describe, it, expect, beforeEach } from 'vitest';
import { mapViewToModel, streamId, parseStreamId, MeshView, SendspinDataService } from '../../../services/sendspinDataService';
import { NowPlaying, currentPositionMs, SendspinControllerClient, TimeFilter } from '../../../services/sendspinControllerClient';

function np(partial: Partial<NowPlaying>): NowPlaying {
  return { groupId: null, groupName: null, playbackState: 'unknown', supportedCommands: [], ...partial };
}

const VIEW: MeshView = {
  units: [
    {
      unit_id: 'unit-210',
      name: 'Pi4-02',
      host: '192.0.2.10',
      sources: [{ source_id: 'airplay', group_id: 'gA', group_name: 'AirPlay', streaming: true, player_ids: ['player-210'] }],
      players: [{ player_id: 'player-210', name: 'Player-210', connected: true, group_id: 'gA', url: 'ws://192.0.2.10:8928/sendspin' }],
    },
    {
      unit_id: 'unit-211',
      name: 'PoE-Temp',
      host: '192.0.2.11',
      sources: [{ source_id: 'airplay', group_id: 'gB', group_name: 'AirPlay', streaming: true, player_ids: [] }],
      // player-211 has roamed to unit-210's group gA (appears under the unit it's connected to)
      players: [],
    },
  ],
};

describe('mapViewToModel', () => {
  it('maps units->servers, sources->streams, players->clients', () => {
    const m = mapViewToModel(VIEW, new Map(), new Map());
    expect(m.servers.map((s) => s.id)).toEqual(['unit-210', 'unit-211']);
    expect(m.streams.map((s) => s.id)).toEqual(['unit-210::airplay', 'unit-211::airplay']);
    expect(m.clients.map((c) => c.id)).toEqual(['player-210']);
  });

  it('derives currentStreamId from the player group_id matching a source group_id', () => {
    const m = mapViewToModel(VIEW, new Map(), new Map());
    expect(m.clients[0].currentStreamId).toBe('unit-210::airplay');
  });

  it('resolves a roamed player to a peer unit source group', () => {
    // player-211 connected to unit-210's server (group gA) though it lives on unit-211
    const view: MeshView = structuredClone(VIEW);
    view.units[0].players.push({ player_id: 'player-211', name: 'Player-211', connected: true, group_id: 'gA', url: 'x' });
    const m = mapViewToModel(view, new Map(), new Map());
    const roamed = m.clients.find((c) => c.id === 'player-211')!;
    expect(roamed.currentStreamId).toBe('unit-210::airplay');
    expect(roamed.serverId).toBe('unit-210');
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
    const s = m.streams.find((s) => s.id === 'unit-210::airplay')!;
    expect(s.isPlaying).toBe(false);
    expect(s.playback!.playback_status).toBe('paused');
    expect(s.currentTrack.title).toBe('Rebel Yell'); // metadata retained through the pause
  });

  it('merges now-playing metadata onto the matching stream', () => {
    const npByGroup = new Map([['gA', np({ groupId: 'gA', playbackState: 'playing', title: 'Redbone', artist: 'Childish Gambino', album: 'Awaken', artworkUrl: 'blob:x', trackDurationMs: 326000 })]]);
    const m = mapViewToModel(VIEW, npByGroup, new Map());
    const s = m.streams.find((s) => s.id === 'unit-210::airplay')!;
    expect(s.currentTrack.title).toBe('Redbone');
    expect(s.currentTrack.duration).toBe(326); // seconds
    expect(s.currentTrack.albumArtUrl).toBe('blob:x');
    expect(s.isPlaying).toBe(true);
    // the other stream has no now-playing -> structural fallback
    const other = m.streams.find((s) => s.id === 'unit-211::airplay')!;
    expect(other.currentTrack.title).toBe('');
    expect(other.isPlaying).toBe(true); // from streaming=true
    expect(other.playback!.is_stale).toBe(true);
  });
});

describe('streamId helpers', () => {
  it('round-trips unit/source even when source has a colon-ish name', () => {
    const id = streamId('unit-211', 'airplay');
    expect(parseStreamId(id)).toEqual({ unitId: 'unit-211', sourceId: 'airplay' });
  });
});

describe('unit identity + source state', () => {
  it('marks the responder\'s own player local BY LISTENER HOST, even after it roams', () => {
    // player-211 lives on unit-211 but is currently connected to unit-210's server (a roam):
    // it must still count as unit-211's own player when unit-211 serves the page.
    const view: MeshView = structuredClone(VIEW);
    view.local_unit_id = 'unit-211';
    view.units[0].players.push({
      player_id: 'player-211', name: 'Player-211', connected: true, group_id: 'gA',
      url: 'ws://192.0.2.11:8928/sendspin',
    });
    const m = mapViewToModel(view, new Map(), new Map());
    expect(m.localUnitId).toBe('unit-211');
    expect(m.localPlayerIds).toEqual(['player-211']);
    expect(m.clients.find((c) => c.id === 'player-210')!.isLocal).toBe(false);
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

describe('the two volumes', () => {
  it('reads each endpoint\'s real level from the snapshot', () => {
    const view: MeshView = structuredClone(VIEW);
    view.units[0].players[0].volume = 42;
    view.units[0].players[0].muted = true;
    const m = mapViewToModel(view, new Map(), new Map());
    expect(m.clients[0].volume).toBe(42);
    expect(m.clients[0].muted).toBe(true);
  });

  it('falls back to full volume for a player whose unit predates the field', () => {
    const m = mapViewToModel(VIEW, new Map(), new Map());
    expect(m.clients[0].volume).toBe(100);
  });

  it('keeps group volume (controller role) and source volume (the sender) separate', () => {
    const view: MeshView = structuredClone(VIEW);
    view.units[0].sources[0].source_volume = 30;
    view.units[0].sources[0].supports_source_volume = true;
    const npByGroup = new Map([['gA', np({ groupId: 'gA', volume: 75 })]]);
    const m = mapViewToModel(view, npByGroup, new Map());
    expect(m.streams[0].volume).toBe(75);        // our endpoints, from the controller role
    expect(m.streams[0].sourceVolume).toBe(30);  // the phone's own slider, from our snapshot
    expect(m.streams[0].supportsSourceVolume).toBe(true);
  });

  it('reports no source volume for a source that cannot do it (slider stays hidden)', () => {
    const m = mapViewToModel(VIEW, new Map(), new Map());
    expect(m.streams[0].sourceVolume).toBeUndefined();
    expect(m.streams[0].supportsSourceVolume).toBe(false);
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

describe('TimeFilter (clock sync)', () => {
  it('is unsynchronized until it has enough samples, then reports the best-delay offset', () => {
    const f = new TimeFilter();
    expect(f.synchronized).toBe(false);
    f.update(1000, 50);
    f.update(1000, 5);
    f.update(1000, 80);
    expect(f.synchronized).toBe(true);
    expect(f.offsetUs).toBe(1000);
    expect(f.errorUs).toBe(5);
  });

  it('picks the minimum-delay sample regardless of order or offset noise', () => {
    const f = new TimeFilter();
    f.update(2200, 40);
    f.update(2000, 3);
    f.update(1800, 60);
    expect(f.offsetUs).toBe(2000);
  });

  it('reproduces the aiosendspin NTP offset/delay formula', () => {
    const ct = 1000, sr = 6000, st = 6000, now = 2000;
    const offset = ((sr - ct) + (st - now)) / 2;
    const delay = ((now - ct) - (st - sr)) / 2;
    expect(offset).toBe(4500);
    expect(delay).toBe(500);
  });
});

describe('speaker names survive going idle', () => {
  // A speaker names itself twice: over the protocol while it is attached ("Home Assistant Voice
  // PE - 01"), and over mDNS while it is not (the bare instance name, because a third-party device
  // usually publishes no `name` TXT key). The row used to rename itself on every join/leave.
  const URL = 'ws://198.51.100.30:8927/sendspin';
  const ATTACHED: MeshView = {
    local_unit_id: 'unit-10021',
    units: [
      {
        unit_id: 'unit-10021',
        name: 'Plum Amp100',
        host: '198.51.100.21',
        sources: [{ source_id: 'airplay', group_id: 'gA', group_name: 'AirPlay', streaming: true, player_ids: ['aa:bb:cc'] }],
        players: [{ player_id: 'aa:bb:cc', name: 'Home Assistant Voice PE - 01', connected: true, group_id: 'gA', url: URL }],
      },
    ],
  };
  const IDLE: MeshView = {
    local_unit_id: 'unit-10021',
    units: [{ ...ATTACHED.units[0], players: [] }],
  };
  const NEIGHBOURHOOD = {
    players: [{ name: 'home-assistant-voice-a1b2c3', friendly_name: 'home-assistant-voice-a1b2c3', url: URL, host: '198.51.100.30', port: 8927, is_own: false }],
    servers: [],
  };

  beforeEach(() => window.localStorage.clear());

  it('keeps the protocol name once the speaker leaves the group', () => {
    const svc = new SendspinDataService();
    // @ts-expect-error — driving the poll's private hand-off directly; no network in a unit test.
    svc.applyView(ATTACHED);
    // @ts-expect-error — same: the neighbourhood is normally filled by the second poll fetch.
    svc.neighbourhood = NEIGHBOURHOOD;
    expect(svc.snapshot().clients.find((c) => c.url === URL)!.name).toBe('Home Assistant Voice PE - 01');

    // @ts-expect-error — the speaker goes idle: no unit lists it, only mDNS still sees it.
    svc.applyView(IDLE);
    const idle = svc.snapshot().clients.find((c) => c.url === URL)!;
    expect(idle.currentStreamId).toBeNull();
    expect(idle.name).toBe('Home Assistant Voice PE - 01'); // not "home-assistant-voice-a1b2c3"
  });

  it('learns the name across a reload, and lets a rename win', () => {
    // @ts-expect-error — private hand-off, as above.
    new SendspinDataService().applyView(ATTACHED);

    const reloaded = new SendspinDataService();      // fresh instance reads the persisted memo
    // @ts-expect-error — private field.
    reloaded.neighbourhood = NEIGHBOURHOOD;
    // @ts-expect-error — private hand-off.
    reloaded.applyView(IDLE);
    expect(reloaded.snapshot().clients.find((c) => c.url === URL)!.name).toBe('Home Assistant Voice PE - 01');

    const renamed: MeshView = structuredClone(ATTACHED);
    renamed.units[0].players[0].name = 'Kitchen';
    // @ts-expect-error — private hand-off.
    reloaded.applyView(renamed);
    expect(reloaded.snapshot().clients.find((c) => c.url === URL)!.name).toBe('Kitchen');
  });
});

describe('a live unit rename reaches its PEERS', () => {
  // Reported from the rig 2026-08-05: renaming unit-210 updated its own GUI but not unit-211's,
  // and a page refresh did not help.
  //
  // Two names again, but a different pair. `players[].name` is what the speaker declared at the
  // Sendspin HANDSHAKE, and that is fixed at connect — a unit renamed while attached keeps
  // announcing the old name to its server until the audio process restarts, which is deliberate
  // (restarting it to apply a rename would drop playback). `local_player` is the unit's own
  // self-report, driven from settings.json, and it reflects a rename within a poll.
  //
  // The memo learned only from the handshake name, and it is PERSISTED — so a peer's GUI pinned
  // the pre-rename name and a refresh restored it from localStorage rather than fixing it.
  const URL = 'ws://192.0.2.10:8928/sendspin';

  const view = (handshakeName: string, selfReportName: string): MeshView => ({
    local_unit_id: 'unit-211',
    units: [
      {
        unit_id: 'unit-211',
        name: '113 Sendspin',
        host: '192.0.2.11',
        sources: [{ source_id: 'airplay-1', group_id: 'gA', group_name: 'AirPlay', streaming: true, player_ids: ['player-210'] }],
        // The PEER's server holds unit-210's speaker, carrying its handshake name.
        players: [{ player_id: 'player-210', name: handshakeName, connected: true, group_id: 'gA', url: URL }],
      },
      {
        unit_id: 'unit-210',
        name: selfReportName,
        host: '192.0.2.10',
        sources: [],
        players: [],
        local_player: { player_id: 'player-210', name: selfReportName, url: URL, attached: true },
      },
    ],
  });

  beforeEach(() => window.localStorage.clear());

  const nameOf = (svc: SendspinDataService) => svc.snapshot().clients.find((c) => c.url === URL)!.name;

  it('prefers the self-report over a stale handshake name', () => {
    const svc = new SendspinDataService();
    // @ts-expect-error — private hand-off; no network in a unit test.
    svc.applyView(view('Pi4-02', 'Pi4-02'));
    expect(nameOf(svc)).toBe('Pi4-02');

    // The rename: settings.json moved, so the self-report has it. The handshake name has NOT
    // changed and will not until unit-210's audio process restarts.
    // @ts-expect-error — private hand-off.
    svc.applyView(view('Pi4-02', 'Pi4-02-Renamed'));
    expect(nameOf(svc)).toBe('Pi4-02-Renamed');
  });

  it('does not let the stale handshake name win back on the next poll', () => {
    const svc = new SendspinDataService();
    // @ts-expect-error — private hand-off.
    svc.applyView(view('Pi4-02', 'Pi4-02-Renamed'));
    // @ts-expect-error — poll again with the same (still stale) handshake name.
    svc.applyView(view('Pi4-02', 'Pi4-02-Renamed'));
    expect(nameOf(svc)).toBe('Pi4-02-Renamed');
  });

  it('persists the new name, so a refresh does not restore the old one', () => {
    // @ts-expect-error — private hand-off.
    new SendspinDataService().applyView(view('Pi4-02', 'Pi4-02-Renamed'));

    const reloaded = new SendspinDataService();   // reads the persisted memo, as a refresh would
    // @ts-expect-error — private hand-off.
    reloaded.applyView(view('Pi4-02', 'Pi4-02-Renamed'));
    expect(nameOf(reloaded)).toBe('Pi4-02-Renamed');
  });
});
