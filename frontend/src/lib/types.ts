export interface ProviderModel {
  id: string
  display_name: string
  supports_vision: boolean
}

export interface ProviderListItem {
  name: string
  display_name: string
  default_base_url: string | null
  models: ProviderModel[]
}

export interface ModelConfig {
  provider: string
  model: string
  api_key: string
  base_url?: string | null
}

export interface GenerationOptions {
  canvas_format: string
  style: string
  num_pages?: number | null
  language: string
  detail_level: string
  timeout_seconds?: number | null
  mode: string
  speech_minutes?: number | null
  theme_color?: string | null
  style_overrides?: { density?: string; palette?: string[]; font?: string } | null
}

export interface GenerateRequest {
  session_id: string
  instruction: string
  model_config: ModelConfig
  options: GenerationOptions
}

export interface JobStatus {
  status: string
  progress: number
  message: string
  slides_completed: number
  total_slides: number
  output_path?: string | null
  error?: string | null
  data?: Record<string, unknown> | null
}

export interface NoteSource {
  text: string
  source?: string | null
}

export interface PreviewSlide {
  index: number
  name: string
  source: string
  content: string
  notes?: string | null
  original?: string | null
  notes_sources?: NoteSource[] | null
}

export interface WSEvent {
  type: string
  job_id: string
  stage: string
  status: string
  message: string
  progress: number
  slides_completed: number
  total_slides: number
  seq?: number
  ts?: number
  data?: Record<string, unknown>
}
