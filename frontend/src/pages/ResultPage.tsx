import { useEffect, useState, useMemo, useCallback } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { Download, MessageSquare, CheckCircle, AlertCircle, FileText, Quote, Pencil, X, Check } from 'lucide-react'
import { getJobStatus, getPreview, downloadURL, refinePresentation } from '../lib/api'
import { WSClient } from '../lib/ws'
import type { JobStatus, PreviewSlide, WSEvent } from '../lib/types'

/** Extract [来源: ...] markers from notes text and return highlighted JSX */
function HighlightedNotes({ text }: { text: string }) {
  const parts = useMemo(() => {
    const result: { type: 'text' | 'source'; content: string }[] = []
    const regex = /\[来源[:：]\s*([^\]]+)\]/g
    let lastIndex = 0
    let match: RegExpExecArray | null
    while ((match = regex.exec(text)) !== null) {
      if (match.index > lastIndex) {
        result.push({ type: 'text', content: text.slice(lastIndex, match.index) })
      }
      result.push({ type: 'source', content: match[1] })
      lastIndex = regex.lastIndex
    }
    if (lastIndex < text.length) {
      result.push({ type: 'text', content: text.slice(lastIndex) })
    }
    return result
  }, [text])

  return (
    <span>
      {parts.map((part, i) =>
        part.type === 'source' ? (
          <span
            key={i}
            className="source-highlight"
            title={part.content}
          >
            <Quote size={12} style={{ verticalAlign: -1, marginRight: 2, opacity: 0.7 }} />
            {part.content}
          </span>
        ) : (
          <span key={i}>{part.content}</span>
        )
      )}
    </span>
  )
}

