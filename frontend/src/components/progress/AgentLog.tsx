import type { WSEvent } from '../../lib/types'

interface AgentLogProps {
  events: WSEvent[]
}

export function AgentLog({ events }: AgentLogProps) {
  if (events.length === 0) return null

  return (
    <div style={{
      padding: '0.8rem',
      background: '#1a1a2e',
      borderRadius: 8,
      maxHeight: 300,
      overflowY: 'auto',
      fontFamily: 'monospace',
      fontSize: '0.8rem',
    }}>
      {events.map((e, i) => (
        <div key={i} style={{ marginBottom: '0.3rem', color: '#a0e0a0' }}>
          <span style={{ color: '#888' }}>[{new Date().toLocaleTimeString()}]</span>{' '}
          <span style={{ color: '#60a5fa' }}>{e.stage}</span>{' '}
          {e.message}
        </div>
      ))}
    </div>
  )
}
