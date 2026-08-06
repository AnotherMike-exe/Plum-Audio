/**
 * Unit tests for settingsService — the REAL module.
 *
 * The previous version of this file imported nothing from the service. It built object literals
 * inline and asserted properties on them (`expect(expectedDefaults.theme.mode).toBe('system')`), so
 * every case was a tautology: the whole suite passed with the service deleted.
 *
 * What actually needs guarding is the two-tier split, which is the only non-obvious thing this
 * service does. Some settings live on the UNIT (deviceName, hostname, integrations, autoSwitch —
 * shared by every browser that opens the page) and some live in THIS BROWSER (theme, display). A
 * single `updateSettings` call has to route each key to the right tier: send a theme change to the
 * server and every other unit's GUI changes colour; keep a deviceName local and the rename is lost
 * on the next poll.
 */

import { describe, it, expect, vi, beforeEach, afterEach, beforeAll, afterAll } from 'vitest'
import { http, HttpResponse } from 'msw'
import { server, resetMockState } from '../../mocks/mockFetch'
import { settingsService } from '../../../services/settingsService'

const LOCAL_STORAGE_KEY = 'plum-snapcast-local-settings'

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => {
  server.resetHandlers()
  resetMockState()
  vi.restoreAllMocks()
})
afterAll(() => server.close())

beforeEach(() => {
  localStorage.clear()
})

describe('settingsService — the two-tier split', () => {
  it('sends unit-level settings to the server', async () => {
    const posted: unknown[] = []
    server.use(
      http.post('/api/settings', async ({ request }) => {
        const body = await request.json()
        posted.push(body)
        return HttpResponse.json({ ...(body as object), version: 2 })
      }),
    )

    await settingsService.updateSettings({ deviceName: 'Kitchen' })

    expect(posted).toHaveLength(1)
    expect(posted[0]).toMatchObject({ deviceName: 'Kitchen' })
  })

  it('keeps browser-level settings out of the server payload', async () => {
    // A theme is per-browser. POSTing it would repaint every other unit's GUI.
    const posted: unknown[] = []
    server.use(
      http.post('/api/settings', async ({ request }) => {
        posted.push(await request.json())
        return HttpResponse.json({ version: 2 })
      }),
    )

    await settingsService.updateSettings({ theme: { mode: 'dark', accent: 'purple' } as never })

    expect(posted).toHaveLength(0)  // nothing went to the unit
    expect(settingsService.getMergedSettings().theme?.mode).toBe('dark')
  })

  it('splits a mixed update across both tiers in one call', async () => {
    const posted: Record<string, unknown>[] = []
    server.use(
      http.post('/api/settings', async ({ request }) => {
        posted.push((await request.json()) as Record<string, unknown>)
        return HttpResponse.json({ version: 3 })
      }),
    )

    await settingsService.updateSettings({
      deviceName: 'Studio',
      theme: { mode: 'light', accent: 'blue' } as never,
    })

    expect(posted).toHaveLength(1)
    expect(posted[0]).toMatchObject({ deviceName: 'Studio' })
    expect(posted[0].theme).toBeUndefined()          // the theme did NOT travel
    expect(settingsService.getMergedSettings().theme?.mode).toBe('light')  // but it did apply
  })

  it('persists browser-level settings to localStorage', async () => {
    await settingsService.updateSettings({ theme: { mode: 'dark', accent: 'green' } as never })

    const stored = JSON.parse(localStorage.getItem(LOCAL_STORAGE_KEY) ?? '{}')
    expect(stored.theme).toMatchObject({ mode: 'dark', accent: 'green' })
  })

  it('lets a browser-level value win over the server value of the same name', () => {
    // That is what "merged" means, and it is why the local tier is applied second.
    settingsService.updateLocalSettings({ theme: { mode: 'dark' } as never })
    expect(settingsService.getMergedSettings().theme?.mode).toBe('dark')
  })
})

describe('settingsService — subscribers', () => {
  it('notifies on a local change and stops after unsubscribe', () => {
    const seen: string[] = []
    const unsubscribe = settingsService.subscribe(s => seen.push(s.theme?.mode ?? '?'))

    settingsService.updateLocalSettings({ theme: { mode: 'dark' } as never })
    expect(seen).toEqual(['dark'])

    unsubscribe()
    settingsService.updateLocalSettings({ theme: { mode: 'light' } as never })
    expect(seen).toEqual(['dark'])  // no second call
  })

  it('does not let one throwing subscriber starve the others', () => {
    // Both the theme hook and MeshApp subscribe; one failing must not freeze the other's UI.
    const good = vi.fn()
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const offBad = settingsService.subscribe(() => { throw new Error('boom') })
    const offGood = settingsService.subscribe(good)

    settingsService.updateLocalSettings({ theme: { mode: 'dark' } as never })

    expect(good).toHaveBeenCalled()
    offBad()
    offGood()
  })
})

describe('settingsService — reading from the unit', () => {
  it('survives a server that cannot be reached', async () => {
    // A unit whose config API is still starting must not leave the GUI with no settings at all.
    server.use(http.get('/api/settings', () => HttpResponse.error()))
    vi.spyOn(console, 'error').mockImplementation(() => {})

    const settings = await settingsService.init()
    expect(settings).toBeTruthy()
    expect(settings.theme).toBeTruthy()  // local tier still applies
  })
})
