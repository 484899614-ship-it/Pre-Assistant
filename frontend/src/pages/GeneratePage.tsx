import { useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { Bot, Cpu, Zap, Globe, Key, Eye, EyeOff, Palette, Terminal, X, UploadCloud, StopCircle, RotateCcw, RefreshCw } from 'lucide-react'
import { uploadPDF, extractPDF, getProviders, generatePresentation, retryGeneration } from '../lib/api'
import { useGeneration } from '../hooks/useGeneration'
import type { ProviderListItem, WSEvent } from '../lib/types'

export default function GeneratePage() {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const {
    sessionId, jobId, status, slides, loading, error,
    wsClient, uploadAndExtract, startGeneration, submitFeedback,
    continueWithOutline, cancelGeneration, refreshPreview, reset,
  } = useGeneration()

  const [providers, setProviders] = useState<ProviderListItem[]>([])

  // Initialize from localStorage
  const _load = (key: string, fallback: string = ''): string => {
    try { return localStorage.getItem(key) || fallback } catch { return fallback }
  }
  const _save = (key: string, value: string) => {
    try { localStorage.setItem(key, value) } catch { /* ignore */ }
  }

  const [provider, setProvider] = useState(() => _load('gen_provider'))
  const [model, setModel] = useState(() => _load(`gen_model_${_load('gen_provider')}`))
  const [baseUrl, setBaseUrl] = useState(() => _load(`gen_baseUrl_${_load('gen_provider')}`))
  const [apiKey, setApiKey] = useState(() => _load('gen_apiKey'))
  const [showKey, setShowKey] = useState(false)

  const [style, setStyle] = useState('academic')
  const [canvasFormat, setCanvasFormat] = useState('ppt169')
  const [language, setLanguage] = useState('zh')
  const [detailLevel, setDetailLevel] = useState('normal')
  const [instruction, setInstruction] = useState('')
  const [genMode, setGenMode] = useState<'fancy' | 'quick'>('fancy')
  const [numPages, setNumPages] = useState('')
  const [speechMinutes, setSpeechMinutes] = useState('')
  const [pptDensity, setPptDensity] = useState<'compact' | 'normal' | 'spacious'>('normal')
  const [themeColor, setThemeColor] = useState('')

  const [file, setFile] = useState<File | null>(null)
  const [uploading, setUploading] = useState(false)
  const [uploaded, setUploaded] = useState(false)
  const [selectedSlide, setSelectedSlide] = useState(0)
  const [secondaryPanel, setSecondaryPanel] = useState<'style' | 'log' | null>(null)

  // Outline confirmation
  const [outlineManuscript, setOutlineManuscript] = useState<string | null>(null)
  const [isEditingOutline, setIsEditingOutline] = useState(false)
  const [editedOutline, setEditedOutline] = useState('')
  const [continueLoading, setContinueLoading] = useState(false)
  const [outlineDismissed, setOutlineDismissed] = useState(false)

  const freshRequested = searchParams.get('fresh') === '1'

  useEffect(() => {
    getProviders().then(res => setProviders(res.providers)).catch(() => {})
  }, [])

  useEffect(() => {
    if (freshRequested) {
      reset()
      setFile(null)
      setUploaded(false)
      navigate('/generate', { replace: true })
    }
  }, [freshRequested])

  useEffect(() => {
    if (providers.length > 0 && (!model || !baseUrl)) {
      const p = providers.find(x => x.name === provider) || providers[0]
      if (!provider) setProvider(p.name)
      if (!model) setModel(_load(`gen_model_${p.name}`) || p.models[0]?.id || '')
      if (!baseUrl) setBaseUrl(_load(`gen_baseUrl_${p.name}`) || p.default_base_url || '')
    }
  }, [provider, model, baseUrl, providers])

  // Persist to localStorage on change
  useEffect(() => { if (provider) _save('gen_provider', provider) }, [provider])
  useEffect(() => { _save(`gen_model_${provider}`, model) }, [model, provider])
  useEffect(() => { _save(`gen_baseUrl_${provider}`, baseUrl) }, [baseUrl, provider])
  useEffect(() => { _save('gen_apiKey', apiKey) }, [apiKey])

  // When provider changes, restore last-used model & baseUrl for that provider
  const handleProviderChange = (name: string) => {
    setProvider(name)
    const p = providers.find(x => x.name === name)
    setModel(_load(`gen_model_${name}`) || p?.models[0]?.id || '')
    setBaseUrl(_load(`gen_baseUrl_${name}`) || p?.default_base_url || '')
  }

  useEffect(() => {
    if (status?.status === 'awaiting_confirmation' && status.data?.manuscript && !outlineDismissed) {
      setOutlineManuscript(status.data.manuscript as string)
      setEditedOutline(status.data.manuscript as string)
    }
  }, [status?.status, status?.data, outlineDismissed])

  useEffect(() => {
    if (status?.status === 'complete' && jobId) {
      navigate(`/result/${jobId}`)
    }
  }, [status?.status, jobId, navigate])

  const selectedProvider = providers.find(p => p.name === provider)
  const datalistId = `model-options-${provider || 'default'}`

  async function handleUpload() {
    if (!file) return
    setUploading(true)
    try {
      await uploadAndExtract(file)
      setUploaded(true)
    } catch (e: any) {
      alert('上传失败: ' + e.message)
    } finally {
      setUploading(false)
    }
  }

  async function handleGenerate() {
    if (!sessionId || !apiKey) return
    setOutlineDismissed(false)
    try {
      await startGeneration(
        { provider, model: model.trim(), api_key: apiKey.trim(), base_url: baseUrl.trim() || undefined },
        {
          canvas_format: canvasFormat, style, language, detail_level: detailLevel, mode: genMode,
          num_pages: numPages ? parseInt(numPages, 10) || undefined : undefined,
          speech_minutes: speechMinutes ? parseInt(speechMinutes, 10) || undefined : undefined,
          theme_color: themeColor || undefined,
          style_overrides: { density: pptDensity },
        },
        instruction,
      )
    } catch (e: any) {
      alert('启动失败: ' + e.message)
    }
  }

  async function handleConfirmOutline() {
    if (!jobId) return
    setContinueLoading(true)
    setOutlineDismissed(true)  // prevent re-popup
    try {
      const manuscriptToUse = isEditingOutline ? editedOutline : (outlineManuscript || editedOutline)
      await continueWithOutline(manuscriptToUse, {
        provider, model: model.trim(), api_key: apiKey, base_url: baseUrl || undefined,
      })
      setOutlineManuscript(null)
    } catch (err: any) {
      const msg = typeof err?.message === 'string' ? err.message : JSON.stringify(err)
      alert('确认大纲失败: ' + msg + '\n\n请检查 API Key 和账号是否正常，然后重试。')
      setOutlineDismissed(false)  // allow retry if failed
    } finally {
      setContinueLoading(false)
    }
  }

  async function handleRetry() {
    if (!jobId) return
    try {
      const mc = { provider, model: model.trim(), api_key: apiKey.trim(), base_url: baseUrl.trim() || undefined }
      await retryGeneration(jobId, mc)
    } catch (err: any) {
      const msg = typeof err?.message === 'string' ? err.message : JSON.stringify(err)
      alert('重试失败: ' + msg)
    }
  }

  const isRunning = status && !['complete', 'error', 'cancelled'].includes(status.status)
  const pct = status ? Math.round(status.progress * 100) : 0

  const STAGES = [
    { key: 'parsing', label: 'PDF 解析' },
    { key: 'research', label: '论文分析' },
    { key: 'strategy', label: '设计策略' },
    { key: 'generation', label: 'SVG 生成' },
    { key: 'notes', label: '演讲讲稿' },
    { key: 'postprocess', label: '后处理' },
    { key: 'export', label: '导出' },
  ]

  return (
    <div>
      {/* Outline Confirmation Modal */}
      {outlineManuscript && (
        <div style={{
          position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
          background: 'rgba(0,0,0,0.5)', zIndex: 1000,
          display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 24,
        }}>
          <div style={{
            background: 'var(--surface-strong)', borderRadius: 28, maxWidth: 720, width: '100%',
            maxHeight: '80vh', display: 'flex', flexDirection: 'column',
            boxShadow: '0 30px 90px rgba(0,0,0,0.4)', border: '1px solid var(--line)',
          }}>
            <div style={{ padding: '20px 24px', borderBottom: '1px solid var(--line)', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <div>
                <h2 style={{ margin: 0, fontSize: 18, fontWeight: 600 }}>确认大纲</h2>
                <p style={{ margin: '4px 0 0', fontSize: 13, color: 'var(--muted)' }}>
                  在生成幻灯片之前，审阅并编辑生成的大纲。
                </p>
              </div>
              <button type="button" className="icon-btn" onClick={() => { setOutlineManuscript(null); setOutlineDismissed(true) }}>
                <X size={17} />
              </button>
            </div>
            <div style={{ flex: 1, overflow: 'auto', padding: 24 }}>
              {isEditingOutline ? (
                <textarea
                  value={editedOutline}
                  onChange={(e) => setEditedOutline(e.target.value)}
                  style={{
                    width: '100%', minHeight: 400, fontSize: 13, lineHeight: 1.6,
                    border: '1px solid var(--line)', borderRadius: 18, padding: 16,
                    background: 'var(--surface-strong)', color: 'var(--text)',
                    fontFamily: 'var(--mono)', resize: 'vertical', outline: 'none',
                  }}
                />
              ) : (
                <div style={{ fontSize: 14, lineHeight: 1.8, whiteSpace: 'pre-wrap' }}>
                  {outlineManuscript}
                </div>
              )}
            </div>
            <div style={{ padding: '16px 24px', borderTop: '1px solid var(--line)', display: 'flex', gap: 12, justifyContent: 'flex-end' }}>
              <button type="button" className="secondary-button" onClick={() => setIsEditingOutline(v => !v)}>
                {isEditingOutline ? '预览' : '编辑'}
              </button>
              <button type="button" className="primary-button" disabled={continueLoading} onClick={handleConfirmOutline}>
                {continueLoading ? '正在启动...' : '确认并生成'}
              </button>
            </div>
          </div>
        </div>
      )}

      <section className="studio-layout">
        {/* Left Column: Upload + Progress */}
        <div className="studio-column-left">
          {/* Upload Zone */}
          <div
            className={`upload-zone ${uploading ? 'upload-zone-dragging' : ''}`}
            onClick={() => document.getElementById('file-input')?.click()}
            onDragOver={(e) => { e.preventDefault(); e.stopPropagation() }}
            onDrop={(e) => {
              e.preventDefault()
              const f = e.dataTransfer.files[0]
              if (f && (f.type === 'application/pdf' || f.type === 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')) setFile(f)
            }}
          >
            <input
              id="file-input"
              type="file"
              accept=".pdf,.docx"
              onChange={(e) => {
                const f = e.target.files?.[0]
                if (f) { setFile(f); setUploaded(false) }
              }}
            />
            <div className="upload-icon"><UploadCloud size={40} /></div>
            <div className="upload-hint">
              {file ? file.name : '拖放 PDF 或 Word 文档到此处，或点击选择'}
            </div>
            {uploaded && <div className="upload-done">已上传并解析</div>}
            {file && !uploaded && (
              <button
                className="primary-button full-width"
                disabled={uploading}
                onClick={(e) => { e.stopPropagation(); handleUpload() }}
                style={{ fontSize: '0.88rem', minHeight: 40 }}
              >
                {uploading ? '处理中...' : '上传并解析'}
              </button>
            )}
          </div>

          {/* Progress Panel */}
          {status && (
            <div className="panel">
              <div className="panel-header-row">
                <Bot size={15} className="panel-title-icon" />
                <p className="panel-title">生成进度</p>
              </div>
              <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '0.5rem' }}>
                <span style={{ fontSize: '0.88rem' }}>{STAGES.find(s => s.key === status.status)?.label || status.status}</span>
                <span style={{ fontSize: '0.88rem', color: 'var(--accent)' }}>{pct}%</span>
              </div>
              <div className="progress-bar-track">
                <div className="progress-bar-fill" style={{ width: `${pct}%` }} />
              </div>
              {status.message && (
                <p style={{ color: 'var(--muted)', fontSize: '0.82rem', marginTop: '0.5rem' }}>{status.message}</p>
              )}
              {status.total_slides > 0 && (
                <p style={{ fontSize: '0.82rem', marginTop: '0.3rem' }}>
                  幻灯片: {status.slides_completed}/{status.total_slides}
                </p>
              )}
              <div className="progress-stage-list">
                {STAGES.map(s => {
                  const stageIdx = STAGES.findIndex(x => x.key === status.status)
                  const curIdx = STAGES.indexOf(s)
                  const isActive = s.key === status.status
                  const isComplete = curIdx < stageIdx
                  return (
                    <div
                      key={s.key}
                      className={`progress-stage-item ${isActive ? 'progress-stage-item-active' : ''} ${isComplete ? 'progress-stage-item-complete' : ''}`}
                    >
                      {s.label}
                    </div>
                  )
                })}
              </div>
            </div>
          )}

          {error && <p className="error-text">{error}</p>}
        </div>

        {/* Center Column: Slide Viewer + Thumbnails */}
        <div className="studio-column-preview">
          {slides.length > 0 ? (
            <>
              <div className="viewer-panel">
                <div className="viewer-frame" dangerouslySetInnerHTML={{ __html: slides[selectedSlide]?.content || '' }} />
              </div>
              <div className="slide-preview-grid">
                {slides.map((slide, i) => (
                  <div
                    key={i}
                    className={`thumbnail-card ${i === selectedSlide ? 'thumbnail-card-active' : ''}`}
                    onClick={() => setSelectedSlide(i)}
                  >
                    <div dangerouslySetInnerHTML={{ __html: slide.content }} style={{ background: 'white', borderRadius: 14, overflow: 'hidden' }} />
                    <div className="thumbnail-caption">幻灯片 {slide.index}</div>
                  </div>
                ))}
              </div>
            </>
          ) : (
            <div className="viewer-empty">等待生成幻灯片...</div>
          )}
        </div>

        {/* Right Column: Config */}
        <div className="studio-config-rail">
          {/* Model Selector */}
          <section className="panel">
            <div className="panel-header-row">
              <div>
                <div className="panel-title-row">
                  <Bot size={15} className="panel-title-icon" />
                  <p className="panel-title">模型配置</p>
                </div>
                <p className="panel-support-text">{selectedProvider?.display_name || '选择供应商'}</p>
              </div>
            </div>

            <label className="form-field">
              <span>供应商</span>
              <div className="form-field-icon">
                <Cpu size={14} className="field-icon" />
                <select value={provider} onChange={(e) => handleProviderChange(e.target.value)}>
                  {providers.map(p => (
                    <option key={p.name} value={p.name}>{p.display_name}</option>
                  ))}
                </select>
              </div>
            </label>

            <label className="form-field">
              <span>模型</span>
              <div className="form-field-icon">
                <Zap size={14} className="field-icon" />
                <input
                  list={datalistId}
                  value={model}
                  placeholder="输入或选择模型名称"
                  onChange={(e) => setModel(e.target.value)}
                />
              </div>
              <datalist id={datalistId}>
                {selectedProvider?.models.map(m => (
                  <option key={m.id} value={m.id}>{m.display_name}</option>
                ))}
              </datalist>
            </label>

            <label className="form-field">
              <span>Base URL</span>
              <div className="form-field-icon">
                <Globe size={14} className="field-icon" />
                <input
                  type="url"
                  placeholder="自定义 API 地址（可选）"
                  value={baseUrl}
                  onChange={(e) => setBaseUrl(e.target.value)}
                />
              </div>
            </label>

            <label className="form-field">
              <span>API Key</span>
              <div className="form-field-icon api-key-wrapper">
                <Key size={14} className="field-icon" />
                <input
                  type={showKey ? 'text' : 'password'}
                  placeholder="输入 API 密钥"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                />
                <button type="button" className="api-key-toggle" onClick={() => setShowKey(v => !v)} tabIndex={-1}>
                  {showKey ? <EyeOff size={14} /> : <Eye size={14} />}
                </button>
              </div>
            </label>
          </section>

          {/* Options */}
          <section className="panel">
            <div className="panel-header-row">
              <p className="panel-title">生成选项</p>
            </div>
            <div className="options-grid">
              <label className="form-field">
                <span>画幅比例</span>
                <select value={canvasFormat} onChange={(e) => setCanvasFormat(e.target.value)}>
                  <option value="ppt169">16:9 宽屏</option>
                  <option value="ppt43">4:3 标准</option>
                </select>
              </label>
              <label className="form-field">
                <span>语言</span>
                <select value={language} onChange={(e) => setLanguage(e.target.value)}>
                  <option value="zh">中文</option>
                  <option value="en">英文</option>
                </select>
              </label>
              <label className="form-field">
                <span>详细程度</span>
                <select value={detailLevel} onChange={(e) => setDetailLevel(e.target.value)}>
                  <option value="concise">简洁</option>
                  <option value="normal">标准</option>
                  <option value="detailed">详细</option>
                </select>
              </label>
              <label className="form-field">
                <span>生成页数</span>
                <input
                  type="number"
                  min="3"
                  max="30"
                  placeholder="自动"
                  value={numPages}
                  onChange={(e) => setNumPages(e.target.value)}
                />
              </label>
              <label className="form-field">
                <span>PPT 内容</span>
                <select value={pptDensity} onChange={(e) => setPptDensity(e.target.value as 'compact' | 'normal' | 'spacious')}>
                  <option value="compact">紧凑</option>
                  <option value="normal">适中</option>
                  <option value="spacious">精简</option>
                </select>
              </label>
              <label className="form-field">
                <span>演讲时间（分钟）</span>
                <input
                  type="number"
                  min="1"
                  max="60"
                  placeholder="自动"
                  value={speechMinutes}
                  onChange={(e) => setSpeechMinutes(e.target.value)}
                />
              </label>
              <label className="form-field">
                <span>主题颜色</span>
                <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                  {[
                    { color: '', label: '默认' },
                    { color: '#1A365D', label: '深蓝' },
                    { color: '#065F46', label: '深绿' },
                    { color: '#7C2D12', label: '深棕' },
                    { color: '#581C87', label: '紫色' },
                    { color: '#991B1B', label: '深红' },
                    { color: '#1E3A5F', label: '海蓝' },
                    { color: '#334155', label: '石墨' },
                  ].map(c => (
                    <button
                      key={c.color}
                      type="button"
                      title={c.label}
                      onClick={() => setThemeColor(c.color)}
                      style={{
                        width: 22, height: 22, borderRadius: '50%', border: themeColor === c.color ? '2px solid var(--accent)' : '2px solid var(--line)',
                        background: c.color || 'linear-gradient(135deg, #1A365D, #065F46, #581C87)',
                        cursor: 'pointer', padding: 0, flexShrink: 0,
                      }}
                    />
                  ))}
                </div>
              </label>
            </div>
            <label className="form-field" style={{ marginTop: '0.7rem' }}>
              <span>补充指令</span>
              <textarea
                value={instruction}
                onChange={(e) => setInstruction(e.target.value)}
                placeholder="可选：对生成的额外要求"
                style={{ minHeight: 60, resize: 'vertical' }}
              />
            </label>
          </section>

          {/* Style & Log Buttons */}
          <div className="studio-secondary-actions">
            <button
              type="button"
              className={`secondary-action ${secondaryPanel === 'style' ? 'secondary-action-active' : ''}`}
              onClick={() => setSecondaryPanel(v => v === 'style' ? null : 'style')}
            >
              <Palette size={16} />
              <span>风格</span>
            </button>
            <button
              type="button"
              className={`secondary-action ${secondaryPanel === 'log' ? 'secondary-action-active' : ''}`}
              onClick={() => setSecondaryPanel(v => v === 'log' ? null : 'log')}
            >
              <Terminal size={16} />
              <span>日志</span>
            </button>
          </div>

          {/* Style Picker (inline) */}
          {secondaryPanel === 'style' && (
            <section className="panel">
              <div className="panel-header-row">
                <Palette size={15} className="panel-title-icon" />
                <p className="panel-title">视觉风格</p>
              </div>
              <div className="style-grid">
                {[
                  { key: 'academic', icon: '🎓', name: '学术', desc: '简洁专业' },
                  { key: 'consulting', icon: '💼', name: '咨询', desc: '结构清晰' },
                  { key: 'tech', icon: '⚡', name: '科技', desc: '现代感强' },
                  { key: 'nature', icon: '🌿', name: '自然', desc: '柔和有机' },
                ].map(s => (
                  <div
                    key={s.key}
                    className={`style-card ${style === s.key ? 'style-card-active' : ''}`}
                    onClick={() => setStyle(s.key)}
                  >
                    <div className="style-card-icon">{s.icon}</div>
                    <div className="style-card-name">{s.name}</div>
                    <div className="style-card-desc">{s.desc}</div>
                  </div>
                ))}
              </div>
            </section>
          )}

          {/* Agent Log (inline) */}
          {secondaryPanel === 'log' && (
            <section className="panel">
              <div className="panel-header-row">
                <Terminal size={15} className="panel-title-icon" />
                <p className="panel-title">智能体日志</p>
              </div>
              <div className="agent-log">
                {status ? (
                  <div className="agent-log-line">
                    <span className="agent-log-time">[{new Date().toLocaleTimeString()}]</span>{' '}
                    <span className="agent-log-stage">{status.status}</span>{' '}
                    {status.message}
                  </div>
                ) : (
                  <div style={{ color: 'var(--muted)' }}>暂无日志</div>
                )}
              </div>
            </section>
          )}

          {/* Launch Button */}
          <button
            type="button"
            className="primary-button full-width"
            disabled={!sessionId || !provider || !model.trim() || !apiKey || !!isRunning}
            onClick={handleGenerate}
          >
            {isRunning ? '生成中...' : '开始生成'}
          </button>

          {/* Cancel Button */}
          {!!isRunning && (
            <button
              type="button"
              className="secondary-button full-width"
              style={{ borderColor: 'rgba(248, 113, 113, 0.3)', color: '#f87171' }}
              onClick={cancelGeneration}
            >
              <StopCircle size={16} style={{ marginRight: 6, verticalAlign: -2 }} />
              取消生成
            </button>
          )}

          {/* Retry / Reset on error/cancel */}
          {status && (status.status === 'error' || status.status === 'cancelled') && (
            <div style={{ display: 'flex', gap: 8, width: '100%' }}>
              <button
                type="button"
                className="primary-button"
                style={{ flex: 1 }}
                onClick={handleRetry}
              >
                <RefreshCw size={16} style={{ marginRight: 6, verticalAlign: -2 }} />
                重试
              </button>
              <button
                type="button"
                className="secondary-button"
                style={{ borderColor: 'rgba(248, 113, 113, 0.3)', color: '#f87171' }}
                onClick={() => { reset(); setFile(null); setUploaded(false); (document.getElementById('file-input') as HTMLInputElement).value = '' }}
              >
                <RotateCcw size={16} style={{ marginRight: 6, verticalAlign: -2 }} />
                重置
              </button>
            </div>
          )}
        </div>
      </section>
    </div>
  )
}
