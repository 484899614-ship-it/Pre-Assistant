import type { ProviderListItem, ModelConfig } from '../../lib/types'

interface ModelSelectorProps {
  providers: ProviderListItem[]
  config: ModelConfig
  apiKey: string
  onChange: (config: ModelConfig, apiKey: string) => void
}

export function ModelSelector({ providers, config, apiKey, onChange }: ModelSelectorProps) {
  const currentProvider = providers.find(p => p.name === config.provider)

  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0.8rem' }}>
      <label>
        <span style={{ fontSize: '0.85rem', fontWeight: 500 }}>Provider</span>
        <select
          value={config.provider}
          onChange={(e) => {
            const p = providers.find(x => x.name === e.target.value)
            onChange(
              { ...config, provider: e.target.value, model: p?.models[0]?.id || '' },
              apiKey,
            )
          }}
          style={{ width: '100%', marginTop: '0.3rem', padding: '0.4rem' }}
        >
          {providers.map(p => (
            <option key={p.name} value={p.name}>{p.display_name}</option>
          ))}
        </select>
      </label>

      <label>
        <span style={{ fontSize: '0.85rem', fontWeight: 500 }}>Model</span>
        <select
          value={config.model}
          onChange={(e) => onChange({ ...config, model: e.target.value }, apiKey)}
          style={{ width: '100%', marginTop: '0.3rem', padding: '0.4rem' }}
        >
          {currentProvider?.models.map(m => (
            <option key={m.id} value={m.id}>{m.display_name}</option>
          ))}
        </select>
      </label>

      <label style={{ gridColumn: '1 / -1' }}>
        <span style={{ fontSize: '0.85rem', fontWeight: 500 }}>API Key</span>
        <input
          type="password"
          value={apiKey}
          onChange={(e) => onChange(config, e.target.value)}
          placeholder="Enter your API key"
          style={{ width: '100%', marginTop: '0.3rem', padding: '0.4rem' }}
        />
      </label>
    </div>
  )
}
