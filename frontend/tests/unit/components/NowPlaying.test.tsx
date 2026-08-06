/**
 * Unit tests for NowPlaying — the REAL component.
 *
 * The previous version of this file defined a ~50-line `MockNowPlaying` inside the test and asserted
 * against that. The production component was never imported; it could have been deleted and the
 * suite would still have passed.
 *
 * Two behaviours here are load-bearing and neither is obvious from the markup:
 *
 * The artwork must NEVER be handed a src that can fail. An empty albumArtUrl (metadata arrives
 * seconds before artwork on Bluetooth and after every hard refresh) or a URL that 404s both draw the
 * browser's broken-image glyph next to the alt text — which is what a Bluetooth reconnect looked
 * like. The placeholder is the fix, and the reset-on-new-track is what stops one bad URL poisoning
 * every later track.
 *
 * Seeking is opt-in. NowPlaying is rendered with canSeek=false on the main card, because AirPlay and
 * Bluetooth cannot seek; a progress bar that silently swallows clicks is better than one that
 * pretends.
 */

import React from 'react'
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { NowPlaying } from '../../../components/NowPlaying'
import { ALBUM_ART_PLACEHOLDER } from '../../../services/albumArtPlaceholder'
import type { Stream } from '../../../types'

const stream = (over: Partial<Stream['currentTrack']> = {}, progress = 30): Stream =>
  ({
    id: 'unit-a::airplay-1',
    currentTrack: {
      title: 'Weightless',
      artist: 'Marconi Union',
      album: 'Distance',
      albumArtUrl: 'http://unit/art.jpg',
      duration: 480,
      ...over,
    },
    progress,
  } as unknown as Stream)

const art = () => screen.getByRole('img') as HTMLImageElement

describe('NowPlaying — track information', () => {
  it('renders the title, artist and album from the stream', () => {
    render(<NowPlaying stream={stream()} />)

    expect(screen.getByText('Weightless')).toBeTruthy()
    expect(screen.getByText('Marconi Union')).toBeTruthy()
    expect(screen.getByText('Distance')).toBeTruthy()
  })

  it('formats elapsed and total time', () => {
    render(<NowPlaying stream={stream({}, 95)} />)

    // Zero-padded minutes (utils/time.ts) — "01:35", not "1:35".
    expect(screen.getByText('01:35')).toBeTruthy()
    expect(screen.getByText('08:00')).toBeTruthy()
  })
})

describe('NowPlaying — artwork must never show a broken image', () => {
  it('uses the artwork when there is some', () => {
    render(<NowPlaying stream={stream()} />)
    expect(art().src).toBe('http://unit/art.jpg')
  })

  it('falls back to the placeholder when the source published none', () => {
    // Metadata lands seconds before artwork on Bluetooth, and on every hard refresh.
    render(<NowPlaying stream={stream({ albumArtUrl: '' })} />)
    expect(art().src).toBe(ALBUM_ART_PLACEHOLDER)
  })

  it('falls back to the placeholder when the artwork URL fails to load', () => {
    render(<NowPlaying stream={stream()} />)
    fireEvent.error(art())
    expect(art().src).toBe(ALBUM_ART_PLACEHOLDER)
  })

  it('gives a later track a fresh attempt after a failure', () => {
    // Without the reset, one 404 poisons the artwork for the rest of the session.
    const { rerender } = render(<NowPlaying stream={stream()} />)
    fireEvent.error(art())
    expect(art().src).toBe(ALBUM_ART_PLACEHOLDER)

    rerender(<NowPlaying stream={stream({ albumArtUrl: 'http://unit/next.jpg' })} />)
    expect(art().src).toBe('http://unit/next.jpg')
  })

  it('describes the artwork for screen readers even when it is the placeholder', () => {
    render(<NowPlaying stream={stream({ albumArtUrl: '' })} />)
    expect(art().alt).toBe('Album art for Distance')
  })
})

describe('NowPlaying — seeking is opt-in', () => {
  const clickMiddle = () => {
    const bar = document.querySelector('[title="Click to seek"]')
      ?? document.querySelector('.rounded-full.h-2')!
    vi.spyOn(bar, 'getBoundingClientRect').mockReturnValue({
      left: 0, width: 200, top: 0, right: 200, bottom: 0, height: 8, x: 0, y: 0, toJSON: () => ({}),
    } as DOMRect)
    fireEvent.click(bar, { clientX: 100 })
  }

  it('seeks to the clicked position when seeking is enabled', () => {
    const onSeek = vi.fn()
    render(<NowPlaying stream={stream()} canSeek onSeek={onSeek} />)

    clickMiddle()
    expect(onSeek).toHaveBeenCalledWith(240)  // halfway through 480s
  })

  it('ignores clicks when seeking is disabled', () => {
    // The main card renders canSeek=false: AirPlay and Bluetooth cannot seek, and a bar that
    // pretended to would be worse than one that does nothing.
    const onSeek = vi.fn()
    render(<NowPlaying stream={stream()} onSeek={onSeek} />)

    clickMiddle()
    expect(onSeek).not.toHaveBeenCalled()
  })

  it('ignores clicks on a track of unknown length', () => {
    // A live stream has duration 0; seeking within it is meaningless, and the maths would divide it.
    const onSeek = vi.fn()
    render(<NowPlaying stream={stream({ duration: 0 })} canSeek onSeek={onSeek} />)

    clickMiddle()
    expect(onSeek).not.toHaveBeenCalled()
  })
})

describe('NowPlaying — the album art click target', () => {
  it('opens the visualizer when a handler is given', () => {
    const onAlbumArtClick = vi.fn()
    render(<NowPlaying stream={stream()} onAlbumArtClick={onAlbumArtClick} />)

    fireEvent.click(art())
    expect(onAlbumArtClick).toHaveBeenCalled()
    expect(art().title).toBe('Click to open visualizer')
  })

  it('is not advertised as clickable without one', () => {
    render(<NowPlaying stream={stream()} />)
    expect(art().title).toBe('')
  })
})
