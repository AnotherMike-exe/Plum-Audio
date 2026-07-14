/**
 * Vitest Test Setup
 *
 * This file runs before each test file and sets up the testing environment.
 */

import { expect, afterEach, vi } from 'vitest'
import { cleanup } from '@testing-library/react'
import * as matchers from '@testing-library/jest-dom/matchers'

// Extend Vitest's expect with jest-dom matchers
expect.extend(matchers)

// Storage for localStorage mock (defined here for access in afterEach)
const localStorageStore: Record<string, string> = {}

// Cleanup after each test
afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  // Clear localStorage between tests
  Object.keys(localStorageStore).forEach(key => delete localStorageStore[key])
})

// Mock localStorage with actual storage behavior
const localStorageMock = {
  getItem: vi.fn((key: string) => localStorageStore[key] ?? null),
  setItem: vi.fn((key: string, value: string) => {
    localStorageStore[key] = value
  }),
  removeItem: vi.fn((key: string) => {
    delete localStorageStore[key]
  }),
  clear: vi.fn(() => {
    Object.keys(localStorageStore).forEach(key => delete localStorageStore[key])
  }),
  get length() { return Object.keys(localStorageStore).length },
  key: vi.fn((index: number) => Object.keys(localStorageStore)[index] ?? null)
}
Object.defineProperty(window, 'localStorage', { value: localStorageMock })

// Mock matchMedia (used by theme detection)
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn()
  }))
})

// Mock ResizeObserver (used by some components)
class ResizeObserverMock {
  observe = vi.fn()
  unobserve = vi.fn()
  disconnect = vi.fn()
}
Object.defineProperty(window, 'ResizeObserver', { value: ResizeObserverMock })

// Mock IntersectionObserver
class IntersectionObserverMock {
  observe = vi.fn()
  unobserve = vi.fn()
  disconnect = vi.fn()
  root = null
  rootMargin = ''
  thresholds = []
}
Object.defineProperty(window, 'IntersectionObserver', { value: IntersectionObserverMock })

// Mock AudioContext (used by visualizer and browser audio)
class AudioContextMock {
  createAnalyser = vi.fn(() => ({
    connect: vi.fn(),
    disconnect: vi.fn(),
    fftSize: 2048,
    frequencyBinCount: 1024,
    getByteFrequencyData: vi.fn(),
    getByteTimeDomainData: vi.fn()
  }))
  createMediaElementSource = vi.fn(() => ({
    connect: vi.fn(),
    disconnect: vi.fn()
  }))
  createMediaStreamSource = vi.fn(() => ({
    connect: vi.fn(),
    disconnect: vi.fn()
  }))
  createGain = vi.fn(() => ({
    connect: vi.fn(),
    disconnect: vi.fn(),
    gain: { value: 1 }
  }))
  destination = {}
  state = 'running'
  resume = vi.fn()
  suspend = vi.fn()
  close = vi.fn()
}
Object.defineProperty(window, 'AudioContext', { value: AudioContextMock })
Object.defineProperty(window, 'webkitAudioContext', { value: AudioContextMock })

// Mock WebSocket
class WebSocketMock {
  static CONNECTING = 0
  static OPEN = 1
  static CLOSING = 2
  static CLOSED = 3

  readyState = WebSocketMock.OPEN
  url = ''
  onopen: ((event: Event) => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onerror: ((event: Event) => void) | null = null

  constructor(url: string) {
    this.url = url
    // Simulate connection after a tick
    setTimeout(() => {
      this.onopen?.(new Event('open'))
    }, 0)
  }

  send = vi.fn()
  close = vi.fn(() => {
    this.readyState = WebSocketMock.CLOSED
    this.onclose?.(new CloseEvent('close'))
  })
}
Object.defineProperty(window, 'WebSocket', { value: WebSocketMock })

// Suppress console errors/warnings in tests (optional, comment out for debugging)
// vi.spyOn(console, 'error').mockImplementation(() => {})
// vi.spyOn(console, 'warn').mockImplementation(() => {})

// Export mocks for use in tests
export { localStorageMock, WebSocketMock, AudioContextMock }
