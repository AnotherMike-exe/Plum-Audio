/**
 * Unit tests for integrationsService — the REAL module.
 *
 * The previous version imported none of it. It built endpoint literals inline, asserted properties
 * on those literals, and had a whole `describe('DLNA integration')` block exercising
 * `/api/integrations/dlna/*` — a route NO blueprint registers. It passed while testing an API that
 * does not exist.
 *
 * `create_integrations_blueprint` registers airplay, spotify and bluetooth only. `dlnaService` and
 * `plexampService` are dead scaffolding in this file; every one of their calls 404s against a real
 * unit. That is asserted below rather than mocked into working, so the next person reads the truth.
 *
 * What is worth guarding is the error contract: these methods are called straight from
 * IntegrationsTab's click handlers, and a rejected promise is what surfaces the failure to the user.
 * A method that swallowed a non-2xx and resolved would leave the tab showing success for an endpoint
 * that was never created.
 */

import { describe, it, expect, beforeAll, afterEach, afterAll } from 'vitest'
import { http, HttpResponse } from 'msw'
import { server, resetMockState } from '../../mocks/mockFetch'
import { airplayService, bluetoothService, spotifyService, dlnaService } from '../../../services/integrationsService'

// The service builds absolute URLs from window.location, so handlers must match that origin.
const base = `${window.location.protocol}//${window.location.hostname}:${window.location.port}/api/integrations`

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => {
  server.resetHandlers()
  resetMockState()
})
afterAll(() => server.close())

describe('integrationsService — endpoint CRUD', () => {
  it('creates an AirPlay endpoint with the name the user typed', async () => {
    let sent: Record<string, unknown> | null = null
    server.use(
      http.post(`${base}/airplay/endpoints`, async ({ request }) => {
        sent = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({ success: true, message: 'created', endpoint: { id: '2' } })
      }),
    )

    const result = await airplayService.addEndpoint('Kitchen')

    expect(sent).toMatchObject({ deviceName: 'Kitchen', enabled: true })
    expect(result.success).toBe(true)
  })

  it('sends only the fields being changed on an update', async () => {
    let sent: Record<string, unknown> | null = null
    server.use(
      http.put(`${base}/spotify/endpoints/3`, async ({ request }) => {
        sent = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({ success: true, message: 'updated' })
      }),
    )

    await spotifyService.updateEndpoint('3', undefined, false)

    expect(sent).toMatchObject({ enabled: false })
    expect(sent!.deviceName).toBeUndefined()  // an unchanged name must not be re-sent
  })

  it('deletes by id', async () => {
    let hit = false
    server.use(
      http.delete(`${base}/bluetooth/endpoints/7`, () => {
        hit = true
        return HttpResponse.json({ success: true, message: 'removed' })
      }),
    )

    await bluetoothService.removeEndpoint('7')
    expect(hit).toBe(true)
  })

  it('lists endpoints', async () => {
    server.use(
      http.get(`${base}/airplay/endpoints`, () =>
        HttpResponse.json({ success: true, endpoints: [{ id: '1', enabled: true, deviceName: 'Lounge', port: 5050, udpPortBase: 6001 }] }),
      ),
    )

    const result = await airplayService.listEndpoints()
    expect(result.endpoints.map(e => e.deviceName)).toEqual(['Lounge'])
  })
})

describe('integrationsService — failures must reach the user', () => {
  it('rejects rather than resolving when the unit refuses a name', async () => {
    // A device name is interpolated into shairport's libconfig and a daemon is respooled, so the
    // backend rejects anything outside its allowlist. Swallowing that would show success in the tab
    // for a rename that never happened.
    server.use(
      http.post(`${base}/airplay/device-name`, () =>
        HttpResponse.json({ message: 'Invalid device name' }, { status: 400 }),
      ),
    )

    await expect(airplayService.updateDeviceName('evil";}')).rejects.toThrow(/Invalid device name/)
  })

  it('surfaces the server message rather than a bare status code', async () => {
    server.use(
      http.post(`${base}/spotify/enable`, () =>
        HttpResponse.json({ message: 'go-librespot is not installed' }, { status: 500 }),
      ),
    )

    await expect(spotifyService.enable()).rejects.toThrow(/go-librespot is not installed/)
  })

  it('falls back to the status code when the body is JSON without a message', async () => {
    server.use(http.post(`${base}/airplay/disable`, () => HttpResponse.json({}, { status: 503 })))

    await expect(airplayService.disable()).rejects.toThrow(/503/)
  })

  it('uses a generic message when the body is not JSON at all', async () => {
    // Documented, not endorsed: an empty or HTML body (nginx returning a 502 page) hits the
    // .catch() default, so the STATUS CODE is lost and the user sees only "Failed to disable
    // AirPlay". Worth knowing when a report says that and the logs say something else entirely.
    server.use(http.post(`${base}/airplay/disable`, () => new HttpResponse(null, { status: 503 })))

    await expect(airplayService.disable()).rejects.toThrow('Failed to disable AirPlay')
  })

  it('rejects on a network failure', async () => {
    server.use(http.get(`${base}/bluetooth/status`, () => HttpResponse.error()))

    await expect(bluetoothService.getStatus()).rejects.toThrow()
  })
})

describe('integrationsService — Bluetooth pairing', () => {
  it('lists bonded devices with their connection state', async () => {
    server.use(
      http.get(`${base}/bluetooth/devices`, () =>
        HttpResponse.json({
          success: true,
          devices: [{ address: 'AA:BB:CC:DD:EE:FF', name: 'iPhone', connected: true, trusted: true }],
        }),
      ),
    )

    const result = await bluetoothService.listPairedDevices()
    expect(result.devices[0]).toMatchObject({ name: 'iPhone', connected: true })
  })

  it('forgets a bond by address', async () => {
    // The stale-link-key case: "pairing unsuccessful" is usually OUR key being stale, and the fix is
    // removing the bond on this side — the phone forgetting it cannot help.
    let hit = ''
    server.use(
      http.delete(`${base}/bluetooth/devices/:address`, ({ params }) => {
        hit = params.address as string
        return HttpResponse.json({ success: true, message: 'removed' })
      }),
    )

    await bluetoothService.forgetPairedDevice('AA:BB:CC:DD:EE:FF')
    expect(hit).toBe('AA:BB:CC:DD:EE:FF')
  })
})

describe('integrationsService — DLNA has no backend', () => {
  it('404s against a real unit, because no blueprint registers the route', async () => {
    // Asserted rather than mocked into working. create_integrations_blueprint registers airplay,
    // spotify and bluetooth only; dlnaService and plexampService are dead scaffolding in this file.
    // The old version of this suite mocked these routes and tested them as though they worked.
    server.use(
      http.get(`${base}/dlna/status`, () => HttpResponse.json({ error: 'Not Found' }, { status: 404 })),
    )

    await expect(dlnaService.getStatus()).rejects.toThrow()
  })
})
