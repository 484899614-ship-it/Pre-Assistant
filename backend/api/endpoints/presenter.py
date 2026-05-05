"""Presenter mode API endpoints — upload slides and notes for presenter view."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status

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

    if suffix not in (".pdf",):
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
    """Render PPTX slides to PNG images.

    Strategy:
    1. Try LibreOffice headless → PDF → PyMuPDF (best quality)
    2. Fall back to python-pptx manual rendering (text + basic shapes)
    """
    # Build URL prefix
    rel = out_dir.relative_to(settings.sessions_dir)
    url_prefix = f"/assets/{rel.as_posix()}"

    # --- Strategy 1: Convert to PDF then render (best quality) ---
    pdf_bytes = _pptx_to_pdf(content, out_dir)
    if pdf_bytes:
        try:
            import fitz
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            slides: list[dict] = []
            for i, page in enumerate(doc):
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
                fname = f"slide_{i + 1:03d}.png"
                pix.save(str(out_dir / fname))
                slides.append({"url": f"{url_prefix}/{fname}"})
            doc.close()
            return slides
        except Exception:
            pass

    # --- Strategy 2: python-pptx manual rendering ---
    return _render_pptx_manual(content, out_dir, url_prefix)


def _pptx_to_pdf(content: bytes, work_dir: Path) -> bytes | None:
    """Convert PPTX to PDF. Tries PowerPoint COM (Windows) first, then LibreOffice."""
    # Strategy 1: PowerPoint COM automation (Windows, best quality)
    result = _pptx_to_pdf_via_powerpoint(content)
    if result:
        return result

    # Strategy 2: LibreOffice headless
    return _pptx_to_pdf_via_libreoffice(content, work_dir)


def _pptx_to_pdf_via_powerpoint(content: bytes) -> bytes | None:
    """Convert PPTX to PDF using PowerPoint COM automation on Windows."""
    import sys, tempfile
    if sys.platform != "win32":
        return None

    try:
        import comtypes.client
    except ImportError:
        return None

    tmp_dir = tempfile.mkdtemp()
    pptx_path = Path(tmp_dir) / "input.pptx"
    pdf_path = Path(tmp_dir) / "input.pdf"
    pptx_path.write_bytes(content)

    try:
        powerpoint = comtypes.client.CreateObject("Powerpoint.Application")
        powerpoint.Visible = 0  # Do not show the window
        presentation = powerpoint.Presentations.Open(str(pptx_path.resolve()), WithWindow=False)
        # ppSaveAsPDF = 32
        presentation.SaveAs(str(pdf_path.resolve()), 32)
        presentation.Close()
        powerpoint.Quit()
        if pdf_path.exists():
            return pdf_path.read_bytes()
    except Exception:
        # Ensure PowerPoint process is cleaned up on failure
        try:
            powerpoint.Quit()
        except Exception:
            pass
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return None


def _pptx_to_pdf_via_libreoffice(content: bytes, work_dir: Path) -> bytes | None:
    """Convert PPTX to PDF using LibreOffice headless. Returns PDF bytes or None."""
    import subprocess, tempfile

    soffice_paths = [
        "soffice",
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        "/usr/bin/soffice",
        "/usr/bin/libreoffice",
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    ]
    soffice = None
    for p in soffice_paths:
        try:
            result = subprocess.run([p, "--version"], capture_output=True, timeout=5)
            soffice = p
            break
        except Exception:
            continue

    if not soffice:
        return None

    try:
        with tempfile.TemporaryDirectory() as tmp:
            pptx_path = Path(tmp) / "input.pptx"
            pptx_path.write_bytes(content)
            subprocess.run(
                [soffice, "--headless", "--convert-to", "pdf", "--outdir", tmp, str(pptx_path)],
                capture_output=True, timeout=60,
            )
            pdf_path = Path(tmp) / "input.pdf"
            if pdf_path.exists():
                return pdf_path.read_bytes()
    except Exception:
        pass

    return None


def _render_pptx_manual(content: bytes, out_dir: Path, url_prefix: str) -> list[dict]:
    """Render PPTX slides manually using python-pptx + Pillow.

    Extracts slide dimensions, backgrounds, text frames, and shapes
    to produce a reasonable visual approximation.
    """
    try:
        from pptx import Presentation
        from pptx.util import Inches, Pt, Emu
    except ImportError:
        raise HTTPException(status_code=500, detail="PPTX parsing not available (python-pptx missing).")

    import io
    from PIL import Image, ImageDraw, ImageFont

    prs = Presentation(io.BytesIO(content))
    slides: list[dict] = []

    # Slide dimensions (EMU → pixels at 2x for quality)
    slide_w_emu = prs.slide_width or 9144000   # default 10 inches
    slide_h_emu = prs.slide_height or 6858000  # default 7.5 inches
    px_per_emu = 2.0 / 914400  # ~2 px per inch at standard 10" width → ~1280px
    img_w = max(int(slide_w_emu * px_per_emu), 640)
    img_h = max(int(slide_h_emu * px_per_emu), 480)

    def _emu_to_px(emu):
        return int(emu * px_per_emu)

    def _get_font(size_px: int = 20):
        try:
            return ImageFont.truetype("arial.ttf", size_px)
        except Exception:
            try:
                return ImageFont.truetype("msyh.ttc", size_px)
            except Exception:
                return ImageFont.load_default()

    for i, slide in enumerate(prs.slides):
        img = Image.new("RGB", (img_w, img_h), "#FFFFFF")
        draw = ImageDraw.Draw(img)

        # Collect shapes: separate full-slide background images from normal shapes
        bg_images = []  # images that cover 90%+ of the slide area
        fg_shapes = []  # everything else

        for shape in slide.shapes:
            left = _emu_to_px(shape.left or 0)
            top = _emu_to_px(shape.top or 0)
            width = _emu_to_px(shape.width or 0)
            height = _emu_to_px(shape.height or 0)

            # Check if this is a full-slide image (background/thumbnail)
            is_fullslide_img = (
                shape.shape_type == 13  # PICTURE
                and width >= img_w * 0.9
                and height >= img_h * 0.9
            )

            if is_fullslide_img:
                bg_images.append((shape, left, top, width, height))
            else:
                fg_shapes.append((shape, left, top, width, height))

        # Draw slide background color if solid fill
        bg = slide.background
        if bg and bg.fill and bg.fill.type is not None:
            try:
                from pptx.oxml.ns import qn
                bg_elem = bg._element
                solid = bg_elem.find(".//" + qn("a:solidFill"))
                if solid is not None:
                    srgb = solid.find(qn("a:srgbClr"))
                    if srgb is not None:
                        color_hex = srgb.get("val", "FFFFFF")
                        img.paste(f"#{color_hex}", [0, 0, img_w, img_h])
            except Exception:
                pass

        # Draw background images first (full-slide images)
        for shape, left, top, width, height in bg_images:
            try:
                img_data = shape.image.blob
                pil_img = Image.open(io.BytesIO(img_data))
                pil_img = pil_img.resize((max(width, 1), max(height, 1)))
                img.paste(pil_img, (left, top))
            except Exception:
                pass

        # Draw foreground shapes
        for shape, left, top, width, height in fg_shapes:
            # Draw shape fill
            if hasattr(shape, "fill") and shape.fill and shape.fill.type is not None:
                try:
                    from pptx.oxml.ns import qn
                    sp_elem = shape._element
                    solid = sp_elem.find(".//" + qn("a:solidFill"))
                    if solid is not None:
                        srgb = solid.find(qn("a:srgbClr"))
                        if srgb is not None:
                            color_hex = srgb.get("val", "FFFFFF")
                            draw.rectangle([left, top, left + width, top + height], fill=f"#{color_hex}")
                except Exception:
                    pass

            # Draw text content
            if shape.has_text_frame:
                text_y = top + 4
                for para in shape.text_frame.paragraphs:
                    para_text = para.text.strip()
                    if not para_text:
                        text_y += 8
                        continue
                    # Estimate font size
                    font_size = 16
                    try:
                        for run in para.runs:
                            if run.font.size:
                                font_size = max(int(run.font.size * px_per_emu), 10)
                                break
                    except Exception:
                        pass
                    font = _get_font(font_size)
                    # Text color
                    fill_color = "#000000"
                    try:
                        for run in para.runs:
                            if run.font.color and run.font.color.rgb:
                                fill_color = f"#{run.font.color.rgb}"
                                break
                    except Exception:
                        pass
                    # Draw text (wrap if too wide)
                    lines = []
                    words = para_text
                    bbox = font.getbbox(words) if hasattr(font, 'getbbox') else (0, 0, len(words) * font_size, font_size)
                    if bbox[2] - bbox[0] > width - 8:
                        # Simple character-level wrapping
                        line = ""
                        for ch in words:
                            test = line + ch
                            tb = font.getbbox(test) if hasattr(font, 'getbbox') else (0, 0, len(test) * font_size, font_size)
                            if tb[2] - tb[0] > width - 8 and line:
                                lines.append(line)
                                line = ch
                            else:
                                line = test
                        if line:
                            lines.append(line)
                    else:
                        lines.append(words)

                    for line in lines:
                        draw.text((left + 4, text_y), line, fill=fill_color, font=font)
                        line_h = font_size + 4
                        text_y += line_h

            # Draw small images (not full-slide thumbnails)
            if shape.shape_type == 13:  # MSO_SHAPE_TYPE.PICTURE
                try:
                    img_data = shape.image.blob
                    pil_img = Image.open(io.BytesIO(img_data))
                    pil_img = pil_img.resize((max(width, 1), max(height, 1)))
                    img.paste(pil_img, (left, top))
                except Exception:
                    pass

        fname = f"slide_{i + 1:03d}.png"
        img_path = out_dir / fname
        img.save(str(img_path))
        slides.append({"url": f"{url_prefix}/{fname}"})

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

# Chinese number mapping for 第一页, 第二页 etc.
_CN_NUM: dict[str, int] = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    "十一": 11, "十二": 12, "十三": 13, "十四": 14, "十五": 15,
    "十六": 16, "十七": 17, "十八": 18, "十九": 19, "二十": 20,
}
_CN_NUM_PATTERN = "|".join(sorted(_CN_NUM.keys(), key=len, reverse=True))

_PAGE_NUM_RE = re.compile(
    r"(?:^|\n)\s*(?:#\s*)?"           # optional leading # heading
    r"(?:第\s*(\d+)\s*页"              # 第1页, 第 1 页
    rf"|第\s*({_CN_NUM_PATTERN})\s*页"  # 第一页, 第二页
    r"|Page\s*(\d+)"                   # Page 1
    r"|Slide\s*(\d+)"                  # Slide 1
    r"|PPT\s*(\d+)"                    # PPT 1
    r"|幻灯片\s*(\d+)"                 # 幻灯片 1
    r")"
    r"\s*[:：]?",                     # optional colon after
    re.IGNORECASE,
)

# Same as above but matches anywhere in text (not just line start)
_PAGE_NUM_MID_RE = re.compile(
    r"(?:第\s*(\d+)\s*页"
    rf"|第\s*({_CN_NUM_PATTERN})\s*页"
    r"|Page\s*(\d+)"
    r"|Slide\s*(\d+)"
    r"|PPT\s*(\d+)"
    r"|幻灯片\s*(\d+)"
    r")"
    r"\s*[:：]?",
    re.IGNORECASE,
)


@router.post("/upload-notes")
async def upload_notes(file: UploadFile = File(...), total_slides: int = Form(0)):
    """Upload a text/docx/md file with speaker notes and parse by page.
    If total_slides is provided, notes without page markers will be evenly
    distributed across that many slides.
    """
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

    notes = _split_notes(text, total_slides=total_slides if total_slides > 0 else None)
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


def _extract_page_num(m: re.Match) -> int:
    """Extract page number from a regex match of _PAGE_NUM_RE or _PAGE_NUM_MID_RE."""
    groups = m.groups()
    if groups[0]:        # 第1页 (arabic digit)
        return int(groups[0])
    elif groups[1]:      # 第一页 (chinese number)
        return _CN_NUM.get(groups[1], 0)
    else:                # Page/Slide/PPT/幻灯片 (groups 2-5)
        return int(groups[2] or groups[3] or groups[4] or groups[5] or 0)


def _split_notes(text: str, total_slides: int | None = None) -> dict[str, str]:
    """Split notes text into pages. Supports various heading formats:
    第1页, 第一页, Page 1, Slide 1, PPT 1, 幻灯片 1 (matched anywhere in text),
    or --- delimiters. If no markers found and total_slides is given, distribute
    evenly by paragraphs.
    """
    notes: dict[str, str] = {}

    # Strategy 1: Find all page markers anywhere in the text (handles DOCX without line breaks)
    markers: list[tuple[int, int, int]] = []  # (start_pos, end_pos, page_num)
    for m in _PAGE_NUM_MID_RE.finditer(text):
        page_num = _extract_page_num(m)
        if page_num > 0:
            markers.append((m.start(), m.end(), page_num))

    if markers:
        for i, (start, end, page_num) in enumerate(markers):
            # Content starts after the marker
            content_start = end
            # Content ends at the start of the next marker, or end of text
            content_end = markers[i + 1][0] if i + 1 < len(markers) else len(text)
            content = text[content_start:content_end].strip()
            if content:
                notes[str(page_num)] = content
        return notes

    # Strategy 2: Try --- delimiter split
    parts = _PAGE_SPLIT_RE.split(text)
    parts = [p.strip() for p in parts if p.strip()]

    if len(parts) > 1:
        for i, part in enumerate(parts, 1):
            notes[str(i)] = part
        return notes

    # No markers found: split by paragraph blocks (double newlines) and distribute
    if total_slides and total_slides > 1 and text.strip():
        # Split into paragraph blocks separated by blank lines
        para_blocks = re.split(r"\n\s*\n", text.strip())
        para_blocks = [p.strip() for p in para_blocks if p.strip()]
        if not para_blocks:
            notes["1"] = text.strip()
            return notes

        # Evenly distribute paragraphs across slides
        n = len(para_blocks)
        per_slide = n / total_slides
        for slide_num in range(1, total_slides + 1):
            start = int(round((slide_num - 1) * per_slide))
            end = int(round(slide_num * per_slide))
            chunk = "\n\n".join(para_blocks[start:end])
            if chunk:
                notes[str(slide_num)] = chunk

        # If some slides ended up empty, fill from neighbors
        for i in range(1, total_slides + 1):
            if str(i) not in notes and str(i - 1) in notes:
                # Split the previous slide's notes in half
                prev = notes[str(i - 1)]
                mid = len(prev) // 2
                # Find nearest sentence break
                for offset in range(0, max(mid, len(prev) - mid)):
                    for pos in [mid + offset, mid - offset]:
                        if 0 < pos < len(prev) and prev[pos] in '。！？\n':
                            mid = pos + 1
                            break
                    else:
                        continue
                    break
                notes[str(i - 1)] = prev[:mid].strip()
                notes[str(i)] = prev[mid:].strip()

        return notes

    # Single block: assign all to page 1
    if text.strip():
        notes["1"] = text.strip()

    return notes
