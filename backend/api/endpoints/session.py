"""Session and history endpoints."""

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel

from backend.session.manager import session_manager

router = APIRouter()


class HistorySession(BaseModel):
    session_id: str
    file_name: str
    file_size: int
    source_type: str
    title: str | None = None
    authors: str | None = None


class HistoryJob(BaseModel):
    job_id: str
    session_id: str
    status: str
    progress: float
    provider: str | None = None
    model_name: str | None = None
    canvas_format: str | None = None
    style: str | None = None
    language: str | None = None
    output_path: str | None = None
    error: str | None = None
    total_slides: int = 0
    slides_completed: int = 0


class HistoryItem(BaseModel):
    session: HistorySession
    jobs: list[HistoryJob]


class HistoryResponse(BaseModel):
    items: list[HistoryItem]


@router.get("/history", response_model=HistoryResponse)
async def get_history() -> HistoryResponse:
    """Return recent sessions with their associated jobs."""
    items: list[HistoryItem] = []
    # Iterate sessions in reverse order (newest first)
    for session in reversed(list(session_manager._sessions.values())):
        # Find all jobs for this session
        session_jobs = [
            job for job in session_manager._jobs.values()
            if job.session_id == session.id
        ]
        # Sort jobs newest first
        session_jobs.sort(key=lambda j: j.id, reverse=True)

        # Get paper title from extracted data
        title = None
        authors = None
        if session.extracted_data:
            title = session.extracted_data.get("title")
            authors_raw = session.extracted_data.get("authors")
            if isinstance(authors_raw, list):
                authors = ", ".join(str(a) for a in authors_raw[:3])
                if len(authors_raw) > 3:
                    authors += " et al."
            elif authors_raw:
                authors = str(authors_raw)

        items.append(HistoryItem(
            session=HistorySession(
                session_id=session.id,
                file_name=session.file_name,
                file_size=session.file_size,
                source_type=session.source_type,
                title=title,
                authors=authors,
            ),
            jobs=[
                HistoryJob(
                    job_id=j.id,
                    session_id=j.session_id,
                    status=j.status,
                    progress=j.progress,
                    provider=j.provider,
                    model_name=j.model_name,
                    canvas_format=j.canvas_format,
                    style=j.style,
                    language=j.language,
                    output_path=j.output_path,
                    error=j.error,
                    total_slides=j.total_slides,
                    slides_completed=j.slides_completed,
                )
                for j in session_jobs
            ],
        ))

    return HistoryResponse(items=items)


@router.delete("/session/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(session_id: str) -> Response:
    session = session_manager.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found.")

    session_manager.delete_session(session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
