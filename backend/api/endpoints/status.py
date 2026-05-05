"""Job status endpoint."""

from pathlib import Path

from fastapi import APIRouter, HTTPException, status

from backend.api.schemas import CancelJobResponse, JobStatus
from backend.session.manager import session_manager

router = APIRouter()


def _extract_manuscript_from_events(job) -> dict | None:
    """Extract manuscript data from job events if the pipeline is awaiting confirmation."""
    if job.status != "research":
        return None
    # Search events in reverse order for manuscript data
    for ev in reversed(job.events):
        ev_data = ev.get("data") if isinstance(ev, dict) else getattr(ev, "data", None)
        if isinstance(ev_data, dict) and "manuscript" in ev_data:
            return {"manuscript": ev_data["manuscript"]}
    return None


@router.get("/status/{job_id}", response_model=JobStatus)
async def get_job_status(job_id: str) -> JobStatus:
    job = session_manager.get_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found.",
        )

    # Determine if awaiting user confirmation
    data = None
    if job.status == "research" and "Waiting for user confirmation" in (job.message or ""):
        data = _extract_manuscript_from_events(job)

    normalized_status = "complete" if job.output_path and Path(job.output_path).exists() else job.status

    # Use awaiting_confirmation as a clearer status for frontend
    if data and normalized_status == "research":
        normalized_status = "awaiting_confirmation"

    return JobStatus(
        status=normalized_status,
        progress=job.progress,
        message=job.message,
        slides_completed=job.slides_completed,
        total_slides=job.total_slides,
        output_path=job.output_path,
        error=None if normalized_status == "complete" else job.error,
        data=data,
    )


@router.post("/status/{job_id}/cancel", response_model=CancelJobResponse)
async def cancel_job(job_id: str) -> CancelJobResponse:
    job = session_manager.get_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found.",
        )

    if job.status in {"complete", "error", "cancelled"}:
        return CancelJobResponse(job_id=job_id, status=job.status)

    cancelled = session_manager.cancel_job(job_id)
    if not cancelled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Job is not cancellable.",
        )

    return CancelJobResponse(job_id=job_id, status="cancelling")
