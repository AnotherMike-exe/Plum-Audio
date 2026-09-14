/**
 * Unit tests for updateService.
 *
 * These exercise `deriveState`, which is the whole of the tab's correctness: every badge, colour
 * and warning is computed from it, so a wrong answer here is a wrong answer everywhere.
 *
 * Four cases carry real weight, because each one is a lie the operator would act on:
 *   - an OLD IMAGE must not read as unreachable. Measured on .7.200/.203/.204: they answered 405
 *     for the update route while serving audio and their GUI perfectly. "Unreachable" sends the
 *     reader to check the network when the fix is a deploy.
 *   - an unreachable registry must read `unknown`, never `up-to-date`. Units sit on isolated AV
 *     VLANs, so "we could not ask" is the normal state, and rendering it as current would hide a
 *     unit stuck on an old image.
 *   - a unit MID-RESTART must not read as failed. Applying an update kills the container answering
 *     the request, so ~10 s of network failures is the mechanism working.
 *   - a STALE pending request must read as failed. It is the only signal that the host agent is
 *     installed but not running, and without it a dead agent looks like a slow pull forever.
 */

import { describe, it, expect } from 'vitest'
import {
  deriveState,
  shortDigest,
  type StatusProbe,
  type UnitUpdateStatus,
} from '../../../services/updateService'

const base = (over: Partial<UnitUpdateStatus> = {}): UnitUpdateStatus => ({
  running: { version: '1.0.0', buildType: 'dev', gitDescribe: null },
  agent: { installed: true, version: '1.0.0', lastSeen: '2026-09-13T00:00:00Z' },
  channels: ['dev', 'latest'],
  pending: false,
  requestedAt: null,
  stale: false,
  digest: 'sha256:aaa',
  available: 'sha256:aaa',
  phase: 'idle',
  ...over,
})

/** A successful probe wrapping a status, which is what deriveState takes. */
const ok = (over: Partial<UnitUpdateStatus> = {}): StatusProbe => ({ kind: 'ok', status: base(over) })

const UNREACHABLE: StatusProbe = { kind: 'unreachable' }
const NO_ENDPOINT: StatusProbe = { kind: 'no-endpoint' }

describe('deriveState — what a row shows', () => {
  it('reports a unit we could not reach at all', () => {
    expect(deriveState(UNREACHABLE)).toBe('unreachable')
  })

  it('tells an OLD IMAGE apart from an unreachable unit', () => {
    // The regression this pins: .7.200/.203/.204 all answered 405 for the update route with a
    // healthy /api/mesh/snapshot. Reading that as "Unreachable" sent the operator to the network
    // when the fix was a deploy.
    expect(deriveState(NO_ENDPOINT)).toBe('needs-image')
  })

  it('reports a host with no agent, which is every pre-agent unit', () => {
    expect(deriveState(ok({ agent: { installed: false, version: null, lastSeen: null } }))).toBe(
      'no-agent',
    )
  })

  it('says up to date only when both digests are known AND equal', () => {
    expect(deriveState(ok())).toBe('up-to-date')
  })

  it('says an update is available when the digests differ', () => {
    expect(deriveState(ok({ available: 'sha256:bbb' }))).toBe('update-available')
  })

  it('says UNKNOWN when the registry could not be reached, never up to date', () => {
    // The failure this prevents: an isolated AV VLAN cannot reach ghcr.io, and a unit stuck three
    // releases back would otherwise render green.
    expect(deriveState(ok({ available: null }))).toBe('unknown')
  })

  it('says UNKNOWN when the unit runs a tarball image with no registry digest', () => {
    // deploy.sh's default path loads an image from a tarball. It has no RepoDigest at all, so there
    // is nothing to compare and "up to date" would be invented.
    expect(deriveState(ok({ digest: null }))).toBe('unknown')
  })

  it('reports the agent phases while work is in flight', () => {
    expect(deriveState(ok({ phase: 'pulling' }))).toBe('pulling')
    expect(deriveState(ok({ phase: 'restarting' }))).toBe('restarting')
    expect(deriveState(ok({ phase: 'failed' }))).toBe('failed')
  })

  it('treats a fresh pending request as work in progress', () => {
    expect(deriveState(ok({ pending: true, stale: false, requestedAt: Date.now() / 1000 }))).toBe(
      'pulling',
    )
  })

  it('treats a STALE pending request as a failure, not as a slow pull', () => {
    // The agent consumes a request within seconds. Still pending later means it is not running, and
    // the operator needs to see that rather than wait forever.
    expect(deriveState(ok({ pending: true, stale: true, requestedAt: 0 }))).toBe('failed')
  })

  it('puts the phase ahead of the digest comparison', () => {
    // Mid-pull the digests still match, because nothing has been installed yet. Reading that as
    // "up to date" would clear the row the instant the work started.
    expect(deriveState(ok({ phase: 'pulling', digest: 'sha256:aaa', available: 'sha256:aaa' }))).toBe(
      'pulling',
    )
  })

  it('puts a missing agent ahead of everything else', () => {
    expect(
      deriveState(
        ok({
          agent: { installed: false, version: null, lastSeen: null },
          phase: 'pulling',
          available: 'sha256:bbb',
        }),
      ),
    ).toBe('no-agent')
  })
})

describe('shortDigest', () => {
  it('trims a sha256 digest to something a table cell can hold', () => {
    expect(shortDigest('sha256:b92af6532dc27ef7a98dd48cf07a099bf')).toBe('b92af6532dc2')
  })

  it('renders an absent digest as a dash rather than as empty', () => {
    expect(shortDigest(null)).toBe('—')
    expect(shortDigest(undefined)).toBe('—')
  })

  it('tolerates a digest with no algorithm prefix', () => {
    expect(shortDigest('abcdef0123456789')).toBe('abcdef012345')
  })
})
