import type { JobStatus } from '../../lib/types'

interface ProgressPanelProps {
  status: JobStatus | null
}

const STAGE_LABELS: Record<string, string> = {
  parsing: 'PDF Parsing',
  research: 'Research & Analysis',
  strategy: 'Design Strategy',
  generation: 'SVG Generation',
  postprocess: 'Post-Processing',
  export: 'Export',
  awaiting_confirmation: 'Awaiting Confirmation',
  complete: 'Complete',
  error: 'Error',
}

export function ProgressPanel({ status }: ProgressPanelProps) {
  if (!status) return null

  const pct = Math.round(status.progress * 100)

  return (
    <div style={{ padding: '1rem', background: '#f0f7ff', borderRadius: 8 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '0.5rem' }}>
        <strong>{STAGE_LABELS[status.status] || status.status}</strong>
        <span>{pct}%</span>
      </div>

      <div style={{ background: '#ddd', height: 8, borderRadius: 4 }}>
        <div
          style={{
            background: status.status === 'error' ? '#E53E3E' : '#1A365D',
            height: 8,
            borderRadius: 4,
            width: `${pct}%`,
            transition: 'width 0.3s',
          }}
        />
      </div>

      {status.message && (
        <p style={{ color: '#666', fontSize: '0.85rem', marginTop: '0.5rem' }}>{status.message}</p>
      )}

      {status.total_slides > 0 && (
        <p style={{ fontSize: '0.85rem', marginTop: '0.3rem' }}>
          Slides: {status.slides_completed ?? 0}/{status.total_slides}
        </p>
      )}
    </div>
  )
}
