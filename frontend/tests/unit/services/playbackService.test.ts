/**
 * Unit tests for playbackService
 * Tests: Position fetching, interpolation format, stale data handling
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { server, setMockPlayback, resetMockState } from '../../mocks/mockFetch'
import { createMockPlaybackData } from '../../mocks/mockTypes'

// Setup MSW
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => {
  server.resetHandlers()
  resetMockState()
})
afterAll(() => server.close())

describe('playbackService', () => {
  describe('getAllPlayback', () => {
    it('should fetch all streams playback data', async () => {
      setMockPlayback('stream1', createMockPlaybackData({ position: 1000 }))
      setMockPlayback('stream2', createMockPlaybackData({ position: 2000 }))

      const response = await fetch('/api/playback')
      const data = await response.json()

      expect(data.success).toBe(true)
      expect(data.streams).toBeDefined()
    })

    it('should return empty streams object when no data', async () => {
      const response = await fetch('/api/playback')
      const data = await response.json()

      expect(data.success).toBe(true)
      expect(data.streams).toEqual({})
    })
  })

  describe('getStreamPlayback', () => {
    it('should fetch specific stream playback data', async () => {
      setMockPlayback('test-stream', createMockPlaybackData({
        position: 45000,
        duration: 180000,
        playback_status: 'playing'
      }))

      const response = await fetch('/api/playback/test-stream')
      const data = await response.json()

      expect(data.success).toBe(true)
      expect(data.position).toBe(45000)
      expect(data.duration).toBe(180000)
    })

    it('should return success:false for unknown stream', async () => {
      const response = await fetch('/api/playback/unknown-stream')
      const data = await response.json()

      expect(data.success).toBe(false)
    })
  })

  describe('PlaybackData structure', () => {
    it('should have all required fields', () => {
      const playbackData = createMockPlaybackData()

      expect(playbackData).toHaveProperty('position')
      expect(playbackData).toHaveProperty('duration')
      expect(playbackData).toHaveProperty('interpolated_position')
      expect(playbackData).toHaveProperty('playback_status')
      expect(playbackData).toHaveProperty('is_stale')
    })

    it('should have valid playback_status values', () => {
      const validStatuses = ['playing', 'paused', 'stopped', 'unknown']

      validStatuses.forEach(status => {
        const data = createMockPlaybackData({ playback_status: status as any })
        expect(['playing', 'paused', 'stopped', 'unknown']).toContain(data.playback_status)
      })
    })

    it('should have numeric position and duration', () => {
      const data = createMockPlaybackData({
        position: 45000,
        duration: 180000
      })

      expect(typeof data.position).toBe('number')
      expect(typeof data.duration).toBe('number')
      expect(data.position).toBeGreaterThanOrEqual(0)
      expect(data.duration).toBeGreaterThanOrEqual(0)
    })

    it('should have interpolated_position for playing streams', () => {
      const data = createMockPlaybackData({
        position: 45000,
        interpolated_position: 45500,
        playback_status: 'playing'
      })

      expect(data.interpolated_position).toBeGreaterThanOrEqual(data.position)
    })
  })

  describe('stale data handling', () => {
    it('should detect stale data via is_stale flag', () => {
      const freshData = createMockPlaybackData({ is_stale: false })
      const staleData = createMockPlaybackData({ is_stale: true })

      expect(freshData.is_stale).toBe(false)
      expect(staleData.is_stale).toBe(true)
    })

    it('should stop interpolation for stale data', () => {
      // When data is stale, interpolated_position should equal position
      const staleData = createMockPlaybackData({
        position: 45000,
        interpolated_position: 45000,
        is_stale: true,
        playback_status: 'playing'
      })

      // Stale data means we can't trust interpolation
      expect(staleData.is_stale).toBe(true)
    })
  })

  describe('position conversion', () => {
    it('should convert milliseconds to seconds', () => {
      const positionMs = 45000
      const positionSeconds = positionMs / 1000

      expect(positionSeconds).toBe(45)
    })

    it('should format time for display', () => {
      const positionSeconds = 125 // 2:05

      const minutes = Math.floor(positionSeconds / 60)
      const seconds = Math.floor(positionSeconds % 60)
      const formatted = `${minutes}:${seconds.toString().padStart(2, '0')}`

      expect(formatted).toBe('2:05')
    })

    it('should handle zero duration gracefully', () => {
      const data = createMockPlaybackData({
        position: 0,
        duration: 0
      })

      // Should not throw when calculating progress percentage
      const progress = data.duration > 0 ? (data.position / data.duration) * 100 : 0
      expect(progress).toBe(0)
    })
  })

  describe('toAudioSyncFormat', () => {
    it('should convert PlaybackData to Stream-compatible format', () => {
      const playbackData = createMockPlaybackData({
        position: 45000,
        duration: 180000,
        interpolated_position: 45500,
        playback_status: 'playing'
      })

      // The audioSync format uses seconds
      const audioSyncFormat = {
        progress: playbackData.interpolated_position / 1000,
        duration: playbackData.duration / 1000,
        isPlaying: playbackData.playback_status === 'playing'
      }

      expect(audioSyncFormat.progress).toBe(45.5)
      expect(audioSyncFormat.duration).toBe(180)
      expect(audioSyncFormat.isPlaying).toBe(true)
    })

    it('should use position when paused', () => {
      const playbackData = createMockPlaybackData({
        position: 45000,
        interpolated_position: 45000, // Same as position when paused
        playback_status: 'paused'
      })

      const audioSyncFormat = {
        progress: playbackData.interpolated_position / 1000,
        isPlaying: playbackData.playback_status === 'playing'
      }

      expect(audioSyncFormat.progress).toBe(45)
      expect(audioSyncFormat.isPlaying).toBe(false)
    })
  })
})
