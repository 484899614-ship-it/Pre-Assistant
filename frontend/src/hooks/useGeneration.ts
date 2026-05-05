import { create } from 'zustand'
import { uploadPDF, extractPDF, generatePresentation, getJobStatus, getPreview, refinePresentation, continueGeneration, cancelJob } from '../lib/api'
import { WSClient } from '../lib/ws'
import type { JobStatus, PreviewSlide, WSEvent, ModelConfig, GenerationOptions } from '../lib/types'

interface GenerationState {
  sessionId: string | null
  jobId: string | null
  status: JobStatus | null
  slides: PreviewSlide[]
  loading: boolean
  error: string | null
  wsClient: WSClient | null

  uploadAndExtract: (file: File) => Promise<void>
  startGeneration: (modelConfig: ModelConfig, options: GenerationOptions, instruction?: string) => Promise<void>
  submitFeedback: (feedback: string) => Promise<void>
  continueWithOutline: (manuscript: string, modelConfig?: Record<string, unknown>) => Promise<void>
  cancelGeneration: () => Promise<void>
  refreshPreview: () => Promise<void>
  reset: () => void
}

export const useGeneration = create<GenerationState>((set, get) => ({
  sessionId: null,
  jobId: null,
  status: null,
  slides: [],
  loading: false,
  error: null,
  wsClient: null,

  uploadAndExtract: async (file: File) => {
    set({ loading: true, error: null })
    try {
      const res = await uploadPDF(file)
      set({ sessionId: res.session_id })
      await extractPDF(res.session_id)
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
    set({ loading: true, error: null })
    try {
      const res = await generatePresentation({
        session_id: sessionId,
        instruction: instruction || '',
        model_config: modelConfig as unknown as Record<string, unknown>,
        options: options as unknown as Record<string, unknown>,
      })
      const jobId = res.job_id
      set({ jobId })

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

      // Start polling as fallback
      const poll = async () => {
        try {
          const s = await getJobStatus(jobId)
          set({ status: s })
          if (s.status !== 'complete' && s.status !== 'error' && s.status !== 'cancelled') {
            setTimeout(poll, 3000)
          } else if (s.status === 'complete') {
            const p = await getPreview(jobId)
            set({ slides: p.slides, loading: false })
          }
        } catch {
          setTimeout(poll, 3000)
        }
      }
      poll()
    } catch (e: any) {
      set({ error: e.message, loading: false })
    }
  },

  submitFeedback: async (feedback: string) => {
    const { jobId } = get()
    if (!jobId) return
    set({ loading: true, error: null })
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
      set({ jobId: res.job_id, slides: [], loading: true })
    } catch (e: any) {
      set({ error: e.message, loading: false })
    }
  },

  continueWithOutline: async (manuscript: string, modelConfig?: Record<string, unknown>) => {
    const { jobId } = get()
    if (!jobId) return
    set({ loading: true, error: null })
    try {
      await continueGeneration(jobId, {
        manuscript,
        model_settings: modelConfig,
      })
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
    set({
      sessionId: null, jobId: null, status: null,
      slides: [], loading: false, error: null, wsClient: null,
    })
  },
}))
