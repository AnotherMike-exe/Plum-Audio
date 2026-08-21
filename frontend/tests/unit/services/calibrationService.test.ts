/**
 * Unit tests for calibrationService.
 *
 * These exercise the SERVICE against handlers shaped like the real backends, and concentrate on the
 * three things the UI can get wrong:
 *
 *   - WHICH BACKEND. Curves are configuration and live on the Flask config API; the tone lives on
 *     the mesh API, because playing it means creating a source and routing a player and only the
 *     audio loop can do that. Sending either to the other silently 404s.
 *   - READ MERGED, WRITE LOCAL. A record is saved to the unit serving the page, but the endpoint it
 *     describes may be grouped elsewhere, so reads come from the mesh's merged view. If reads went
 *     local-only, which unit's page you opened would change what the tab showed.
 *   - A REJECTED FIT IS NOT A CRASH. Flat measurements come back as a 400 with an explanation, and
 *     the user has to see it — that is the predecessor's exact failure, stored silently.
 */

import { describe, it, expect, afterEach, beforeAll, afterAll, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { calibrationService } from '../../../services/calibrationService'

const CONFIG = '/api/audio/calibration'
// Resolved exactly as the service does. A dev .env points VITE_MESH_API_URL at a real rig unit, so
// hard-coding '/api/mesh' here would leave the tone handlers unmatched and the tests would "fail"
// for a reason that has nothing to do with the service.
const MESH = import.meta.env.VITE_MESH_API_URL || '/api/mesh'

function record(overrides: Record<string, unknown> = {}) {
  return {
    playerId: 'kitchen',
    name: 'Kitchen',
    url: 'ws://192.168.1.20:8928/sendspin',
    enabled: true,
    samples: [{ volume: 35, db: 57 }, { volume: 85, db: 65 }],
    maxLimit: { mode: 'percentage', value: 100 },
    trimDb: 0,
    lastCalibrated: '2026-08-20T10:00:00+00:00',
    calibrated: true,
    fitRejected: false,
    curve: { a: 20.1, b: 25.9, n: 2, rmsError: 0, suspect: false },
    effectiveMaxVolume: 100,
    dbRange: { lowVolume: 10, lowDb: 46, highVolume: 100, highDb: 66 },
    ...overrides,
  }
}

const server = setupServer(
  http.get(CONFIG, () =>
    HttpResponse.json({
      calibrations: { kitchen: record() },
      policy: { mode: 'follow', sets: [] },
      modes: ['off', 'stream', 'follow', 'sets'],
      suggestedVolumes: [35, 60, 85],
      minSamples: 2,
      maxSamples: 5,
    }),
  ),
  http.get(`${MESH}/calibration`, () =>
    HttpResponse.json({ kitchen: record(), living: record({ playerId: 'living', name: 'Living' }) }),
  ),
  // Registered BEFORE `/:playerId`: MSW matches in order, so the parameterised route would
  // otherwise swallow /policy. (Flask ranks static segments above converters, so the real
  // backend needs no such care — see test_calibration_api.py's policy tests.)
  http.put(`${CONFIG}/policy`, async ({ request }) => {
    const policy = await request.json()
    return HttpResponse.json({ calibrations: {}, policy, modes: [], suggestedVolumes: [], minSamples: 2, maxSamples: 5 })
  }),
  http.put(`${CONFIG}/:playerId`, async ({ request, params }) => {
    const body = (await request.json()) as { samples?: Array<{ volume: number; db: number }> }
    const samples = body.samples ?? []
    // Mirror the backend's refusal: loudness must rise with volume.
    const flat = samples.length >= 2 && samples.every((s) => s.db === samples[0].db)
    if (flat) {
      return HttpResponse.json(
        { error: 'these measurements do not describe a speaker: loudness must rise with volume.', fitRejected: true },
        { status: 400 },
      )
    }
    return HttpResponse.json(record({ playerId: params.playerId as string, samples }))
  }),
  http.delete(`${CONFIG}/:playerId`, () =>
    HttpResponse.json({ calibrations: {}, policy: { mode: 'follow', sets: [] }, modes: [], suggestedVolumes: [], minSamples: 2, maxSamples: 5 }),
  ),
  http.get(`${MESH}/calibration/tone`, () => HttpResponse.json({ playing: false })),
  http.post(`${MESH}/calibration/tone`, async ({ request }) => {
    const body = (await request.json()) as { player_id: string; volume: number; type: string }
    return HttpResponse.json({ playing: true, playerId: body.player_id, volume: body.volume, toneType: body.type })
  }),
  http.post(`${MESH}/calibration/tone/volume`, async ({ request }) => {
    const { volume } = (await request.json()) as { volume: number }
    return HttpResponse.json({ playing: true, playerId: 'kitchen', volume })
  }),
  http.post(`${MESH}/calibration/tone/stop`, () => HttpResponse.json({ playing: false })),
  http.get(`${MESH}/neighbourhood`, () =>
    HttpResponse.json({
      players: [
        // An idle third-party speaker: mDNS only, nothing holds it.
        { name: 'home-assistant-voice-a1b2c3', friendly_name: 'Voice PE - 01', url: 'ws://192.168.1.87:8927/sendspin', host: '192.168.1.87', is_own: false },
        // Our own player, which the mesh view already covers.
        { name: 'unit-a-player', url: 'ws://a:8928/sendspin', host: 'a', is_own: true },
        // An ALREADY-ADOPTED speaker: it is in unit.players under its MAC, so it must not double up.
        { name: 'esp32-kitchen', url: 'ws://192.168.1.55:8927/sendspin', host: '192.168.1.55', is_own: false },
      ],
      servers: [],
    }),
  ),
  http.get(`${MESH}/view`, () =>
    HttpResponse.json({
      local_unit_id: 'unit-a',
      units: [
        {
          unit_id: 'unit-a',
          name: 'Living Room',
          players: [
            { player_id: 'living', name: 'Living', url: 'ws://a:8928/sendspin', volume: 40, connected: true },
            // Server-side bookkeeping, not speakers.
            { player_id: 'src:airplay-1', name: 'anchor', volume: 100, connected: true },
            { player_id: 'ctrl:airplay-1:abc', name: 'gui', volume: 100, connected: true },
          ],
          local_player: { player_id: 'living', name: 'Living', volume: 40 },
        },
        {
          unit_id: 'unit-c',
          name: 'Den',
          host: '192.168.1.30',
          players: [
            // A third-party speaker adopted onto this unit: real id is its MAC, and it carries the
            // URL mDNS also advertises.
            { player_id: 'aa:bb:cc:dd:ee:ff', name: 'ESP32 Kitchen', url: 'ws://192.168.1.55:8927/sendspin', volume: 100, connected: true },
          ],
          local_player: { player_id: 'den', name: 'Den', volume: 55 },
        },
        {
          unit_id: 'unit-b',
          name: 'Kitchen',
          players: [],
          // Claimed by another server, so it is in no unit's `players` — only the self-report.
          local_player: { player_id: 'kitchen', name: 'Kitchen', url: 'ws://b:8928/sendspin', volume: 70 },
        },
      ],
    }),
  ),
)

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

describe('calibrationService', () => {
  describe('reading', () => {
    it('reads the local snapshot with its policy and wizard bounds', async () => {
      const snap = await calibrationService.getSnapshot()
      expect(snap.policy.mode).toBe('follow')
      expect(snap.suggestedVolumes).toEqual([35, 60, 85])
      expect(snap.maxSamples).toBe(5)
      expect(snap.calibrations.kitchen.calibrated).toBe(true)
    })

    it('reads records merged across the mesh, not just this unit', async () => {
      expect(Object.keys(await calibrationService.getMerged()).sort()).toEqual(['kitchen', 'living'])
    })
  })

  describe('writing', () => {
    it('saves a record to the config API and returns the derived curve', async () => {
      const saved = await calibrationService.save('kitchen', {
        name: 'Kitchen',
        url: null,
        enabled: true,
        samples: [{ volume: 35, db: 57 }, { volume: 85, db: 65 }],
        maxLimit: { mode: 'percentage', value: 100 },
        trimDb: 0,
        knownRev: 0,
      })
      expect(saved.curve?.a).toBeCloseTo(20.1)
    })

    it('surfaces a rejected fit as an error the wizard can show', async () => {
      await expect(
        calibrationService.save('kitchen', {
          name: 'Kitchen',
          url: null,
          enabled: true,
          samples: [{ volume: 38, db: 62 }, { volume: 80, db: 62 }],
          maxLimit: { mode: 'percentage', value: 100 },
          trimDb: 0,
          knownRev: 0,
        }),
      ).rejects.toThrow(/rise with volume/)
    })

    it('sends the policy to the config API', async () => {
      const result = await calibrationService.setPolicy({ mode: 'sets', sets: [{ id: 's1', name: 'Open plan', members: ['a', 'b'] }] })
      expect(result.policy.mode).toBe('sets')
      expect(result.policy.sets[0].members).toEqual(['a', 'b'])
    })

    it('deletes a record', async () => {
      expect((await calibrationService.remove('kitchen')).calibrations).toEqual({})
    })
  })

  describe('the tone', () => {
    it('starts on the MESH api, not the config api', async () => {
      const spy = vi.spyOn(globalThis, 'fetch')
      await calibrationService.toneStart('kitchen', 40, { type: 'pink' })
      expect(String(spy.mock.calls[0][0])).toContain('/api/mesh/calibration/tone')
      spy.mockRestore()
    })

    it('passes the endpoint and level through', async () => {
      const state = await calibrationService.toneStart('kitchen', 40, { type: 'sine' })
      expect(state).toMatchObject({ playing: true, playerId: 'kitchen', volume: 40, toneType: 'sine' })
    })

    it('re-levels without restarting', async () => {
      expect(await calibrationService.toneVolume(75)).toMatchObject({ playing: true, volume: 75 })
    })

    it('stops', async () => {
      expect(await calibrationService.toneStop()).toEqual({ playing: false })
    })
  })

  describe('endpoints', () => {
    it('lists real speakers and drops server-side bookkeeping clients', async () => {
      const ids = (await calibrationService.getEndpoints()).map((e) => e.playerId)
      expect(ids).toEqual(expect.arrayContaining(['kitchen', 'living']))
      // `src:` anchors a source's group and `ctrl:` is a GUI controller websocket. Neither renders
      // audio, so neither can be calibrated.
      expect(ids.some((id) => id.startsWith('src:') || id.startsWith('ctrl:'))).toBe(false)
    })

    it('keeps a speaker that is claimed by another server, via the self-report', async () => {
      const kitchen = (await calibrationService.getEndpoints()).find((e) => e.playerId === 'kitchen')
      expect(kitchen).toMatchObject({ name: 'Kitchen', unitName: 'Kitchen', volume: 70 })
    })
  })

  describe('presentation', () => {
    it('reports an endpoint pinned at its ceiling', () => {
      const capped = { ...record({ effectiveMaxVolume: 60 }) } as never
      expect(calibrationService.isAtLimit(capped, 60)).toBe(true)
      expect(calibrationService.isAtLimit(capped, 40)).toBe(false)
    })

    it('never calls an uncalibrated endpoint at-limit', () => {
      const raw = { ...record({ calibrated: false, effectiveMaxVolume: 100 }) } as never
      expect(calibrationService.isAtLimit(raw, 100)).toBe(false)
    })

    it('summarises a calibrated endpoint', () => {
      const summary = calibrationService.summarize(record({ trimDb: -3 }) as never)
      expect(summary).toContain('46–66 dB')
      expect(summary).toContain('-3 dB trim')
    })

    it('says so when a fit was rejected rather than showing a range', () => {
      const summary = calibrationService.summarize(record({ fitRejected: true, dbRange: null }) as never)
      expect(summary).toMatch(/rejected/i)
    })

    it('handles an endpoint with no record at all', () => {
      expect(calibrationService.summarize(undefined)).toBe('Not calibrated')
    })
  })
})

describe('calibrationService — third-party endpoints', () => {
  it('lists an idle mDNS-only speaker the mesh view cannot see', async () => {
    const idle = (await calibrationService.getEndpoints()).find((e) => e.name === 'Voice PE - 01')
    expect(idle).toMatchObject({
      playerId: 'ws://192.168.1.87:8927/sendspin', // provisional until adoption reports the real id
      foreign: true,
      idle: true,
      connected: false,
    })
  })

  it('does not list our own player twice via the neighbourhood', async () => {
    const endpoints = await calibrationService.getEndpoints()
    expect(endpoints.filter((e) => e.playerId === 'living')).toHaveLength(1)
    expect(endpoints.some((e) => e.playerId === 'ws://a:8928/sendspin')).toBe(false)
  })

  it('does not double-list an adopted speaker that mDNS also advertises', async () => {
    const endpoints = await calibrationService.getEndpoints()
    // Joined on URL: it must appear once, under its real handshake id, not again under its URL.
    expect(endpoints.filter((e) => e.url === 'ws://192.168.1.55:8927/sendspin')).toHaveLength(1)
    expect(endpoints.find((e) => e.url === 'ws://192.168.1.55:8927/sendspin')?.playerId).toBe('aa:bb:cc:dd:ee:ff')
  })

  it('marks an adopted speaker foreign but not idle', async () => {
    const esp = (await calibrationService.getEndpoints()).find((e) => e.playerId === 'aa:bb:cc:dd:ee:ff')
    expect(esp).toMatchObject({ foreign: true, idle: false })
  })

  it('marks a unit own player as neither foreign nor idle', async () => {
    const living = (await calibrationService.getEndpoints()).find((e) => e.playerId === 'living')
    expect(living).toMatchObject({ foreign: false, idle: false })
  })

  it('passes the listener URL when starting a tone for an idle speaker', async () => {
    const spy = vi.spyOn(globalThis, 'fetch')
    await calibrationService.toneStart('ws://192.168.1.87:8927/sendspin', 40, {
      url: 'ws://192.168.1.87:8927/sendspin',
    })
    const body = JSON.parse(String((spy.mock.calls[0][1] as RequestInit).body))
    expect(body.url).toBe('ws://192.168.1.87:8927/sendspin')
    spy.mockRestore()
  })

  it('omits the URL for a routable endpoint', async () => {
    const spy = vi.spyOn(globalThis, 'fetch')
    await calibrationService.toneStart('living', 40)
    const body = JSON.parse(String((spy.mock.calls[0][1] as RequestInit).body))
    expect(body.url).toBeUndefined()
    spy.mockRestore()
  })

  it('still lists endpoints when the neighbourhood is unavailable', async () => {
    server.use(http.get(`${MESH}/neighbourhood`, () => HttpResponse.json({ error: 'nope' }, { status: 503 })))
    expect((await calibrationService.getEndpoints()).length).toBeGreaterThan(0)
  })
})

describe('calibrationService — causal revision', () => {
  it('sends the high-water mark so the save outranks a peer record', async () => {
    const spy = vi.spyOn(globalThis, 'fetch')
    await calibrationService.save('kitchen', {
      name: 'Kitchen',
      url: null,
      enabled: true,
      samples: [{ volume: 35, db: 57 }, { volume: 85, db: 65 }],
      maxLimit: { mode: 'percentage', value: 100 },
      trimDb: 0,
      knownRev: 7,
    })
    const body = JSON.parse(String((spy.mock.calls[0][1] as RequestInit).body))
    expect(body.knownRev).toBe(7)
    spy.mockRestore()
  })
})
