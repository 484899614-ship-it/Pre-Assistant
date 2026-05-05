"""Preview endpoint for slide SVG content."""

import json
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, status

from backend.api.schemas import (
    ModelConfig,
    PreviewResponse,
    PreviewSlide,
    RegenerateNotesRequest,
    RegenerateNotesResponse,
)
from backend.generator.project_manager import get_svg_files, get_notes
from backend.llm.registry import create_provider
from backend.orchestrator.svg_executor import generate_speaker_notes
from backend.session.manager import session_manager

router = APIRouter()


def _build_fallback_notes(project_dir: Path, svg_files: list[Path]) -> dict[str, str]:
    """Build basic speaker notes from manuscript when notes.json doesn't exist."""
    manuscript_path = project_dir / "manuscript.md"

    if manuscript_path.exists():
        manuscript = manuscript_path.read_text(encoding="utf-8")
        _SLIDE_DELIMITER_RE = re.compile(r"(?m)^\s*---\s*$")
        pages = [p.strip() for p in _SLIDE_DELIMITER_RE.split(manuscript) if p.strip()]
    else:
        pages = []

    notes: dict[str, str] = {}
    for i, svg_path in enumerate(svg_files):
        stem = svg_path.stem

        # Try manuscript-derived notes first
        if i < len(pages):
            page = pages[i]
            heading_match = re.match(r"^##?\s+(.+)$", page, re.MULTILINE)
            heading = heading_match.group(1).strip() if heading_match else f"第{i + 1}页"
            bullets = [line.strip() for line in page.split("\n") if line.strip().startswith("- ") or line.strip().startswith("• ")]
            bullet_text = "\n".join(bullets[:5])
            notes[stem] = f"[过渡] 接下来介绍{heading}。\n\n{bullet_text}"
        else:
            # Fallback: extract title from SVG filename
            parts = stem.split("_", 1)
            title = parts[1].replace("_", " ") if len(parts) > 1 else f"第{i + 1}页"
            notes[stem] = f"[过渡] 接下来介绍{title}。"

    # Save as notes.json for future requests
    import json
    notes_path = project_dir / "notes.json"
    notes_path.write_text(json.dumps(notes, ensure_ascii=False, indent=2), encoding="utf-8")

    return notes


@router.get("/preview/{job_id}", response_model=PreviewResponse)
async def preview_slides(job_id: str) -> PreviewResponse:
    job = session_manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    if not job.project_dir:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No project workspace.")

    project_dir = Path(job.project_dir)
    if not project_dir.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project directory not found.")

    # Try final first, then output
    svg_files = get_svg_files(project_dir, "final") or get_svg_files(project_dir, "output")
    notes = get_notes(project_dir, svg_files)

    # If no notes.json exists, build fallback notes from manuscript
    if not notes and svg_files:
        notes = _build_fallback_notes(project_dir, svg_files)

    # Load sentence-level source mapping if available
    sources_path = project_dir / "notes_sources.json"
    notes_sources: dict[str, list[dict]] = {}
    if sources_path.exists():
        try:
            notes_sources = json.loads(sources_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    slides = []
    for i, svg_path in enumerate(svg_files):
        content = svg_path.read_text(encoding="utf-8")
        stem = svg_path.stem
        slide_notes = notes.get(stem, None)
        slide_sources = notes_sources.get(stem, None)
        slides.append(
            PreviewSlide(
                index=i + 1,
                name=stem,
                source="final" if "svg_final" in str(svg_path) else "output",
                content=content,
                notes=slide_notes,
                notes_sources=slide_sources,
            )
        )

    return PreviewResponse(
        job_id=job_id,
        project_dir=str(project_dir),
        slides=slides,
        output_path=job.output_path,
        status=job.status,
    )


@router.post("/notes/regenerate/{job_id}", response_model=RegenerateNotesResponse)
async def regenerate_notes(job_id: str, req: RegenerateNotesRequest):
    """Regenerate speaker notes for an existing job without re-generating slides."""
    job = session_manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    if not job.project_dir:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No project workspace.")

    project_dir = Path(job.project_dir)
    if not project_dir.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project directory not found.")

    # Load manuscript from project dir
    manuscript_path = project_dir / "manuscript.md"
    manuscript = ""
    if manuscript_path.exists():
        manuscript = manuscript_path.read_text(encoding="utf-8")
    else:
        # Build a manuscript from SVG content
        svg_files = get_svg_files(project_dir, "final") or get_svg_files(project_dir, "output")
        if not svg_files:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No slides found.")
        parts = []
        for svg_path in svg_files:
            # Extract text content from SVG for the manuscript
            svg_text = svg_path.read_text(encoding="utf-8")
            text_matches = re.findall(r'<text[^>]*>([^<]+)</text>', svg_text)
            stem = svg_path.stem
            heading = stem.split("_", 1)[1].replace("_", " ") if "_" in stem else stem
            content = "\n".join(f"- {t.strip()}" for t in text_matches if t.strip())
            parts.append(f"## {heading}\n{content}")
        manuscript = "\n\n---\n\n".join(parts)

    if not manuscript.strip():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No manuscript content available.")

    # Get paper text from the original PDF
    paper_text = ""
    session = session_manager.get_session(job.session_id)
    if session and session.file_path and Path(str(session.file_path)).exists():
        try:
            from backend.parser.pdf_parser import PDFParser
            parser = PDFParser()
            paper = await parser.parse(Path(str(session.file_path)), project_dir / "images")
            paper_text = paper.to_markdown() if hasattr(paper, "to_markdown") else ""
        except Exception:
            pass

    # Create LLM provider
    try:
        llm = create_provider(
            req.model_settings.provider,
            req.model_settings.api_key,
            base_url=req.model_settings.base_url,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    # Generate notes
    language = req.language or job.language or "zh"
    speech_minutes = req.speech_minutes

    notes = await generate_speaker_notes(
        manuscript,
        project_dir,
        llm,
        req.model_settings.model,
        language=language,
        paper_text=paper_text,
        speech_minutes=speech_minutes,
    )

    # Also load the sentence-level sources
    sources_path = project_dir / "notes_sources.json"
    notes_sources: dict[str, list[dict]] = {}
    if sources_path.exists():
        try:
            notes_sources = json.loads(sources_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    return RegenerateNotesResponse(job_id=job_id, notes=notes, notes_sources=notes_sources)
