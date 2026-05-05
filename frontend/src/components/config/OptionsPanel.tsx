import type { GenerationOptions } from '../../lib/types'

interface OptionsPanelProps {
  options: GenerationOptions
  onChange: (options: GenerationOptions) => void
}

const CANVAS_FORMATS = [
  { value: 'ppt169', label: '16:9 Widescreen' },
  { value: 'ppt43', label: '4:3 Standard' },
]

const STYLES = [
  { value: 'academic', label: 'Academic' },
  { value: 'consulting', label: 'Consulting' },
  { value: 'tech', label: 'Tech' },
  { value: 'nature', label: 'Nature' },
]

const LANGUAGES = [
  { value: 'zh', label: 'Chinese' },
  { value: 'en', label: 'English' },
]

const DETAIL_LEVELS = [
  { value: 'concise', label: 'Concise' },
  { value: 'normal', label: 'Normal' },
  { value: 'detailed', label: 'Detailed' },
]

export function OptionsPanel({ options, onChange }: OptionsPanelProps) {
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0.8rem' }}>
      <label>
        <span style={{ fontSize: '0.85rem', fontWeight: 500 }}>Format</span>
        <select
          value={options.canvas_format}
          onChange={(e) => onChange({ ...options, canvas_format: e.target.value as any })}
          style={{ width: '100%', marginTop: '0.3rem', padding: '0.4rem' }}
        >
          {CANVAS_FORMATS.map(f => <option key={f.value} value={f.value}>{f.label}</option>)}
        </select>
      </label>

      <label>
        <span style={{ fontSize: '0.85rem', fontWeight: 500 }}>Style</span>
        <select
          value={options.style}
          onChange={(e) => onChange({ ...options, style: e.target.value as any })}
          style={{ width: '100%', marginTop: '0.3rem', padding: '0.4rem' }}
        >
          {STYLES.map(s => <option key={s.value} value={s.value}>{s.label}</option>)}
        </select>
      </label>

      <label>
        <span style={{ fontSize: '0.85rem', fontWeight: 500 }}>Language</span>
        <select
          value={options.language}
          onChange={(e) => onChange({ ...options, language: e.target.value })}
          style={{ width: '100%', marginTop: '0.3rem', padding: '0.4rem' }}
        >
          {LANGUAGES.map(l => <option key={l.value} value={l.value}>{l.label}</option>)}
        </select>
      </label>

      <label>
        <span style={{ fontSize: '0.85rem', fontWeight: 500 }}>Detail Level</span>
        <select
          value={options.detail_level}
          onChange={(e) => onChange({ ...options, detail_level: e.target.value as any })}
          style={{ width: '100%', marginTop: '0.3rem', padding: '0.4rem' }}
        >
          {DETAIL_LEVELS.map(d => <option key={d.value} value={d.value}>{d.label}</option>)}
        </select>
      </label>
    </div>
  )
}
