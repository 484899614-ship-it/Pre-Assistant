"""Extract API endpoint — extracts text, figures, equations from uploaded PDF using enhanced parser."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, status

from backend.api.schemas import ExtractResponse
from backend.parser.pdf_parser import PDFParser
from backend.session.manager import session_manager

router = APIRouter()

parser = PDFParser()


@router.post("/extract", response_model=ExtractResponse)
async def extract_pdf(session_id: str) -> ExtractResponse:
    """Extract text, figures, equations, and tables from a previously uploaded PDF."""
    session = session_manager.get_session(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found.",
        )

    pdf_path = session.file_path
    session_dir = pdf_path.parent

    try:
        paper = await parser.parse(pdf_path, session_dir)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"PDF extraction failed: {exc}",
        )

    # Build response from ParsedPaper
    all_figures = paper.all_figures()
    total_text = sum(len(s.content) for s in paper.sections)
    if paper.abstract:
        total_text += len(paper.abstract)

    # Store parsed paper data on the session
    extracted_data = {
        "total_pages": 0,  # Will be filled from doc
        "title": paper.title,
        "authors": paper.authors,
        "abstract": paper.abstract,
        "sections": [
            {
                "title": s.title,
                "level": s.level,
                "content": s.content[:500],
                "figures_count": len(s.figures),
                "tables_count": len(s.tables),
            }
            for s in paper.sections
        ],
        "figures": [
            {
                "path": str(f.path),
                "caption": f.caption,
                "page_number": f.page_number,
                "extraction_method": f.extraction_method,
                "quality_score": f.quality_score,
                "natural_width": f.natural_width,
                "natural_height": f.natural_height,
                "fig_id": f.fig_id,
            }
            for f in all_figures
        ],
    }

    session_manager.update_session(
        session_id,
        extracted_data=extracted_data,
    )

    # Compute page count from PDF
    import fitz
    doc = fitz.open(str(pdf_path))
    page_count = doc.page_count
    doc.close()

    return ExtractResponse(
        session_id=session_id,
        total_pages=page_count,
        figures_count=len(all_figures),
        equations_count=0,
        text_length=total_text,
        outline=[
            {"title": s.title, "level": s.level}
            for s in paper.sections
            if s.level <= 2
        ],
        pages=[
            {"page": i + 1, "title": paper.sections[i].title if i < len(paper.sections) else ""}
            for i in range(min(page_count, len(paper.sections)))
        ],
        visual_assets=[
            {
                "type": "figure",
                "path": str(f.path),
                "caption": f.caption,
                "page": f.page_number,
                "fig_id": f.fig_id,
            }
            for f in all_figures
        ],
    )
