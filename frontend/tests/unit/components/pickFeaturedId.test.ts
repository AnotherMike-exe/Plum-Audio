/**
 * Unit tests for MeshApp's `pickFeaturedId`.
 *
 * This one function decides whether the left card shows anything at all, and everything downstream
 * is gated on its result: the transport controls, ClientManager's "Join Stream" button, the synced-
 * devices list and the group volume inside it. On an ingest-only unit it used to return null forever
 * — so a unit actively feeding four other rooms rendered "Nothing Playing" with every control dead,
 * and nothing anywhere logged a thing.
 *
 * Extracted and exported for the same reason as playerControlsPropsEqual: a wrong answer here is
 * invisible until someone notices the UI is inert.
 */

import { describe, it, expect } from 'vitest'
import { pickFeaturedId } from '../../../MeshApp'
import type { Stream } from '../../../types'

const stream = (id: string, active: boolean): Stream =>
  ({ id, active } as unknown as Stream)

describe('pickFeaturedId', () => {
  it('always honours an explicit pick', () => {
    expect(pickFeaturedId('chosen', stream('mine', true), false, [stream('ingested', true)])).toBe('chosen')
  })

  it('shows where this unit’s own speaker is playing', () => {
    expect(pickFeaturedId(null, stream('mine', true), false, [])).toBe('mine')
  })

  it('shows nothing when this unit’s speaker is idle', () => {
    // The source is routed but quiet: the holding state is correct, the player stays where it is.
    expect(pickFeaturedId(null, stream('mine', false), false, [])).toBeNull()
  })

  it('falls back to what a playerless unit is ingesting', () => {
    // The regression this exists for. No speaker means no "where my player is" — but the unit is
    // very much doing something, and the card has to show it.
    expect(pickFeaturedId(null, undefined, true, [stream('ingested', true)])).toBe('ingested')
  })

  it('shows nothing when a playerless unit is ingesting nothing', () => {
    expect(pickFeaturedId(null, undefined, true, [])).toBeNull()
  })

  it('does not use the ingest fallback on a unit that HAS a speaker', () => {
    // A normal unit with an idle player and an active local source must keep showing the holding
    // state — the left card means "where my speaker is", and quietly changing that would be wrong.
    expect(pickFeaturedId(null, undefined, false, [stream('ingested', true)])).toBeNull()
  })

  it('takes the first of several ingested sources, given a sorted list', () => {
    // MeshApp sorts by id before calling this, so the pick cannot reshuffle between polls.
    const sorted = [stream('a-airplay', true), stream('b-spotify', true)]
    expect(pickFeaturedId(null, undefined, true, sorted)).toBe('a-airplay')
  })
})
