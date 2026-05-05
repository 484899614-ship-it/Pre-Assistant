import { useEffect, useState, useCallback, useRef } from 'react'
import {
  Upload, Presentation, Clock, ChevronLeft, ChevronRight,
  Eye, X, FileUp, BookOpen, Monitor,
} from 'lucide-react'

/** Render speaker notes with markers highlighted:
 *  - [来源: "exact quote"] → purple highlight showing the quoted text, hover for full quote
 *  - [停顿] [过渡] [数据] etc. → subtle gray stage directions
 */
function HighlightedNotes({ text }: { text: string }) {
  // Split on all bracket markers
  const parts = text.split(/(\[(?:来源[:：][^\]]*|停顿|过渡|数据|Pause|Transition|Data)\])/gi)
  return (
    <>
      {parts.map((part, i) => {
        // [来源: "exact quote"] — source citation
        const src = part.match(/^\[来源[:：]\s*(.*)\]$/i)
        if (src) {
          return (
            <span
              key={i}
              style={{
                background: 'rgba(99, 102, 241, 0.12)',
                borderBottom: '1px dashed rgba(99, 102, 241, 0.5)',
                cursor: 'help',
                borderRadius: 2,
                padding: '1px 3px',
                fontSize: '0.92em',
                color: '#4f46e5',
              }}
              title={src[1]}
            >
              {src[1]}
            </span>
          )
        }
        // Stage direction markers
        if (/^\[(停顿|过渡|数据|Pause|Transition|Data)\]$/i.test(part)) {
          return (
            <span
              key={i}
              style={{
                color: 'rgba(107, 114, 128, 0.6)',
                fontSize: '0.85em',
                fontStyle: 'italic',
              }}
            >
              {part}
            </span>
          )
        }
        return <span key={i}>{part}</span>
      })}
    </>
  )
}
import { getPreview } from '../lib/api'
import type { PreviewSlide } from '../lib/types'

// ── Types ──

interface SlideData {
  index: number
  content: string  // SVG HTML or image URL
  notes: string
  type: 'svg' | 'image'
}

// ── Source Selection Screen ──