export default function ResultPage() {
  const { jobId } = useParams<{ jobId: string }>()
  const navigate = useNavigate()
  const [status, setStatus] = useState<JobStatus | null>(null)
  const [slides, setSlides] = useState<PreviewSlide[]>([])
  const [selectedSlide, setSelectedSlide] = useState<number>(0)
  const [feedback, setFeedback] = useState('')
  const [refineLoading, setRefineLoading] = useState(false)

  // Editable notes: track which slide is being edited and its draft text
  const [editingSlide, setEditingSlide] = useState<number | null>(null)
  const [editDraft, setEditDraft] = useState('')
  // Store locally edited notes (overrides from preview API)
  const [editedNotes, setEditedNotes] = useState<Record<number, string>>({})

  useEffect(() => {
    if (!jobId) return

    // Initial load
    getJobStatus(jobId).then(s => {
      setStatus(s)
      if (s.status === 'complete') {
        getPreview(jobId).then(p => setSlides(p.slides))
      }
    }).catch(() => {})

    // Poll
    const poll = setInterval(async () => {
      try {
        const s = await getJobStatus(jobId)
        setStatus(s)
        if (s.status === 'complete' || s.status === 'error') {
          clearInterval(poll)
          if (s.status === 'complete') {
            const p = await getPreview(jobId)
            setSlides(p.slides)
          }
        }
      } catch { /* ignore */ }
    }, 2000)

    // WebSocket
    const ws = new WSClient(jobId, (event: WSEvent) => {
      setStatus(prev => prev ? {
        ...prev,
        status: event.stage,
        progress: event.progress,
        message: event.message,
        slides_completed: event.slides_completed,
        total_slides: event.total_slides,
      } : prev)
    })
    ws.connect()

    return () => {
      clearInterval(poll)
      ws.disconnect()
    }
  }, [jobId])

  const isComplete = status?.status === 'complete'
  const isError = status?.status === 'error'
  const isRunning = status && !isComplete && !isError
  const pct = status ? Math.round(status.progress * 100) : 0

  // Current slide notes: use locally edited version if available
  const currentNotes = editedNotes[selectedSlide] ?? slides[selectedSlide]?.notes ?? ''

  const startEdit = useCallback((slideIndex: number) => {
    setEditingSlide(slideIndex)
    setEditDraft(editedNotes[slideIndex] ?? slides[slideIndex]?.notes ?? '')
  }, [editedNotes, slides])

  const saveEdit = useCallback(() => {
    if (editingSlide !== null) {
      setEditedNotes(prev => ({ ...prev, [editingSlide]: editDraft }))
      setEditingSlide(null)
    }
  }, [editingSlide, editDraft])

  const cancelEdit = useCallback(() => {
    setEditingSlide(null)
    setEditDraft('')
  }, [])

  async function handleRefine() {
    if (!jobId || !feedback.trim()) return
    setRefineLoading(true)
    try {
      const _load = (key: string, fb = ''): string => { try { return localStorage.getItem(key) || fb } catch { return fb } }
      const provider = _load('gen_provider')
      const model = _load(`gen_model_${provider}`)
      const api_key = _load('gen_apiKey')
      const base_url = _load(`gen_baseUrl_${provider}`) || null

      const model_config = (provider && model && api_key)
        ? { provider, model, api_key, base_url }
        : undefined

      const res = await refinePresentation(jobId, { feedback: feedback.trim(), model_config })
      navigate(`/result/${res.job_id}`)
    } catch (e: any) {
      const msg = typeof e?.message === 'string' ? e.message : JSON.stringify(e)
      alert('提交反馈失败: ' + msg)
    } finally {
      setRefineLoading(false)
    }
  }

  return (
    <div className="result-layout">
      {/* Main Content */}
      <div className="result-main">
        {/* Progress / Status */}
        {isRunning && (
          <div className="panel" style={{ padding: '1.2rem' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '0.5rem' }}>
              <span>正在生成...</span>
              <span style={{ color: 'var(--accent)' }}>{pct}%</span>
            </div>
            <div className="progress-bar-track">
              <div className="progress-bar-fill" style={{ width: `${pct}%` }} />
            </div>
            {status?.message && <p style={{ color: 'var(--muted)', fontSize: '0.85rem', marginTop: '0.5rem' }}>{status.message}</p>}
          </div>
        )}

        {isError && (
          <div className="panel" style={{ padding: '1.2rem', borderColor: '#f87171' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', color: '#f87171' }}>
              <AlertCircle size={18} />
              <strong>生成失败</strong>
            </div>
            <p style={{ color: 'var(--muted)', marginTop: '0.5rem' }}>{status?.error || status?.message}</p>
          </div>
        )}

        {isComplete && (
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', color: 'var(--success)', marginBottom: '1rem' }}>
            <CheckCircle size={20} />
            <strong>生成完成</strong>
            <span style={{ color: 'var(--muted)', fontSize: '0.88rem' }}>- 共 {slides.length} 页</span>
          </div>
        )}

        {/* Main two-column: thumbnails + slide + notes */}
        {slides.length > 0 ? (
          <div className="result-detail-layout">
            {/* Left: vertical thumbnail list */}
            <div className="result-thumb-list">
              {slides.map((slide, i) => (
                <div
                  key={i}
                  className={`result-thumb-item ${i === selectedSlide ? 'result-thumb-item-active' : ''}`}
                  onClick={() => setSelectedSlide(i)}
                >
                  <div className="result-thumb-preview" dangerouslySetInnerHTML={{ __html: slide.content }} />
                  <span className="result-thumb-label">
                    {i + 1}
                    {(editedNotes[i] ?? slide.notes) && <FileText size={9} style={{ marginLeft: 3, opacity: 0.5, verticalAlign: -1 }} />}
                  </span>
                </div>
              ))}
            </div>

            {/* Right: slide viewer + notes */}
            <div className="result-detail-content">
              <div className="viewer-panel">
                <div className="viewer-frame" dangerouslySetInnerHTML={{ __html: slides[selectedSlide]?.content || '' }} />
              </div>

              {/* Notes section */}
              <div className="result-notes-panel">
                <div className="result-notes-header">
                  <div style={{ display: 'flex', alignItems: 'center', gap: '0.4rem' }}>
                    <FileText size={14} style={{ color: 'var(--accent)' }} />
                    <span style={{ fontWeight: 600, fontSize: '0.88rem' }}>第 {selectedSlide + 1} 页讲稿</span>
                  </div>
                  {editingSlide === selectedSlide ? (
                    <div style={{ display: 'flex', gap: '0.3rem' }}>
                      <button className="icon-btn" onClick={saveEdit} title="保存" style={{ width: 28, height: 28 }}>
                        <Check size={14} />
                      </button>
                      <button className="icon-btn" onClick={cancelEdit} title="取消" style={{ width: 28, height: 28 }}>
                        <X size={14} />
                      </button>
                    </div>
                  ) : (
                    <button className="icon-btn" onClick={() => startEdit(selectedSlide)} title="编辑讲稿" style={{ width: 28, height: 28 }}>
                      <Pencil size={13} />
                    </button>
                  )}
                </div>
                {editingSlide === selectedSlide ? (
                  <textarea
                    className="result-notes-editor"
                    value={editDraft}
                    onChange={(e) => setEditDraft(e.target.value)}
                    autoFocus
                  />
                ) : (
                  <div className="result-notes-text">
                    {currentNotes ? <HighlightedNotes text={currentNotes} /> : <span style={{ color: 'var(--muted)', fontStyle: 'italic' }}>（无讲稿）</span>}
                  </div>
                )}
              </div>
            </div>
          </div>
        ) : !isRunning && !isError && (
          <div className="viewer-empty">暂无幻灯片数据</div>
        )}
      </div>

      {/* Right Sidebar */}
      <div className="result-sidebar">
        {/* Download */}
        {isComplete && jobId && (
          <a
            href={downloadURL(jobId)}
            download
            className="primary-button full-width"
            style={{ textDecoration: 'none', textAlign: 'center' }}
          >
            <Download size={16} style={{ marginRight: 8, verticalAlign: -2 }} />
            下载 PPTX
          </a>
        )}

        {/* Metrics */}
        {isComplete && slides.length > 0 && (
          <div className="metric-stripe">
            <span>幻灯片数量</span>
            <strong>{slides.length}</strong>
          </div>
        )}

        {isComplete && slides.some(s => s.notes || editedNotes[slides.indexOf(s)]) && (
          <div className="metric-stripe">
            <span>含讲稿页面</span>
            <strong>{slides.filter((s, i) => editedNotes[i] || s.notes).length}</strong>
          </div>
        )}

        {/* Feedback / Refine */}
        {isComplete && (
          <div className="panel feedback-area">
            <div className="panel-header-row">
              <MessageSquare size={15} className="panel-title-icon" />
              <p className="panel-title">反馈优化</p>
            </div>
            <textarea
              value={feedback}
              onChange={(e) => setFeedback(e.target.value)}
              placeholder={'输入反馈意见，如"第三页字体太大""增加一页总结"...'}
            />
            <button
              className="secondary-button full-width"
              disabled={!feedback.trim() || refineLoading}
              onClick={handleRefine}
              style={{ marginTop: '0.75rem' }}
            >
              {refineLoading ? '提交中...' : '提交反馈'}
            </button>
          </div>
        )}

      </div>
    </div>
  )
}
