"""Usage tracking endpoint."""

from fastapi import APIRouter

from backend.session.manager import session_manager

router = APIRouter()


@router.get("/usage/{job_id}")
async def get_usage(job_id: str) -> dict:
    """Get token usage statistics for a job."""
    job = session_manager.get_job(job_id)
    if job is None:
        return {"job_id": job_id, "usage": []}
    return {"job_id": job_id, "usage": []}