function SourceSelect({
  onSelect,
}: {
  onSelect: (slides: SlideData[], notes: Record<number, string>) => void
}) {
  const [slideSource, setSlideSource] = useState<'generated' | 'upload' | null>(null)
  const [noteSource, setNoteSource] = useState<'generated' | 'upload' | null>(null)
  const [jobs, setJobs] = useState<{ job_id: string; file_name: string; has_notes: boolean }[]>([])
  const [selectedJob, setSelectedJob] = useState('')
  const [uploadingSlides, setUploadingSlides] = useState(false)
  const [uploadingNotes, setUploadingNotes] = useState(false)
  const [slideFile, setSlideFile] = useState<File | null>(null)
  const [noteFile, setNoteFile] = useState<File | null>(null)
  const [ready, setReady] = useState(false)

  const selectedJobInfo = jobs.find(j => j.job_id === selectedJob)

  useEffect(() => {
    // Load history to get completed jobs with file names
    fetch('/api/history')
      .then(r => r.json())
      .then(data => {
        const completed: { job_id: string; file_name: string; has_notes: boolean }[] = []
        for (const item of data.items || []) {
          const fileName = item.session?.file_name || '未命名'
          for (const job of item.jobs || []) {
            if (job.status === 'complete') {
              completed.push({
                job_id: job.job_id,
                file_name: fileName,
                has_notes: !!job.output_path, // proxy: if output exists, notes likely exist
              })
            }
          }
        }
        setJobs(completed)
        if (completed.length > 0) setSelectedJob(completed[0].job_id)
      })
      .catch(() => {})
  }, [])

  useEffect(() => {
    setReady(
      ((slideSource === 'generated' && selectedJob) ||
      (slideSource === 'upload' && slideFile)) as boolean
    )
  }, [slideSource, selectedJob, slideFile])

  async function handleStart() {
    let slides: SlideData[] = []
    let notesMap: Record<number, string> = {}

    if (slideSource === 'generated' && selectedJob) {
      const preview = await getPreview(selectedJob)
      slides = preview.slides.map((s: PreviewSlide, i: number) => ({
        index: i + 1,
        content: s.content,
        notes: s.notes || '',
        type: 'svg' as const,
      }))
      notesMap = Object.fromEntries(slides.map(s => [s.index, s.notes]))
    } else if (slideSource === 'upload' && slideFile) {
      setUploadingSlides(true)
      try {
        const form = new FormData()
        form.append('file', slideFile)
        const res = await fetch('/api/presenter/upload-slides', { method: 'POST', body: form })
        const data = await res.json()
        slides = data.slides.map((s: any, i: number) => ({
          index: i + 1,
          content: s.url,
          notes: '',
          type: 'image' as const,
        }))
      } catch (e) {
        alert('幻灯片上传失败')
        setUploadingSlides(false)
        return
      }
      setUploadingSlides(false)
    }

    // Load notes
    if (noteSource === 'generated' && selectedJob) {
      const preview = await getPreview(selectedJob)
      notesMap = Object.fromEntries(
        preview.slides.map((s: PreviewSlide, i: number) => [i + 1, s.notes || ''])
      )
    } else if (noteSource === 'upload' && noteFile) {
      setUploadingNotes(true)
      try {
        const form = new FormData()
        form.append('file', noteFile)
        const res = await fetch('/api/presenter/upload-notes', { method: 'POST', body: form })
        const data = await res.json()
        for (const [page, text] of Object.entries(data.notes)) {
          notesMap[parseInt(page)] = text as string
        }
        // Merge notes into slides
        slides = slides.map(s => ({ ...s, notes: notesMap[s.index] || s.notes }))
      } catch (e) {
        alert('讲稿上传失败')
      }
      setUploadingNotes(false)
    } else if (noteSource === null && slideSource === 'generated' && selectedJob) {
      // Default: use generated notes
      const preview = await getPreview(selectedJob)
      notesMap = Object.fromEntries(
        preview.slides.map((s: PreviewSlide, i: number) => [i + 1, s.notes || ''])
      )
    }

    if (slides.length > 0) onSelect(slides, notesMap)
  }

  return (
    <div style={{ maxWidth: 720, margin: '0 auto', padding: '2rem' }}>
      <h2 style={{ fontSize: '1.5rem', fontWeight: 700, marginBottom: '0.5rem' }}>演讲者模式</h2>
      <p style={{ color: 'var(--muted)', marginBottom: '2rem' }}>选择幻灯片和讲稿来源，开始演讲练习。</p>

      {/* Slides Source */}
      <div className="panel" style={{ marginBottom: '1.5rem' }}>
        <div className="panel-header-row">
          <Presentation size={16} className="panel-title-icon" />
          <p className="panel-title">幻灯片来源</p>
        </div>
        <div style={{ display: 'flex', gap: '0.75rem', marginTop: '0.75rem' }}>
          <button
            className={`secondary-button ${slideSource === 'generated' ? 'primary-button' : ''}`}
            style={{ flex: 1 }}
            onClick={() => setSlideSource('generated')}
          >
            使用已生成的PPT
          </button>
          <button
            className={`secondary-button ${slideSource === 'upload' ? 'primary-button' : ''}`}
            style={{ flex: 1 }}
            onClick={() => setSlideSource('upload')}
          >
            <FileUp size={14} style={{ marginRight: 4, verticalAlign: -2 }} />
            上传PPT文件
          </button>
        </div>

        {slideSource === 'generated' && (
          <div style={{ marginTop: '1rem' }}>
            {jobs.length === 0 ? (
              <p style={{ color: 'var(--muted)', fontSize: '0.88rem' }}>暂无已完成的生成任务</p>
            ) : (
              <select
                value={selectedJob}
                onChange={(e) => setSelectedJob(e.target.value)}
                style={{ width: '100%', padding: '0.6rem 0.8rem', borderRadius: 12, border: '1px solid var(--line)', background: 'var(--surface-strong)', color: 'var(--text)' }}
              >
                {jobs.map(j => (
                  <option key={j.job_id} value={j.job_id}>
                    {j.file_name}
                  </option>
                ))}
              </select>
            )}
          </div>
        )}

        {slideSource === 'upload' && (
          <div style={{ marginTop: '1rem' }}>
            <label className="upload-zone" style={{ minHeight: 80, cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8 }}>
              <Upload size={20} />
              <span>{slideFile ? slideFile.name : '选择 PPT / PDF 文件'}</span>
              <input type="file" accept=".pdf,.pptx" hidden onChange={(e) => { const f = e.target.files?.[0]; if (f) setSlideFile(f) }} />
            </label>
          </div>
        )}
      </div>

      {/* Notes Source */}
      <div className="panel" style={{ marginBottom: '1.5rem' }}>
        <div className="panel-header-row">
          <BookOpen size={16} className="panel-title-icon" />
          <p className="panel-title">逐字稿来源</p>
        </div>
        <div style={{ display: 'flex', gap: '0.75rem', marginTop: '0.75rem' }}>
          {slideSource === 'generated' && (
            <button
              className={`secondary-button ${noteSource === 'generated' ? 'primary-button' : ''}`}
              style={{ flex: 1 }}
              onClick={() => setNoteSource('generated')}
            >
              使用已生成的讲稿
            </button>
          )}
          <button
            className={`secondary-button ${noteSource === 'upload' ? 'primary-button' : ''}`}
            style={{ flex: 1 }}
            onClick={() => setNoteSource('upload')}
          >
            <FileUp size={14} style={{ marginRight: 4, verticalAlign: -2 }} />
            上传讲稿文件
          </button>
        </div>

        {noteSource === 'generated' && slideSource === 'generated' && (
          <div style={{ marginTop: '0.75rem', color: 'var(--muted)', fontSize: '0.82rem' }}>
            将使用「{selectedJobInfo?.file_name || ''}」对应的演讲讲稿
          </div>
        )}

        {noteSource === 'upload' && (
          <div style={{ marginTop: '1rem' }}>
            <label className="upload-zone" style={{ minHeight: 80, cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8 }}>
              <Upload size={20} />
              <span>{noteFile ? noteFile.name : '选择 txt / docx 文件'}</span>
              <input type="file" accept=".txt,.docx,.md" hidden onChange={(e) => { const f = e.target.files?.[0]; if (f) setNoteFile(f) }} />
            </label>
          </div>
        )}
      </div>

      {/* Start Button */}
      <button
        className="primary-button full-width"
        disabled={!ready || uploadingSlides || uploadingNotes}
        onClick={handleStart}
      >
        {(uploadingSlides || uploadingNotes) ? '处理中...' : '开始演讲'}
      </button>
    </div>
  )
}

