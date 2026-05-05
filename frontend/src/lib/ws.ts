import { WSEvent } from './types'

export class WSClient {
  private ws: WebSocket | null = null
  private jobId: string
  private onEvent: (event: WSEvent) => void
  private reconnectAttempts = 0
  private maxReconnectAttempts = 10
  private lastSeq = 0

  constructor(jobId: string, onEvent: (event: WSEvent) => void) {
    this.jobId = jobId
    this.onEvent = onEvent
  }

  connect() {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const url = `${proto}//${window.location.host}/ws/${this.jobId}?since_seq=${this.lastSeq}`
    this.ws = new WebSocket(url)

    this.ws.onmessage = (event) => {
      try {
        const data: WSEvent = JSON.parse(event.data)
        if (data.type === 'ping') return
        if (data.seq) this.lastSeq = data.seq
        this.onEvent(data)
      } catch { /* ignore parse errors */ }
    }

    this.ws.onclose = () => {
      if (this.reconnectAttempts < this.maxReconnectAttempts) {
        this.reconnectAttempts++
        const delay = Math.min(1000 * Math.pow(2, this.reconnectAttempts), 30000)
        setTimeout(() => this.connect(), delay)
      }
    }

    this.ws.onerror = () => {
      this.ws?.close()
    }
  }

  disconnect() {
    this.reconnectAttempts = this.maxReconnectAttempts
    this.ws?.close()
    this.ws = null
  }
}
