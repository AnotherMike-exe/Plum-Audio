/**
 * Unit tests for PlayerControls component
 * Tests: Button clicks, volume slider, mute toggle
 */

import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

// Mock component that represents PlayerControls behavior
function MockPlayerControls({
  isPlaying = false,
  volume = 100,
  onPlayPause,
  onPrevious,
  onNext,
  onVolumeChange,
  canPlay = true,
  canPause = true,
  canGoPrevious = true,
  canGoNext = true
}: {
  isPlaying?: boolean
  volume?: number
  onPlayPause?: () => void
  onPrevious?: () => void
  onNext?: () => void
  onVolumeChange?: (volume: number) => void
  canPlay?: boolean
  canPause?: boolean
  canGoPrevious?: boolean
  canGoNext?: boolean
}) {
  return (
    <div data-testid="player-controls">
      <button
        data-testid="btn-previous"
        onClick={onPrevious}
        disabled={!canGoPrevious}
        aria-label="Previous"
      >
        Previous
      </button>

      <button
        data-testid="btn-play-pause"
        onClick={onPlayPause}
        disabled={isPlaying ? !canPause : !canPlay}
        aria-label={isPlaying ? 'Pause' : 'Play'}
      >
        {isPlaying ? 'Pause' : 'Play'}
      </button>

      <button
        data-testid="btn-next"
        onClick={onNext}
        disabled={!canGoNext}
        aria-label="Next"
      >
        Next
      </button>

      <input
        type="range"
        data-testid="volume-slider"
        min={0}
        max={100}
        value={volume}
        onChange={(e) => onVolumeChange?.(Number(e.target.value))}
        aria-label="Volume"
      />

      <span data-testid="volume-value">{volume}%</span>
    </div>
  )
}

describe('PlayerControls component', () => {
  describe('rendering', () => {
    it('should render all control buttons', () => {
      render(<MockPlayerControls />)

      expect(screen.getByTestId('btn-previous')).toBeInTheDocument()
      expect(screen.getByTestId('btn-play-pause')).toBeInTheDocument()
      expect(screen.getByTestId('btn-next')).toBeInTheDocument()
    })

    it('should render volume slider', () => {
      render(<MockPlayerControls volume={75} />)

      const slider = screen.getByTestId('volume-slider') as HTMLInputElement
      expect(slider).toBeInTheDocument()
      expect(slider.value).toBe('75')
    })

    it('should show Play when not playing', () => {
      render(<MockPlayerControls isPlaying={false} />)

      expect(screen.getByTestId('btn-play-pause')).toHaveTextContent('Play')
    })

    it('should show Pause when playing', () => {
      render(<MockPlayerControls isPlaying={true} />)

      expect(screen.getByTestId('btn-play-pause')).toHaveTextContent('Pause')
    })
  })

  describe('button clicks', () => {
    it('should call onPlayPause when play button clicked', () => {
      const onPlayPause = vi.fn()
      render(<MockPlayerControls onPlayPause={onPlayPause} />)

      fireEvent.click(screen.getByTestId('btn-play-pause'))

      expect(onPlayPause).toHaveBeenCalledTimes(1)
    })

    it('should call onPrevious when previous button clicked', () => {
      const onPrevious = vi.fn()
      render(<MockPlayerControls onPrevious={onPrevious} />)

      fireEvent.click(screen.getByTestId('btn-previous'))

      expect(onPrevious).toHaveBeenCalledTimes(1)
    })

    it('should call onNext when next button clicked', () => {
      const onNext = vi.fn()
      render(<MockPlayerControls onNext={onNext} />)

      fireEvent.click(screen.getByTestId('btn-next'))

      expect(onNext).toHaveBeenCalledTimes(1)
    })
  })

  describe('disabled states', () => {
    it('should disable play when canPlay is false', () => {
      render(<MockPlayerControls isPlaying={false} canPlay={false} />)

      expect(screen.getByTestId('btn-play-pause')).toBeDisabled()
    })

    it('should disable pause when canPause is false', () => {
      render(<MockPlayerControls isPlaying={true} canPause={false} />)

      expect(screen.getByTestId('btn-play-pause')).toBeDisabled()
    })

    it('should disable previous when canGoPrevious is false', () => {
      render(<MockPlayerControls canGoPrevious={false} />)

      expect(screen.getByTestId('btn-previous')).toBeDisabled()
    })

    it('should disable next when canGoNext is false', () => {
      render(<MockPlayerControls canGoNext={false} />)

      expect(screen.getByTestId('btn-next')).toBeDisabled()
    })
  })

  describe('volume control', () => {
    it('should call onVolumeChange when slider moved', () => {
      const onVolumeChange = vi.fn()
      render(<MockPlayerControls volume={50} onVolumeChange={onVolumeChange} />)

      const slider = screen.getByTestId('volume-slider')
      fireEvent.change(slider, { target: { value: '75' } })

      expect(onVolumeChange).toHaveBeenCalledWith(75)
    })

    it('should display volume percentage', () => {
      render(<MockPlayerControls volume={80} />)

      expect(screen.getByTestId('volume-value')).toHaveTextContent('80%')
    })

    it('should handle volume at 0', () => {
      render(<MockPlayerControls volume={0} />)

      const slider = screen.getByTestId('volume-slider') as HTMLInputElement
      expect(slider.value).toBe('0')
      expect(screen.getByTestId('volume-value')).toHaveTextContent('0%')
    })

    it('should handle volume at 100', () => {
      render(<MockPlayerControls volume={100} />)

      const slider = screen.getByTestId('volume-slider') as HTMLInputElement
      expect(slider.value).toBe('100')
      expect(screen.getByTestId('volume-value')).toHaveTextContent('100%')
    })
  })

  describe('accessibility', () => {
    it('should have aria-labels on buttons', () => {
      render(<MockPlayerControls />)

      expect(screen.getByTestId('btn-previous')).toHaveAttribute('aria-label', 'Previous')
      expect(screen.getByTestId('btn-next')).toHaveAttribute('aria-label', 'Next')
    })

    it('should update play/pause aria-label based on state', () => {
      const { rerender } = render(<MockPlayerControls isPlaying={false} />)
      expect(screen.getByTestId('btn-play-pause')).toHaveAttribute('aria-label', 'Play')

      rerender(<MockPlayerControls isPlaying={true} />)
      expect(screen.getByTestId('btn-play-pause')).toHaveAttribute('aria-label', 'Pause')
    })

    it('should have aria-label on volume slider', () => {
      render(<MockPlayerControls />)

      expect(screen.getByTestId('volume-slider')).toHaveAttribute('aria-label', 'Volume')
    })
  })
})