// ── Presenter View ──

function PresenterView({
  slides,
  notesMap,
  onExit,
}: {
  slides: SlideData[]
  notesMap: Record<number, string>
  onExit: () => void
}) {
  const [current, setCurrent] = useState(0)
  const [elapsed, setElapsed] = useState(0)
  const [paused, setPaused] = useState(false)
  const [castWin, setCastWin] = useState<Window | null>(null)
  const timerRef = useRef<ReturnType<typeof setInterval>>()
  const notesScrollRef = useRef<HTMLDivElement>(null)
  const nextNotesRef = useRef<HTMLDivElement>(null)
  const autoAdvanceLock = useRef(false)

  const isCasting = castWin && !castWin.closed

  const total = slides.length
  const currentSlide = slides[current]
  const nextSlide = current < total - 1 ? slides[current + 1] : null
  const currentNotes = notesMap[current + 1] || currentSlide?.notes || ''
  const nextNotes = nextSlide ? (notesMap[current + 2] || nextSlide.notes || '') : ''

  const goNext = useCallback(() => setCurrent(c => Math.min(c + 1, total - 1)), [total])
  const goPrev = useCallback(() => setCurrent(c => Math.max(c - 1, 0)), [])

  // Auto-advance: when user scrolls to fully reveal next page's notes, switch slide
  useEffect(() => {
    const container = notesScrollRef.current
    const target = nextNotesRef.current
    if (!container || !target) return

    const observer = new IntersectionObserver(
      ([entry]) => {
        // Only trigger when fully visible and not already advancing
        if (entry.intersectionRatio >= 0.95 && !autoAdvanceLock.current) {
          autoAdvanceLock.current = true
          goNext()
          // Reset scroll to top after advancing
          requestAnimationFrame(() => {
            container.scrollTo({ top: 0, behavior: 'smooth' })
            // Release lock after a short delay to prevent re-trigger
            setTimeout(() => { autoAdvanceLock.current = false }, 500)
          })
        }
      },
      { root: container, threshold: 0.95 }
    )
    observer.observe(target)
    return () => observer.disconnect()
  }, [current, goNext, nextNotes])

  // Timer
  useEffect(() => {
    if (!paused) {
      timerRef.current = setInterval(() => setElapsed(e => e + 1), 1000)
    }
    return () => { if (timerRef.current) clearInterval(timerRef.current) }
  }, [paused])

  // Sync cast window content when slide changes
  useEffect(() => {
    if (!castWin || castWin.closed) return
    const container = castWin.document.getElementById('cast-slide')
    if (!container) return
    if (currentSlide?.type === 'svg') {
      container.innerHTML = currentSlide.content
      const svg = container.querySelector('svg')
      if (svg) { svg.setAttribute('width', '100%'); svg.setAttribute('height', '100%') }
    } else {
      container.innerHTML = `<img src="${currentSlide?.content}" style="width:100%;height:100%;object-fit:contain" />`
    }
  }, [current, castWin, currentSlide])

  // Clean up cast window on unmount
  useEffect(() => {
    return () => { if (castWin && !castWin.closed) castWin.close() }
  }, [castWin])

  function startCast() {
    const w = window.open('', 'presenter-cast', 'width=1280,height=720,popup=yes')
    if (!w) { alert('无法打开投屏窗口，请允许弹出窗口'); return }
    w.document.write(`<!DOCTYPE html><html><head><title>投屏 - 幻灯片</title>
<style>*{margin:0;padding:0;box-sizing:border-box}html,body{width:100%;height:100%;overflow:hidden;background:#000;display:flex;align-items:center;justify-content:center}
#cast-slide{width:100%;height:100%;display:flex;align-items:center;justify-content:center;background:#fff}
#cast-slide svg{width:100%;height:100%}</style></head>
<body><div id="cast-slide"></div></body></html>`)
    w.document.close()
    setCastWin(w)
  }

  function stopCast() {
    if (castWin && !castWin.closed) castWin.close()
    setCastWin(null)
  }

  // Keyboard shortcuts
  useEffect(() => {
    function handleKey(e: KeyboardEvent) {
      if (e.key === 'ArrowRight' || e.key === ' ') { e.preventDefault(); goNext() }
      else if (e.key === 'ArrowLeft') { e.preventDefault(); goPrev() }
      else if (e.key === 'Escape') { e.preventDefault(); onExit() }
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [goNext, goPrev, onExit])

  const formatTime = (sec: number) => {
    const m = Math.floor(sec / 60)
    const s = sec % 60
    return `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`
  }

  return (
    <div className="presenter-layout">
      {/* Main slide area */}
      <div className="presenter-main">
        <div className="presenter-slide-frame">
          {currentSlide?.type === 'svg' ? (
            <div className="presenter-slide-svg" dangerouslySetInnerHTML={{ __html: currentSlide.content }} />
          ) : (
            <img src={currentSlide?.content} alt={`Slide ${current + 1}`} className="presenter-slide-img" />
          )}
        </div>

        {/* Thumbnail navigation */}
        <div className="presenter-thumbs">
          <button className="presenter-nav-btn" onClick={goPrev} disabled={current === 0}>
            <ChevronLeft size={18} />
          </button>
          <div className="presenter-thumbs-scroll">
            {slides.map((s, i) => (
              <div
                key={i}
                className={`presenter-thumb ${i === current ? 'presenter-thumb-active' : ''}`}
                onClick={() => setCurrent(i)}
              >
                {s.type === 'svg' ? (
                  <div dangerouslySetInnerHTML={{ __html: s.content }} />
                ) : (
                  <img src={s.content} alt={`Thumb ${i + 1}`} />
                )}
                <span className="presenter-thumb-num">{i + 1}</span>
              </div>
            ))}
          </div>
          <button className="presenter-nav-btn" onClick={goNext} disabled={current === total - 1}>
            <ChevronRight size={18} />
          </button>
        </div>
      </div>

      {/* Right panel: next preview + notes + timer */}
      <div className="presenter-right">
        {/* Next page preview */}
        {nextSlide && (
          <div className="presenter-next-section">
            <div className="presenter-section-header">
              <Eye size={14} />
              <span>下一页预览</span>
            </div>
            <div className="presenter-next-frame">
              {nextSlide.type === 'svg' ? (
                <div dangerouslySetInnerHTML={{ __html: nextSlide.content }} />
              ) : (
                <img src={nextSlide.content} alt="Next slide" />
              )}
            </div>
          </div>
        )}

        {/* Speaker notes (verbatim script) */}
        <div className="presenter-notes-section">
          <div className="presenter-section-header">
            <BookOpen size={14} />
            <span>演讲逐字稿</span>
          </div>
          <div className="presenter-notes-content" ref={notesScrollRef}>
            <div className="presenter-notes-current">
              <div className="presenter-notes-label">第 {current + 1} 页</div>
              <div className="presenter-notes-text"><HighlightedNotes text={currentNotes || '（无逐字稿）'} /></div>
            </div>
            {nextSlide && nextNotes && (
              <div className="presenter-notes-next" ref={nextNotesRef}>
                <div className="presenter-notes-label">第 {current + 2} 页（下一页）</div>
                <div className="presenter-notes-text" style={{ opacity: 0.6 }}><HighlightedNotes text={nextNotes} /></div>
              </div>
            )}
          </div>
        </div>

        {/* Timer */}
        <div className="presenter-timer-section">
          <div className="presenter-section-header">
            <Clock size={14} />
            <span>计时器</span>
          </div>
          <div className="presenter-timer-display">
            <span className="presenter-timer-value">{formatTime(elapsed)}</span>
            <span className="presenter-timer-page">{current + 1} / {total}</span>
          </div>
          <div style={{ display: 'flex', gap: 6, marginTop: 6 }}>
            <button className="secondary-button" style={{ flex: 1, fontSize: '0.78rem' }} onClick={() => setPaused(p => !p)}>
              {paused ? '继续' : '暂停'}
            </button>
            <button className="secondary-button" style={{ flex: 1, fontSize: '0.78rem' }} onClick={() => { setElapsed(0); setPaused(false) }}>
              重置
            </button>
          </div>
        </div>

        {/* Cast window for screen sharing */}
        <button
          className={isCasting ? 'primary-button' : 'secondary-button'}
          style={{ width: '100%', marginTop: '0.5rem' }}
          onClick={isCasting ? stopCast : startCast}
        >
          <Monitor size={14} style={{ marginRight: 4, verticalAlign: -2 }} />
          {isCasting ? '停止投屏' : '投屏（共享窗口）'}
        </button>

        {/* Exit */}
        <button className="secondary-button full-width" style={{ marginTop: '0.5rem' }} onClick={onExit}>
          <X size={14} style={{ marginRight: 4, verticalAlign: -2 }} />
          退出演讲者模式
        </button>
      </div>
    </div>
  )
}

// ── Main Page ──

export default function PresenterPage() {
  const [slides, setSlides] = useState<SlideData[] | null>(null)
  const [notesMap, setNotesMap] = useState<Record<number, string>>({})

  if (!slides) {
    return (
      <SourceSelect
        onSelect={(s, n) => { setSlides(s); setNotesMap(n) }}
      />
    )
  }

  return (
    <PresenterView
      slides={slides}
      notesMap={notesMap}
      onExit={() => { setSlides(null); setNotesMap({}) }}
    />
  )
}
