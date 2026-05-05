"""Generation API endpoint."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, status


def _accent_from_primary(hex_color: str) -> str:
    """Generate a lighter accent color from a dark primary hex color."""
    hex_color = hex_color.lstrip("#")
    if len(hex_color) != 6:
        return "#4A90D9"
    r, g, b = int(hex_color[:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    # Shift hue towards blue and lighten
    r = min(255, r + 60)
    g = min(255, g + 40)
    b = min(255, b + 80)
    return f"#{r:02X}{g:02X}{b:02X}"

from backend.api.schemas import GenerateRequest, GenerateResponse
from backend.config import settings
from backend.session.manager import session_manager
from backend.session.progress import payloads_from_progress_event

router = APIRouter()


async def _iterate_pipeline(job_id: str, request: Any) -> None:
    from backend.orchestrator.pipeline import run_pipeline

    async for event in run_pipeline(request):
        current_job = session_manager.get_job(job_id)
        if current_job is None:
            return
        for payload, updates in payloads_from_progress_event(job_id, current_job, event):
            session_manager.record_event(job_id, payload, **updates)


async def _run_generation_job(job_id: str, request: Any) -> None:
    job = session_manager.get_job(job_id)
    if job is None:
        return

    timeout = getattr(request, "timeout_seconds", None)
    try:
        if timeout and timeout > 0:
            await asyncio.wait_for(_iterate_pipeline(job_id, request), timeout=timeout)
        else:
            await _iterate_pipeline(job_id, request)
    except asyncio.TimeoutError:
        session_manager.update_job(
            job_id,
            status="error",
            error=f"Job exceeded timeout of {timeout}s",
        )
    except asyncio.CancelledError:
        session_manager.mark_job_cancelled(job_id)
        raise
    except Exception as exc:
        session_manager.update_job(
            job_id,
            status="error",
            error=str(exc),
            message=str(exc),
        )


@router.post("/generate", response_model=GenerateResponse)
async def generate_presentation(request: GenerateRequest) -> GenerateResponse:
    from backend.orchestrator.pipeline import GenerationRequest

    session = session_manager.get_session(request.session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found.",
        )

    job = session_manager.create_job(request.session_id)
    session_manager.update_job(
        job.id,
        status="pending",
        message="Queued for generation",
        provider=request.model_settings.provider,
        model_name=request.model_settings.model,
        base_url=request.model_settings.base_url,
        canvas_format=request.options.canvas_format,
        style=request.options.style,
        language=request.options.language,
        detail_level=request.options.detail_level,
        instruction=request.instruction,
    )

    # Build style_overrides: merge density + theme_color
    style_overrides_dict = (
        request.options.style_overrides.model_dump(exclude_none=True)
        if request.options.style_overrides
        else {}
    )
    if request.options.theme_color:
        style_overrides_dict["palette"] = [
            request.options.theme_color,
            _accent_from_primary(request.options.theme_color),
        ]

    pipeline_request = GenerationRequest(
        file_path=session.file_path,
        source_type="pdf",
        provider=request.model_settings.provider,
        model=request.model_settings.model,
        api_key=request.model_settings.api_key,
        base_url=request.model_settings.base_url,
        canvas_format=request.options.canvas_format,
        style=request.options.style,
        num_pages=request.options.num_pages,
        instruction=request.instruction,
        language=request.options.language,
        detail_level=request.options.detail_level,
        timeout_seconds=request.options.timeout_seconds,
        style_overrides=style_overrides_dict or None,
        enable_visual_critic=request.options.enable_visual_critic,
        confirm_outline=True,
        quick_mode=request.options.mode == "quick",
        speech_minutes=request.options.speech_minutes,
        theme_color=request.options.theme_color,
    )

    task = asyncio.create_task(_run_generation_job(job.id, pipeline_request))
    session_manager.register_task(job.id, task)

    return GenerateResponse(job_id=job.id, status="started")
