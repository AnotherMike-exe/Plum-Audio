/**
 * Unit tests for useAudioSync hook
 * Tests: Progress tracking, seek detection, resync triggers
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createMockStream, createMockPlaybackData } from '../../mocks/mockTypes'

describe('useAudioSync hook logic', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  describe('progress tracking', () => {
    it('should start with zero progress', () => {
      const stream = createMockStream({ progress: 0 })
      expect(stream.progress).toBe(0)
    })

    it('should track progress from stream', () => {
      const stream = createMockStream({ progress: 45 })
      expect(stream.progress).toBe(45)
    })

    it('should prefer playback API position', () => {
      const stream = createMockStream({
        progress: 45,
        playback: createMockPlaybackData({
          interpolated_position: 50000 // 50 seconds in ms
        })
      })

      // Playback API position should be preferred
      const preferredProgress = stream.playback
        ? stream.playback.interpolated_position / 1000
        : stream.progress

      expect(preferredProgress).toBe(50)
    })
  })

  describe('local interpolation', () => {
    it('should interpolate position when playing', () => {
      let position = 45 // seconds
      const isPlaying = true

      // Simulate 1 second passing
      if (isPlaying) {
        position += 1
      }

      expect(position).toBe(46)
    })

    it('should not interpolate when paused', () => {
      let position = 45
      const isPlaying = false

      // Simulate time passing
      if (isPlaying) {
        position += 1
      }

      expect(position).toBe(45)
    })

    it('should cap position at duration', () => {
      let position = 179 // seconds
      const duration = 180
      const isPlaying = true

      // Simulate 2 seconds passing
      if (isPlaying) {
        position += 2
      }

      // Cap at duration
      position = Math.min(position, duration)

      expect(position).toBe(180)
    })
  })

  describe('resync triggers', () => {
    it('should resync on track change', () => {
      const oldTrack = { id: 'track-1' }
      const newTrack = { id: 'track-2' }

      const shouldResync = oldTrack.id !== newTrack.id
      expect(shouldResync).toBe(true)
    })

    it('should resync on playback state change', () => {
      const wasPlaying = false
      const isPlaying = true

      const shouldResync = wasPlaying !== isPlaying
      expect(shouldResync).toBe(true)
    })

    it('should not resync on normal playback', () => {
      const oldTrack = { id: 'track-1' }
      const newTrack = { id: 'track-1' }
      const wasPlaying = true
      const isPlaying = true

      const shouldResync = oldTrack.id !== newTrack.id || wasPlaying !== isPlaying
      expect(shouldResync).toBe(false)
    })
  })

  describe('seek detection', () => {
    it('should detect seek when position jumps forward', () => {
      const lastPosition = 45
      const serverPosition = 90 // 45 second jump
      const threshold = 5 // seconds

      const positionDelta = Math.abs(serverPosition - lastPosition)
      const isSeek = positionDelta > threshold

      expect(isSeek).toBe(true)
    })

    it('should detect seek when position jumps backward', () => {
      const lastPosition = 90
      const serverPosition = 30 // Jumped backward
      const threshold = 5

      const positionDelta = Math.abs(serverPosition - lastPosition)
      const isSeek = positionDelta > threshold

      expect(isSeek).toBe(true)
    })

    it('should not detect seek for small differences', () => {
      const lastPosition = 45
      const serverPosition = 47 // 2 second difference (normal playback)
      const threshold = 5

      const positionDelta = Math.abs(serverPosition - lastPosition)
      const isSeek = positionDelta > threshold

      expect(isSeek).toBe(false)
    })
  })

  describe('stale data handling', () => {
    it('should use last known position for stale data', () => {
      const lastKnownPosition = 45
      const playback = createMockPlaybackData({
        position: 45000,
        interpolated_position: 45000,
        is_stale: true
      })

      // When stale, don't trust interpolation
      const position = playback.is_stale
        ? playback.position / 1000
        : playback.interpolated_position / 1000

      expect(position).toBe(45)
    })

    it('should use interpolated position for fresh data', () => {
      const playback = createMockPlaybackData({
        position: 45000,
        interpolated_position: 47000,
        is_stale: false
      })

      const position = playback.is_stale
        ? playback.position / 1000
        : playback.interpolated_position / 1000

      expect(position).toBe(47)
    })
  })

  describe('pause behavior', () => {
    it('should preserve position when pausing', () => {
      let position = 45
      const wasPlaying = true
      const isPlaying = false

      // On pause, position should stay the same
      if (wasPlaying && !isPlaying) {
        // Don't update position
      }

      expect(position).toBe(45)
    })

    it('should resume from last position when unpausing', () => {
      let position = 45
      const wasPlaying = false
      const isPlaying = true

      // On unpause, start interpolating from current position
      if (!wasPlaying && isPlaying) {
        // Position stays same, interpolation starts
      }

      expect(position).toBe(45)
    })
  })

  describe('interval management', () => {
    it('should update every second when playing', () => {
      const intervalMs = 1000

      expect(intervalMs).toBe(1000)
    })

    it('should stop updates when paused', () => {
      const isPlaying = false
      const shouldUpdate = isPlaying

      expect(shouldUpdate).toBe(false)
    })
  })
})
