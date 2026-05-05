import { create } from 'zustand'
import { uploadPDF, extractPDF, generatePresentation, getJobStatus, getPreview, refinePresentation, continueGeneration, cancelJob } from '../lib/api'
import { WSClient } from '../lib/ws'
import type { JobStatus, PreviewSlide, WSEvent, ModelConfig, GenerationOptions } from '../lib/types'

const LS_JOB_KEY = 'gen_activeJobId'
const LS_SESSION_KEY = 'gen_activeSessionId'

function _lsGet(key: string): string | null {
  try { return localStorage.getItem(key) } catch { return null }
}
function _lsSet(key: string, val: string | null) {
  try { if (val) localStorage.setItem(key, val); else localStorage.removeItem(key) } catch { /* */ }
}

/** Start polling a job, returning the generation counter used. */
function _startPolling(jobId: string, gen: number, set: (p: Partial<GenerationState>) => void, get: () => GenerationState) {
  const poll = async () => {
    if (get()._pollGen !== gen) return
    try {
      const s = await getJobStatus(jobId)
      if (get()._pollGen !== gen) return
      set({ status: s })
      if (s.status === 'awaiting_confirmation') {
        // Stopped for user confirmation
        set({ loading: false })
        return
      }
      if (s.status !== 'complete' && s.status !== 'error' && s.status !== 'cancelled') {
        setTimeout(poll, 3000)
      } else if (s.status === 'complete') {
        const p = await getPreview(jobId)
        set({ slides: p.slides, loading: false })
        _lsSet(LS_JOB_KEY, null) // done — stop persisting
      } else {
        set({ loading: false })
        _lsSet(LS_JOB_KEY, null)
      }
    } catch {
      if (get()._pollGen === gen) setTimeout(poll, 3000)
    }
  }
  poll()
}

interface GenerationState {
  sessionId: string | null
  jobId: string | null
  status: JobStatus | null
  slides: PreviewSlide[]
  loading: boolean
  error: string | null
  wsClient: WSClient | null
  _pollGen: number

  uploadAndExtract: (file: File) => Promise<void>
  startGeneration: (modelConfig: ModelConfig, options: GenerationOptions, instruction?: string) => Promise<void>
  submitFeedback: (feedback: string) => Promise<void>
  continueWithOutline: (manuscript: string, modelConfig?: Record<string, unknown>) => Promise<void>
  cancelGeneration: () => Promise<void>
  refreshPreview: () => Promise<void>
  reset: () => void
}

// Recover persisted jobId on store creation
const _savedJobId = _lsGet(LS_JOB_KEY)
const _savedSessionId = _lsGet(LS_SESSION_KEY)

