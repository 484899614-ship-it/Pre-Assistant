"""Upload API endpoint."""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile, status

from backend.api.schemas import FileInfo, UploadResponse
from backend.config import settings
from backend.session.manager import session_manager

router = APIRouter()

SUPPORTED_EXTENSIONS = {
    ".pdf": "pdf",
    ".docx": "docx",
}


@router.post("/upload", response_model=UploadResponse)
async def upload_paper(file: UploadFile = File(...)) -> UploadResponse:
    suffix = Path(file.filename or "").suffix.lower()
    source_type = SUPPORTED_EXTENSIONS.get(suffix)
    if source_type is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported file type. Please upload a PDF or Word (.docx) document.",
        )

    content = await file.read()
    if len(content) > settings.max_upload_bytes:
        max_mb = settings.max_upload_bytes // (1024 * 1024)
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds max upload size of {max_mb} MB.",
        )
    if len(content) == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty.",
        )

    session_id = uuid.uuid4().hex[:12]
    session_dir = settings.sessions_dir / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    file_path = session_dir / (file.filename or f"input{suffix}")
    file_path.write_bytes(content)

    session = session_manager.create_session(
        file_path=file_path,
        source_type=source_type,
        file_name=file.filename or file_path.name,
        file_size=len(content),
        session_id=session_id,
    )

    return UploadResponse(
        session_id=session.id,
        file_info=FileInfo(
            name=session.file_name,
            size=session.file_size,
            source_type=session.source_type,
        ),
    )
