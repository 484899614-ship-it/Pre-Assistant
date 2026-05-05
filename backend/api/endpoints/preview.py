"""Preview endpoint for slide SVG content."""

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, status

from backend.api.schemas import PreviewResponse, PreviewSlide
from backend.generator.project_manager import get_svg_files, get_notes
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

    slides = []
    for i, svg_path in enumerate(svg_files):
        content = svg_path.read_text(encoding="utf-8")
        slide_notes = notes.get(svg_path.stem, None)
        slides.append(
            PreviewSlide(
                index=i + 1,
                name=svg_path.stem,
                source="final" if "svg_final" in str(svg_path) else "output",
                content=content,
                notes=slide_notes,
            )
        )

    return PreviewResponse(
        job_id=job_id,
        project_dir=str(project_dir),
        slides=slides,
        output_path=job.output_path,
        status=job.status,
    )
