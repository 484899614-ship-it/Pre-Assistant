"""Presenter mode API endpoints — upload slides and notes for presenter view."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile, status

from backend.config import settings

router = APIRouter(prefix="/presenter")

# Temp directory for presenter uploads
_presenter_dir: Path | None = None


def _get_presenter_dir() -> Path:
    """Store presenter uploads under sessions_dir so they are served by the /assets static mount."""
    d = settings.sessions_dir / "presenter"
    d.mkdir(parents=True, exist_ok=True)
    return d


@router.post("/upload-slides")
async def upload_slides(file: UploadFile = File(...)):
    """Upload a PDF file and render each page as an image for presenter mode."""
    suffix = Path(file.filename or "").suffix.lower()

    if suffix not in (".pdf", ".pptx"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported file type. Please upload a PDF file.",
        )

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="File is empty.")
    if len(content) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="File too large.")

    upload_id = uuid.uuid4().hex[:10]
    out_dir = _get_presenter_dir() / upload_id / "slides"
    out_dir.mkdir(parents=True, exist_ok=True)

    if suffix == ".pdf":
        slides = _render_pdf(content, out_dir)
    else:
        # PPTX rendering requires LibreOffice which may not be available;
        # fall back to extracting thumbnail images from the PPTX archive.
        slides = _render_pptx(content, out_dir)

    return {"upload_id": upload_id, "slides": slides}


def _render_pdf(content: bytes, out_dir: Path) -> list[dict]:
    """Render PDF pages as PNG images using PyMuPDF."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        raise HTTPException(status_code=500, detail="PDF rendering not available (PyMuPDF missing).")

    # Build URL prefix: /assets/<relative path from sessions_dir>
    rel = out_dir.relative_to(settings.sessions_dir)
    url_prefix = f"/assets/{rel.as_posix()}"

    doc = fitz.open(stream=content, filetype="pdf")
    slides: list[dict] = []
    for i, page in enumerate(doc):
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))  # 2x for decent quality
        fname = f"slide_{i + 1:03d}.png"
        img_path = out_dir / fname
        pix.save(str(img_path))
        slides.append({"url": f"{url_prefix}/{fname}"})
    doc.close()
    return slides


def _render_pptx(content: bytes, out_dir: Path) -> list[dict]:
    """Attempt to render PPTX slides. Extracts embedded thumbnails if available."""
    try:
        from pptx import Presentation
    except ImportError:
        raise HTTPException(status_code=500, detail="PPTX parsing not available (python-pptx missing).")

    import io
    from PIL import Image

    # Build URL prefix
    rel = out_dir.relative_to(settings.sessions_dir)
    url_prefix = f"/assets/{rel.as_posix()}"

    prs = Presentation(io.BytesIO(content))
    slides: list[dict] = []

    for i, slide in enumerate(prs.slides):
        # Try to extract the thumbnail embedded in the PPTX
        thumb = None
        for rel in slide.part.rels.values():
            if "image" in rel.reltype:
                try:
                    img_data = rel.target_part.blob
                    img = Image.open(io.BytesIO(img_data))
                    fname = f"slide_{i + 1:03d}.png"
                    img_path = out_dir / fname
                    img.save(str(img_path))
                    thumb = f"{url_prefix}/{fname}"
                    break
                except Exception:
                    continue

        if thumb:
            slides.append({"url": thumb})
        else:
            # No embedded image — create a placeholder
            placeholder = _create_placeholder(out_dir, i + 1, f"Slide {i + 1}")
            slides.append({"url": placeholder})

    return slides


def _create_placeholder(out_dir: Path, index: int, text: str) -> str:
    """Create a simple placeholder image for slides without thumbnails."""
    from PIL import Image, ImageDraw, ImageFont

    # Build URL prefix
    rel = out_dir.relative_to(settings.sessions_dir)
    url_prefix = f"/assets/{rel.as_posix()}"

    img = Image.new("RGB", (1280, 720), "#F7FAFC")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 48)
    except Exception:
        font = ImageFont.load_default()
    draw.text((640, 360), text, fill="#2D3748", font=font, anchor="mm")
    fname = f"slide_{index:03d}.png"
    img_path = out_dir / fname
    img.save(str(img_path))
    return f"{url_prefix}/{fname}"


# ── Notes Upload ──

_PAGE_SPLIT_RE = re.compile(r"(?:^|\n)\s*---+\s*(?:\n|$)")
_PAGE_NUM_RE = re.compile(r"(?:^|\n)\s*#\s*(?:第\s*(\d+)\s*页|Page\s*(\d+)|Slide\s*(\d+))", re.IGNORECASE)


@router.post("/upload-notes")
async def upload_notes(file: UploadFile = File(...)):
    """Upload a text/docx/md file with speaker notes and parse by page."""
    suffix = Path(file.filename or "").suffix.lower()

    if suffix not in (".txt", ".md", ".docx"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported file type. Please upload a .txt, .md, or .docx file.",
        )

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="File is empty.")

    if suffix == ".docx":
        text = _parse_docx(content)
    else:
        text = content.decode("utf-8", errors="replace")

    notes = _split_notes(text)
    return {"notes": notes}


def _parse_docx(content: bytes) -> str:
    """Extract text from a .docx file. Requires python-docx."""
    try:
        from docx import Document
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="DOCX parsing not available. Please install python-docx or upload a .txt file.",
        )

    import io

    doc = Document(io.BytesIO(content))
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())


def _split_notes(text: str) -> dict[str, str]:
    """Split notes text into pages. Supports --- delimiters or # 第N页 / # Page N / # Slide N headings."""
    notes: dict[str, str] = {}

    # Try page number headings first: # 第1页, # Page 1, # Slide 1
    page_blocks: list[tuple[int, str]] = []
    current_page = None
    current_lines: list[str] = []

    for line in text.split("\n"):
        m = _PAGE_NUM_RE.match(line)
        if m:
            if current_page is not None and current_lines:
                page_blocks.append((current_page, "\n".join(current_lines).strip()))
            page_num = int(m.group(1) or m.group(2) or m.group(3))
            current_page = page_num
            current_lines = []
        elif current_page is not None:
            current_lines.append(line)

    if current_page is not None and current_lines:
        page_blocks.append((current_page, "\n".join(current_lines).strip()))

    if page_blocks:
        for page_num, block_text in page_blocks:
            if block_text:
                notes[str(page_num)] = block_text
        return notes

    # Try --- delimiter split
    parts = _PAGE_SPLIT_RE.split(text)
    parts = [p.strip() for p in parts if p.strip()]

    if len(parts) > 1:
        for i, part in enumerate(parts, 1):
            notes[str(i)] = part
        return notes

    # Single block: assign all to page 1
    if text.strip():
        notes["1"] = text.strip()

    return notes
