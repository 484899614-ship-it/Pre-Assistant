"""Version history endpoint."""

from pathlib import Path

from fastapi import APIRouter, HTTPException

from backend.session.manager import session_manager

router = APIRouter()


@router.get("/versions/{job_id}")
async def get_versions(job_id: str) -> dict:
    """Get version history for a job's refine iterations."""
    job = session_manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if not job.project_dir:
        return {"job_id": job_id, "versions": []}

    project_dir = Path(job.project_dir)
    versions = []

    # Check for svg_archive directories
    archive_dir = project_dir / "svg_archive"
    if archive_dir.exists():
        for round_dir in sorted(archive_dir.iterdir()):
            if round_dir.is_dir() and round_dir.name.startswith("round_"):
                svg_count = len(list(round_dir.glob("*.svg")))
                versions.append({
                    "round": int(round_dir.name.replace("round_", "")),
                    "svg_count": svg_count,
                    "path": str(round_dir),
                })

    # Current version
    current_svgs = len(list((project_dir / "svg_output").glob("*.svg"))) if (project_dir / "svg_output").exists() else 0
    if current_svgs > 0:
        versions.append({
            "round": len(versions) + 1,
            "svg_count": current_svgs,
            "path": str(project_dir / "svg_output"),
            "current": True,
        })

    return {"job_id": job_id, "versions": versions}
