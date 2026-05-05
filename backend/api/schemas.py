"""Pydantic request/response models for the API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class FileInfo(BaseModel):
    name: str
    size: int
    source_type: str  # "pdf"


class UploadResponse(BaseModel):
    session_id: str
    file_info: FileInfo


class ModelConfig(BaseModel):
    provider: str  # "openai", "deepseek", "doubao"
    model: str
    api_key: str
    base_url: str | None = None


class StyleOverrides(BaseModel):
    palette: list[str] | None = None
    font: str | None = None
    density: str | None = None  # "compact" | "normal" | "spacious"


class GenerationOptions(BaseModel):
    canvas_format: str = "ppt169"
    style: str = "academic"
    num_pages: int | None = None
    language: str = "zh"
    detail_level: str = "normal"
    timeout_seconds: int | None = Field(default=None, ge=1)
    style_overrides: StyleOverrides | None = None
    enable_visual_critic: bool = False
    mode: str = "fancy"  # "quick" or "fancy"
    speech_minutes: int | None = None  # Target speech duration in minutes (controls notes length)
    theme_color: str | None = None     # Primary theme color hex (e.g. "#1A365D")


class GenerateRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    session_id: str
    instruction: str = ""
    model_settings: ModelConfig = Field(alias="model_config")
    options: GenerationOptions = Field(default_factory=GenerationOptions)


class GenerateResponse(BaseModel):
    job_id: str
    status: str = "started"


class JobStatus(BaseModel):
    status: str
    progress: float = 0.0
    message: str = ""
    slides_completed: int = 0
    total_slides: int = 0
    output_path: str | None = None
    error: str | None = None
    data: dict | None = None


class CancelJobResponse(BaseModel):
    job_id: str
    status: str


class OutlineRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    session_id: str
    model_settings: ModelConfig = Field(alias="model_config")


class OutlineResponse(BaseModel):
    reading_matrix: dict
    slides: list[dict]


class RetryRequest(BaseModel):
    """Retry a failed job from the strategy stage."""

    job_id: str
    model_settings: ModelConfig | None = Field(default=None, alias="model_config")


class ContinueRequest(BaseModel):
    """Resume a paused pipeline after user confirms/edits the outline."""

    job_id: str
    manuscript: str
    model_settings: ModelConfig | None = None


class RefineRequest(BaseModel):
    """Request to iterate on an existing generation using user feedback."""

    model_config = ConfigDict(populate_by_name=True)

    job_id: str
    feedback: str
    model_settings: ModelConfig | None = Field(default=None, alias="model_config")
    options: GenerationOptions = Field(default_factory=GenerationOptions)
    target_pages: list[int] = Field(default_factory=list)
    allow_structure_changes: bool = False


class RefineResponse(BaseModel):
    job_id: str
    status: str = "started"


class PreviewSlide(BaseModel):
    index: int
    name: str
    source: str  # "output" or "final"
    content: str
    notes: str | None = None
    original: str | None = None
    notes_sources: list[dict] | None = None  # [{"text": "...", "source": "..."}]


class PreviewResponse(BaseModel):
    job_id: str
    project_dir: str | None = None
    slides: list[PreviewSlide] = Field(default_factory=list)
    output_path: str | None = None
    status: str


class ReexportResponse(BaseModel):
    job_id: str
    status: str
    output_path: str


class RegenerateNotesRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    model_settings: ModelConfig = Field(alias="model_config")
    language: str = "zh"
    speech_minutes: int | None = None


class RegenerateNotesResponse(BaseModel):
    job_id: str
    notes: dict[str, str]
    notes_sources: dict[str, list[dict]] = {}


class ProviderModel(BaseModel):
    id: str
    display_name: str
    supports_vision: bool = False


class ProviderListItem(BaseModel):
    name: str
    display_name: str
    default_base_url: str | None = None
    models: list[ProviderModel]


class ProvidersResponse(BaseModel):
    providers: list[ProviderListItem]


class ExtractResponse(BaseModel):
    session_id: str
    total_pages: int
    figures_count: int
    equations_count: int
    text_length: int
    outline: list[dict] = Field(default_factory=list)
    pages: list[dict] = Field(default_factory=list)
    visual_assets: list[dict] = Field(default_factory=list)
