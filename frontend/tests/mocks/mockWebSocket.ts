/**
 * Mock WebSocket for testing snapcastService
 *
 * Provides a controllable WebSocket mock that can simulate:
 * - Connection lifecycle (open, close, error)
 * - Message sending and receiving
 * - JSON-RPC 2.0 responses
 * - Server notifications
 */

import { vi } from 'vitest'

type MessageHandler = (event: MessageEvent) => void
type EventHandler = (event: Event) => void
type CloseHandler = (event: CloseEvent) => void
type ErrorHandler = (event: Event) => void

interface QueuedResponse {
  method: string
  result: unknown
}

export class MockWebSocket {
  static CONNECTING = 0
  static OPEN = 1
  static CLOSING = 2
  static CLOSED = 3

  // Instance state
  readyState: number = MockWebSocket.CONNECTING
  url: string
  protocol: string = ''
  bufferedAmount: number = 0
  extensions: string = ''
  binaryType: BinaryType = 'blob'

  // Event handlers
  onopen: EventHandler | null = null
  onclose: CloseHandler | null = null
  onmessage: MessageHandler | null = null
  onerror: ErrorHandler | null = null

  // Mock tracking
  messagesSent: string[] = []
  private _responses: Map<string, unknown> = new Map()
  private _queuedMessages: string[] = []
  private _autoConnect: boolean = true
  private _messageId: number = 0

  // Static mock controls
  private static _instances: MockWebSocket[] = []
  private static _shouldFailConnection: boolean = false
  private static _connectionDelay: number = 0

  constructor(url: string, protocols?: string | string[]) {
    this.url = url
    if (protocols) {
      this.protocol = Array.isArray(protocols) ? protocols[0] : protocols
    }

    MockWebSocket._instances.push(this)

    if (this._autoConnect) {
      this._simulateConnection()
    }
  }

  private _simulateConnection() {
    const delay = MockWebSocket._connectionDelay

    setTimeout(() => {
      if (MockWebSocket._shouldFailConnection) {
        this.readyState = MockWebSocket.CLOSED
        this.onerror?.(new Event('error'))
        this.onclose?.(new CloseEvent('close', { code: 1006, reason: 'Connection failed' }))
      } else {
        this.readyState = MockWebSocket.OPEN
        this.onopen?.(new Event('open'))

        // Send any queued messages
        this._queuedMessages.forEach(msg => {
          this.onmessage?.({ data: msg } as MessageEvent)
        })
        this._queuedMessages = []
      }
    }, delay)
  }

  send(data: string | ArrayBufferLike | Blob | ArrayBufferView): void {
    if (this.readyState !== MockWebSocket.OPEN) {
      throw new Error('WebSocket is not open')
    }

    this.messagesSent.push(data as string)

    // Parse JSON-RPC request and generate response
    try {
      const request = JSON.parse(data as string)
      if (request.method) {
        this._handleRequest(request)
      }
    } catch {
      // Not JSON, ignore
    }
  }

  private _handleRequest(request: { id?: number; method: string; params?: unknown }) {
    const method = request.method
    const id = request.id ?? this._messageId++

    // Check for registered response
    if (this._responses.has(method)) {
      const result = this._responses.get(method)
      const response = JSON.stringify({
        jsonrpc: '2.0',
        id,
        result
      })

      // Simulate async response
      setTimeout(() => {
        this.onmessage?.({ data: response } as MessageEvent)
      }, 0)
    }
  }

  close(code?: number, reason?: string): void {
    this.readyState = MockWebSocket.CLOSING
    setTimeout(() => {
      this.readyState = MockWebSocket.CLOSED
      this.onclose?.(new CloseEvent('close', { code: code ?? 1000, reason: reason ?? '' }))
    }, 0)
  }

  addEventListener(type: string, listener: EventListener): void {
    switch (type) {
      case 'open':
        this.onopen = listener as EventHandler
        break
      case 'close':
        this.onclose = listener as CloseHandler
        break
      case 'message':
        this.onmessage = listener as MessageHandler
        break
      case 'error':
        this.onerror = listener as ErrorHandler
        break
    }
  }

  removeEventListener(_type: string, _listener: EventListener): void {
    // No-op for tests
  }

  dispatchEvent(_event: Event): boolean {
    return true
  }

  // ==========================================================================
  // Test Control Methods
  // ==========================================================================

  /**
   * Add a response for a JSON-RPC method
   */
  addResponse(method: string, result: unknown): void {
    this._responses.set(method, result)
  }

  /**
   * Simulate receiving a server notification
   */
  simulateNotification(method: string, params: unknown): void {
    const notification = JSON.stringify({
      jsonrpc: '2.0',
      method,
      params
    })

    if (this.readyState === MockWebSocket.OPEN) {
      this.onmessage?.({ data: notification } as MessageEvent)
    } else {
      this._queuedMessages.push(notification)
    }
  }

  /**
   * Simulate a connection error
   */
  simulateError(): void {
    this.onerror?.(new Event('error'))
  }

  /**
   * Simulate connection close
   */
  simulateClose(code: number = 1000, reason: string = ''): void {
    this.readyState = MockWebSocket.CLOSED
    this.onclose?.(new CloseEvent('close', { code, reason }))
  }

  /**
   * Get the last sent message as parsed JSON
   */
  getLastMessage(): unknown {
    const last = this.messagesSent[this.messagesSent.length - 1]
    return last ? JSON.parse(last) : null
  }

  /**
   * Clear all sent messages
   */
  clearMessages(): void {
    this.messagesSent = []
  }

  // ==========================================================================
  // Static Control Methods
  // ==========================================================================

  /**
   * Get all created WebSocket instances
   */
  static getInstances(): MockWebSocket[] {
    return this._instances
  }

  /**
   * Get the most recent WebSocket instance
   */
  static getLastInstance(): MockWebSocket | undefined {
    return this._instances[this._instances.length - 1]
  }

  /**
   * Clear all instances
   */
  static clearInstances(): void {
    this._instances = []
  }

  /**
   * Configure connection to fail
   */
  static setConnectionShouldFail(shouldFail: boolean): void {
    this._shouldFailConnection = shouldFail
  }

  /**
   * Set connection delay in milliseconds
   */
  static setConnectionDelay(delay: number): void {
    this._connectionDelay = delay
  }

  /**
   * Reset all static configuration
   */
  static reset(): void {
    this._instances = []
    this._shouldFailConnection = false
    this._connectionDelay = 0
  }
}

/**
 * Install MockWebSocket as the global WebSocket
 *
 * Usage:
 *   beforeEach(() => {
 *     installMockWebSocket()
 *   })
 *
 *   afterEach(() => {
 *     MockWebSocket.reset()
 *   })
 */
export function installMockWebSocket(): void {
  vi.stubGlobal('WebSocket', MockWebSocket)
}

/**
 * Create a pre-configured mock WebSocket for common test scenarios
 */
export function createConnectedMockWebSocket(url: string = 'ws://localhost:1780/jsonrpc'): MockWebSocket {
  const ws = new MockWebSocket(url)

  // Add default Snapcast responses
  ws.addResponse('Server.GetStatus', {
    server: {
      groups: [],
      streams: [
        {
          id: 'none',
          status: 'idle',
          properties: {},
          uri: { raw: 'pipe:///tmp/none-fifo' }
        }
      ]
    }
  })

  return ws
}
