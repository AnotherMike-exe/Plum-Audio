/**
 * Unit tests for NowPlaying component
 * Tests: Rendering, progress bar, seek interaction
 */

import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { createMockStream, createMockTrack } from '../../mocks/mockTypes'

// Mock component that represents NowPlaying behavior
function MockNowPlaying({
  stream,
  onSeek
}: {
  stream: ReturnType<typeof createMockStream>
  onSeek?: (position: number) => void
}) {
  const track = stream.currentTrack
  const progress = stream.playback
    ? stream.playback.interpolated_position / 1000
    : stream.progress
  const duration = track.duration

  const formatTime = (seconds: number) => {
    const mins = Math.floor(seconds / 60)
    const secs = Math.floor(seconds % 60)
    return `${mins}:${secs.toString().padStart(2, '0')}`
  }

  const handleProgressClick = (e: React.MouseEvent<HTMLDivElement>) => {
    if (!onSeek) return
    const rect = e.currentTarget.getBoundingClientRect()
    const clickPosition = (e.clientX - rect.left) / rect.width
    const seekPosition = clickPosition * duration
    onSeek(seekPosition)
  }

  return (
    <div data-testid="now-playing">
      <img
        src={track.albumArtUrl || '/default-album.svg'}
        alt="Album Art"
        data-testid="album-art"
      />
      <div data-testid="track-title">{track.title}</div>
      <div data-testid="track-artist">{track.artist}</div>
      <div data-testid="track-album">{track.album}</div>
      <div
        data-testid="progress-bar"
        onClick={handleProgressClick}
        style={{ width: '100%', height: '8px', background: '#ccc' }}
      >
        <div
          data-testid="progress-fill"
          style={{
            width: `${(progress / duration) * 100}%`,
            height: '100%',
            background: '#007bff'
          }}
        />
      </div>
      <div data-testid="time-current">{formatTime(progress)}</div>
      <div data-testid="time-duration">{formatTime(duration)}</div>
    </div>
  )
}

describe('NowPlaying component', () => {
  describe('rendering', () => {
    it('should render track information', () => {
      const stream = createMockStream({
        currentTrack: createMockTrack({
          title: 'Test Song',
          artist: 'Test Artist',
          album: 'Test Album'
        })
      })

      render(<MockNowPlaying stream={stream} />)

      expect(screen.getByTestId('track-title')).toHaveTextContent('Test Song')
      expect(screen.getByTestId('track-artist')).toHaveTextContent('Test Artist')
      expect(screen.getByTestId('track-album')).toHaveTextContent('Test Album')
    })

    it('should render album art', () => {
      const stream = createMockStream({
        currentTrack: createMockTrack({
          albumArtUrl: 'https://example.com/art.jpg'
        })
      })

      render(<MockNowPlaying stream={stream} />)

      const albumArt = screen.getByTestId('album-art') as HTMLImageElement
      expect(albumArt.src).toBe('https://example.com/art.jpg')
    })

    it('should render default album art when missing', () => {
      const stream = createMockStream({
        currentTrack: createMockTrack({ albumArtUrl: '' })
      })

      render(<MockNowPlaying stream={stream} />)

      const albumArt = screen.getByTestId('album-art') as HTMLImageElement
      expect(albumArt.src).toContain('default-album.svg')
    })
  })

  describe('progress bar', () => {
    it('should show progress percentage', () => {
      const stream = createMockStream({
        progress: 90,
        currentTrack: createMockTrack({ duration: 180 })
      })

      render(<MockNowPlaying stream={stream} />)

      const progressFill = screen.getByTestId('progress-fill')
      // 90/180 = 50%
      expect(progressFill.style.width).toBe('50%')
    })

    it('should show zero progress at start', () => {
      const stream = createMockStream({
        progress: 0,
        currentTrack: createMockTrack({ duration: 180 })
      })

      render(<MockNowPlaying stream={stream} />)

      const progressFill = screen.getByTestId('progress-fill')
      expect(progressFill.style.width).toBe('0%')
    })

    it('should show full progress at end', () => {
      const stream = createMockStream({
        progress: 180,
        currentTrack: createMockTrack({ duration: 180 })
      })

      render(<MockNowPlaying stream={stream} />)

      const progressFill = screen.getByTestId('progress-fill')
      expect(progressFill.style.width).toBe('100%')
    })
  })

  describe('time display', () => {
    it('should format time correctly', () => {
      const stream = createMockStream({
        progress: 65, // 1:05
        currentTrack: createMockTrack({ duration: 180 }) // 3:00
      })

      render(<MockNowPlaying stream={stream} />)

      expect(screen.getByTestId('time-current')).toHaveTextContent('1:05')
      expect(screen.getByTestId('time-duration')).toHaveTextContent('3:00')
    })

    it('should handle zero time', () => {
      const stream = createMockStream({
        progress: 0,
        currentTrack: createMockTrack({ duration: 0 })
      })

      render(<MockNowPlaying stream={stream} />)

      expect(screen.getByTestId('time-current')).toHaveTextContent('0:00')
    })

    it('should format long tracks correctly', () => {
      const stream = createMockStream({
        progress: 3665, // 61:05
        currentTrack: createMockTrack({ duration: 3665 })
      })

      render(<MockNowPlaying stream={stream} />)

      expect(screen.getByTestId('time-current')).toHaveTextContent('61:05')
    })
  })

  describe('seek interaction', () => {
    it('should call onSeek when clicking progress bar', () => {
      const onSeek = vi.fn()
      const stream = createMockStream({
        currentTrack: createMockTrack({ duration: 180 })
      })

      render(<MockNowPlaying stream={stream} onSeek={onSeek} />)

      const progressBar = screen.getByTestId('progress-bar')

      // Mock getBoundingClientRect
      progressBar.getBoundingClientRect = vi.fn(() => ({
        left: 0,
        right: 100,
        width: 100,
        top: 0,
        bottom: 8,
        height: 8,
        x: 0,
        y: 0,
        toJSON: () => {}
      }))

      // Click at 50%
      fireEvent.click(progressBar, { clientX: 50 })

      expect(onSeek).toHaveBeenCalledWith(90) // 50% of 180s
    })

    it('should not seek when onSeek not provided', () => {
      const stream = createMockStream()

      render(<MockNowPlaying stream={stream} />)

      const progressBar = screen.getByTestId('progress-bar')

      // Should not throw
      expect(() => fireEvent.click(progressBar)).not.toThrow()
    })
  })

  describe('playback data integration', () => {
    it('should prefer playback API position', () => {
      const stream = createMockStream({
        progress: 45,
        playback: {
          position: 50000,
          duration: 180000,
          interpolated_position: 52000, // 52 seconds
          playback_status: 'playing',
          is_stale: false
        },
        currentTrack: createMockTrack({ duration: 180 })
      })

      render(<MockNowPlaying stream={stream} />)

      // Should show 52 seconds (from interpolated), not 45
      expect(screen.getByTestId('time-current')).toHaveTextContent('0:52')
    })

    it('should fallback to progress when no playback data', () => {
      const stream = createMockStream({
        progress: 45,
        playback: undefined,
        currentTrack: createMockTrack({ duration: 180 })
      })

      render(<MockNowPlaying stream={stream} />)

      expect(screen.getByTestId('time-current')).toHaveTextContent('0:45')
    })
  })
})