export const useGeneration = create<GenerationState>((set, get) => ({
  sessionId: _savedSessionId,
  jobId: _savedJobId,
  status: null,
  slides: [],
  loading: !!_savedJobId,
  error: null,
  wsClient: null,
  _pollGen: _savedJobId ? 1 : 0,

  // Auto-resume polling for persisted job on first tick
  ...( _savedJobId ? (() => {
    // Kick off initial poll immediately
    const gen = 1
    setTimeout(() => _startPolling(_savedJobId, gen, set as (p: Partial<GenerationState>) => void, get as () => GenerationState), 500)
    return {}
  })() : {} ),

  uploadAndExtract: async (file: File) => {
    set({ loading: true, error: null })
    try {
      const res = await uploadPDF(file)
      const sid = res.session_id
      set({ sessionId: sid })
      _lsSet(LS_SESSION_KEY, sid)
      await extractPDF(sid)
    } catch (e: any) {
      set({ error: e.message })
      throw e
    } finally {
      set({ loading: false })
    }
  },

  startGeneration: async (modelConfig, options, instruction) => {
    const { sessionId } = get()
    if (!sessionId) return

    // Stop previous polling/WebSocket before starting a new job
    const prev = get()
    prev.wsClient?.disconnect()
    const gen = (prev._pollGen || 0) + 1
    set({ loading: true, error: null, _pollGen: gen, wsClient: null })

    try {
      const res = await generatePresentation({
        session_id: sessionId,
        instruction: instruction || '',
        model_config: modelConfig as unknown as Record<string, unknown>,
        options: options as unknown as Record<string, unknown>,
      })
      const jobId = res.job_id
      set({ jobId })
      _lsSet(LS_JOB_KEY, jobId)

      // Connect WebSocket
      const ws = new WSClient(jobId, (event: WSEvent) => {
        set(state => ({
          status: state.status ? {
            ...state.status,
            status: event.stage,
            progress: event.progress,
            message: event.message,
            slides_completed: event.slides_completed,
            total_slides: event.total_slides,
          } : state.status,
        }))
      })
      ws.connect()
      set({ wsClient: ws })

      _startPolling(jobId, gen, set as (p: Partial<GenerationState>) => void, get as () => GenerationState)
    } catch (e: any) {
      set({ error: e.message, loading: false })
    }
  },

  submitFeedback: async (feedback: string) => {
    const { jobId } = get()
    if (!jobId) return

    // Stop previous polling before starting refine
    const prev = get()
    prev.wsClient?.disconnect()
    const gen = (prev._pollGen || 0) + 1
    set({ loading: true, error: null, _pollGen: gen, wsClient: null })

    try {
      // Include model config from localStorage for API key
      const _load = (key: string, fb = ''): string => { try { return localStorage.getItem(key) || fb } catch { return fb } }
      const provider = _load('gen_provider')
      const model = _load(`gen_model_${provider}`)
      const api_key = _load('gen_apiKey').trim()
      const base_url = _load(`gen_baseUrl_${provider}`) || null
      const model_config = (provider && model && api_key)
        ? { provider, model, api_key, base_url }
        : undefined
      const res = await refinePresentation(jobId, { feedback, model_config })
      const newJobId = res.job_id
      set({ jobId: newJobId, slides: [], loading: true })
      _lsSet(LS_JOB_KEY, newJobId)

      _startPolling(newJobId, gen, set as (p: Partial<GenerationState>) => void, get as () => GenerationState)
    } catch (e: any) {
      set({ error: e.message, loading: false })
    }
  },

  continueWithOutline: async (manuscript: string, modelConfig?: Record<string, unknown>) => {
    const { jobId } = get()
    if (!jobId) return

    // Increment generation counter so any stale polling stops
    const gen = (get()._pollGen || 0) + 1
    set({ loading: true, error: null, _pollGen: gen })

    try {
      await continueGeneration(jobId, {
        manuscript,
        model_settings: modelConfig,
      })

      // Restart polling for the same jobId after continuation
      _startPolling(jobId, gen, set as (p: Partial<GenerationState>) => void, get as () => GenerationState)
    } catch (e: any) {
      set({ error: e.message, loading: false })
    }
  },

  cancelGeneration: async () => {
    const { jobId } = get()
    if (!jobId) return
    try {
      await cancelJob(jobId)
      // Immediately refresh status so UI updates
      const s = await getJobStatus(jobId)
      if (s) set({ status: s })
    } catch {
      // Even if cancel API fails, try refreshing status
      try {
        const s = await getJobStatus(jobId)
        if (s) set({ status: s })
      } catch { /* ignore */ }
    }
  },

  refreshPreview: async () => {
    const { jobId } = get()
    if (!jobId) return
    try {
      const p = await getPreview(jobId)
      set({ slides: p.slides })
    } catch { /* ignore */ }
  },

  reset: () => {
    const { wsClient } = get()
    wsClient?.disconnect()
    _lsSet(LS_JOB_KEY, null)
    _lsSet(LS_SESSION_KEY, null)
    set({
      sessionId: null, jobId: null, status: null,
      slides: [], loading: false, error: null, wsClient: null,
      _pollGen: 0,
    })
  },
}))
