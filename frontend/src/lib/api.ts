const API_BASE = '/api'

async function fetchJSON<T>(url: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${url}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    const detail = body.detail
    let msg = `HTTP ${res.status}`
    if (typeof detail === 'string') msg = detail
    else if (Array.isArray(detail)) msg = detail.map((d: any) => d.msg || JSON.stringify(d)).join('; ')
    else if (detail) msg = JSON.stringify(detail)
    throw new Error(msg)
  }
  return res.json()
}

interface UploadResponse {
  session_id: string
  file_info?: { name: string; size: number; source_type: string }
  filename?: string
}

export async function uploadPDF(file: File): Promise<UploadResponse> {
  const form = new FormData()
  form.append('file', file)
  const res = await fetch(`${API_BASE}/upload`, { method: 'POST', body: form })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    const detail = (body as any).detail
    throw new Error(typeof detail === 'string' ? detail : `Upload failed (HTTP ${res.status})`)
  }
  return res.json()
}

export async function extractPDF(sessionId: string) {
  return fetchJSON(`/extract?session_id=${encodeURIComponent(sessionId)}`, {
    method: 'POST',
  })
}

interface GenerateResponse {
  job_id: string
}

export async function generatePresentation(request: {
  session_id: string
  instruction: string
  model_config: Record<string, unknown>
  options: Record<string, unknown>
}): Promise<GenerateResponse> {
  return fetchJSON('/generate', {
    method: 'POST',
    body: JSON.stringify(request),
  })
}

export async function getJobStatus(jobId: string) {
  return fetchJSON<import('./types').JobStatus>(`/status/${jobId}`)
}

export async function getProviders() {
  return fetchJSON<{ providers: import('./types').ProviderListItem[] }>('/providers')
}

export async function getPreview(jobId: string) {
  return fetchJSON<{ job_id: string; slides: import('./types').PreviewSlide[]; output_path?: string }>(`/preview/${jobId}`)
}

interface RefineResponse {
  job_id: string
}

interface ModelConfigPayload {
  provider: string
  model: string
  api_key: string
  base_url?: string | null
}

export async function refinePresentation(
  jobId: string,
  request: { feedback: string; model_config?: ModelConfigPayload },
): Promise<RefineResponse> {
  return fetchJSON('/refine', {
    method: 'POST',
    body: JSON.stringify({ job_id: jobId, ...request }),
  })
}

export async function continueGeneration(jobId: string, request: { manuscript: string; model_settings?: Record<string, unknown> | undefined }): Promise<GenerateResponse> {
  return fetchJSON('/generate/continue', {
    method: 'POST',
    body: JSON.stringify({ job_id: jobId, ...request }),
  })
}

export async function retryGeneration(jobId: string, modelConfig?: Record<string, unknown>): Promise<GenerateResponse> {
  const body: Record<string, unknown> = { job_id: jobId }
  if (modelConfig) body.model_config = modelConfig
  return fetchJSON('/generate/retry', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export function downloadURL(jobId: string) {
  return `${API_BASE}/download/${jobId}`
}

export interface HistorySession {
  session_id: string
  file_name: string
  file_size: number
  source_type: string
  title: string | null
  authors: string | null
}

export interface HistoryJob {
  job_id: string
  session_id: string
  status: string
  progress: number
  provider: string | null
  model_name: string | null
  canvas_format: string | null
  style: string | null
  language: string | null
  output_path: string | null
  error: string | null
  total_slides: number
  slides_completed: number
}

export interface HistoryItem {
  session: HistorySession
  jobs: HistoryJob[]
}

export async function getHistory(): Promise<{ items: HistoryItem[] }> {
  return fetchJSON('/history')
}

export async function deleteSession(sessionId: string) {
  const res = await fetch(`${API_BASE}/session/${sessionId}`, { method: 'DELETE' })
  if (!res.ok && res.status !== 204) throw new Error('Delete failed')
}

export async function cancelJob(jobId: string) {
  const res = await fetch(`${API_BASE}/status/${jobId}/cancel`, { method: 'POST' })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.detail || `HTTP ${res.status}`)
  }
  return res.json()
}
