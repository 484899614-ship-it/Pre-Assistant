"""Refine endpoint for feedback-driven iteration."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException, status

from backend.api.schemas import ContinueRequest, RefineRequest, RefineResponse, RetryRequest
from backend.config import settings
from backend.orchestrator.pipeline import RefineRequest as PipelineRefineRequest
from backend.session.manager import session_manager

router = APIRouter()


@router.post("/refine", response_model=RefineResponse)
async def refine_presentation(request: RefineRequest) -> RefineResponse:
    """Iterate on an existing generation using user feedback."""
    job = session_manager.get_job(request.job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    if not job.project_dir:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Job has no project workspace.")

    # Use provided model_settings or fall back to original job's config
    mc = request.model_settings
    provider = mc.provider if mc else (job.provider or "doubao")
    model = mc.model if mc else (job.model_name or "doubao-seed-2-0-lite-260215")
    api_key = mc.api_key if mc else ""
    base_url = mc.base_url if mc else job.base_url

    refine_job = session_manager.create_refine_job(
        request.job_id,
        request.feedback,
        project_dir=job.project_dir,
    )
    if refine_job is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Cannot create refine job.")

    pipeline_request = PipelineRefineRequest(
        project_dir=Path(job.project_dir),
        feedback=request.feedback,
        feedback_history=refine_job.feedback_history,
        job_id=refine_job.id,
        parent_job_id=request.job_id,
        provider=provider,
        model=model,
        api_key=api_key,
        base_url=base_url,
        canvas_format=request.options.canvas_format or job.canvas_format or "ppt169",
        style=request.options.style or job.style or "academic",
        language=request.options.language or job.language or "zh",
        detail_level=request.options.detail_level or job.detail_level or "normal",
        target_pages=request.target_pages,
        allow_structure_changes=request.allow_structure_changes,
    )

    session_manager.update_job(
        refine_job.id,
        status="pending",
        message="Refinement queued",
    )

    task = asyncio.create_task(_run_refine_job(refine_job.id, pipeline_request))
    session_manager.register_task(refine_job.id, task)

    return RefineResponse(job_id=refine_job.id, status="started")


@router.post("/generate/continue", response_model=RefineResponse)
async def continue_generation(request: ContinueRequest) -> RefineResponse:
    """Resume a paused pipeline after user confirms/edits the outline."""
    job = session_manager.get_job(request.job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")

    # Use provided model_settings or fall back to job's stored config
    mc = request.model_settings
    provider = mc.provider if mc else (job.provider or "doubao")
    model = mc.model if mc else (job.model_name or "doubao-seed-2-0-lite-260215")
    api_key = mc.api_key if mc else ""
    base_url = mc.base_url if mc else job.base_url

    session_manager.update_job(
        request.job_id,
        status="research",
        message="Continuing with confirmed outline...",
    )

    from backend.orchestrator.pipeline import GenerationRequest, continue_pipeline

    pipeline_request = GenerationRequest(
        file_path=Path(""),
        provider=provider,
        model=model,
        api_key=api_key,
        base_url=base_url,
        canvas_format=job.canvas_format or "ppt169",
        style=job.style or "academic",
        language=job.language or "zh",
        detail_level=job.detail_level or "normal",
        confirm_outline=False,
        quick_mode=getattr(job, 'mode', None) == 'quick',
    )

    async def _run():
        from backend.session.progress import payloads_from_progress_event
        async for event in continue_pipeline(request.job_id, request.manuscript, pipeline_request):
            current_job = session_manager.get_job(request.job_id)
            if current_job is None:
                return
            for payload, updates in payloads_from_progress_event(request.job_id, current_job, event):
                session_manager.record_event(request.job_id, payload, **updates)

    task = asyncio.create_task(_run())
    session_manager.register_task(request.job_id, task)

    return RefineResponse(job_id=request.job_id, status="started")


async def _run_refine_job(job_id: str, request: PipelineRefineRequest) -> None:
    from backend.orchestrator.pipeline import run_refine_pipeline
    from backend.session.progress import payloads_from_progress_event

    try:
        async for event in run_refine_pipeline(request):
            current_job = session_manager.get_job(job_id)
            if current_job is None:
                return
            for payload, updates in payloads_from_progress_event(job_id, current_job, event):
                session_manager.record_event(job_id, payload, **updates)
    except asyncio.CancelledError:
        session_manager.mark_job_cancelled(job_id)
        raise
    except Exception as exc:
        session_manager.update_job(job_id, status="error", error=str(exc), message=str(exc))


@router.post("/generate/retry", response_model=RefineResponse)
async def retry_generation(request: RetryRequest) -> RefineResponse:
    """Retry a failed job from the strategy stage using the stored manuscript."""
    job = session_manager.get_job(request.job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")

    # Allow retry if job is error/cancelled, or if stuck in a running stage but task is gone
    terminal = job.status in ("error", "cancelled", "complete")
    task_alive = request.job_id in session_manager._tasks and not session_manager._tasks[request.job_id].done()
    if not terminal and task_alive:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Job is still running. Cancel it first.")

    # Extract manuscript from job events (research complete event)
    manuscript = None
    for ev in reversed(job.events):
        if ev.get("stage") == "research" and ev.get("status") == "complete":
            manuscript = (ev.get("data") or {}).get("manuscript")
            if manuscript:
                break

    if not manuscript:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot retry: manuscript not found in job history. Please start a new generation.",
        )

    # Use provided model_settings or fall back to job's stored config
    mc = request.model_settings
    provider = mc.provider if mc else (job.provider or "doubao")
    model = mc.model if mc else (job.model_name or "doubao-seed-2-0-lite-260215")
    api_key = mc.api_key if mc else ""
    base_url = mc.base_url if mc else job.base_url

    session_manager.update_job(
        request.job_id,
        status="strategy",
        message="Retrying from strategy stage...",
        error=None,
    )

    from backend.orchestrator.pipeline import GenerationRequest, continue_pipeline

    pipeline_request = GenerationRequest(
        file_path=Path(""),
        provider=provider,
        model=model,
        api_key=api_key,
        base_url=base_url,
        canvas_format=job.canvas_format or "ppt169",
        style=job.style or "academic",
        language=job.language or "zh",
        detail_level=job.detail_level or "normal",
        confirm_outline=False,
        quick_mode=getattr(job, 'mode', None) == 'quick',
    )

    async def _run():
        from backend.session.progress import payloads_from_progress_event
        async for event in continue_pipeline(request.job_id, manuscript, pipeline_request):
            current_job = session_manager.get_job(request.job_id)
            if current_job is None:
                return
            for payload, updates in payloads_from_progress_event(request.job_id, current_job, event):
                session_manager.record_event(request.job_id, payload, **updates)

    task = asyncio.create_task(_run())
    session_manager.register_task(request.job_id, task)

    return RefineResponse(job_id=request.job_id, status="started")
