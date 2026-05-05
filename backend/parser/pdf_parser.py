"""Enhanced PDF paper parser using PyMuPDF.

Extraction strategy (layered, best-effort):

1. **Embedded raster images** — fast, via page.get_images(). Skips tiny
   decorative bitmaps.

2. **Vector / mixed figure regions** — detects figure bounding boxes by
   locating caption text ("Figure N", "Fig.", "图N") and rendering
   that page region at high DPI.

3. **Whole-page screenshot fallback** — if a page appears to contain a
   figure block but neither of the above methods produced an image.

4. **Tables** — uses page.find_tables() when available (PyMuPDF ≥ 1.23).
   Extracted as Markdown.

Caption proximity matching assigns each extracted image to the nearest
caption/label found on the same page.

Font-size analysis drives heading-level detection.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path

import fitz  # PyMuPDF

from .base import PaperParser
from .paper_model import PaperEquation, PaperFigure, PaperSection, PaperTable, ParsedPaper

# ── tunables ────────────────────────────────────────────────────────────────
MIN_IMG_PX = 80
RENDER_DPI = 150
REGION_DPI = 200
CAPTION_SEARCH_PX = 120
MAX_CAPTION_GAP_PX = 420
MIN_RENDERED_FIGURE_SIDE = 72
MIN_GRAPHIC_AREA_RATIO = 0.015
BODY_TEXT_CHARS_THRESHOLD = 140
LABEL_TEXT_CHARS_THRESHOLD = 80
# ─────────────────────────────────────────────────────────────────────────────

_CAPTION_RE = re.compile(
    r"^\s*(fig(?:ure)?\.?\s*\d+|table\s*\d+|图\s*\d+|表\s*\d+)\b",
    re.IGNORECASE,
)


class PDFParser(PaperParser):
    """Parse academic PDF papers using PyMuPDF."""

    last_parse_info: dict[str, object] = {}

    async def parse(self, file_path: Path, output_dir: Path) -> ParsedPaper:
        output_dir.mkdir(parents=True, exist_ok=True)
        images_dir = output_dir / "images"
        images_dir.mkdir(exist_ok=True)

        info: dict[str, object] = {"path": "heuristic"}

        doc = fitz.open(str(file_path))
        try:
            size_map = self._analyze_font_sizes(doc)
            title = self._extract_title(doc, size_map)
            authors = self._extract_authors(doc)
            sections, abstract = self._extract_sections(doc, size_map, images_dir)

            PDFParser.last_parse_info = info

            paper = ParsedPaper(
                title=title,
                authors=authors,
                abstract=abstract,
                sections=sections,
                source_type="pdf",
                figures_dir=images_dir,
            )
            self._write_figure_review_manifest(paper, images_dir)
            return paper
        finally:
            doc.close()

    # ── font-size analysis ────────────────────────────────────────────────

    def _analyze_font_sizes(self, doc: fitz.Document) -> dict[str, float]:
        size_counter: Counter[float] = Counter()
        for page in doc:
            blocks = page.get_text("dict")["blocks"]
            for block in blocks:
                if block["type"] == 0:
                    for line in block["lines"]:
                        for span in line["spans"]:
                            size = round(span["size"], 1)
                            text = span["text"].strip()
                            if text:
                                size_counter[size] += len(text)

        if not size_counter:
            return {"body": 12, "h1": 24, "h2": 18, "h3": 14}

        sorted_sizes = sorted(size_counter.items(), key=lambda x: x[1], reverse=True)
        body_size = sorted_sizes[0][0]
        larger_sizes = sorted(
            [s for s in size_counter if s > body_size + 1], reverse=True
        )

        size_map: dict[str, float] = {"body": body_size}
        if len(larger_sizes) >= 1:
            size_map["h1"] = larger_sizes[0]
        if len(larger_sizes) >= 2:
            size_map["h2"] = larger_sizes[1]
        if len(larger_sizes) >= 3:
            size_map["h3"] = larger_sizes[2]
        return size_map

    def _get_heading_level(self, size: float, size_map: dict, text: str) -> int:
        text = text.strip()
        if len(text) > 80:
            return 0
        if "h1" in size_map and size >= size_map["h1"] - 0.5:
            return 1
        if "h2" in size_map and size >= size_map["h2"] - 0.5:
            return 2
        if "h3" in size_map and size >= size_map["h3"] - 0.5:
            return 3
        return 0

    # ── metadata ──────────────────────────────────────────────────────────

    def _extract_title(self, doc: fitz.Document, size_map: dict) -> str:
        if not doc.page_count:
            return "Untitled"
        page = doc[0]
        blocks = page.get_text("dict")["blocks"]
        max_size = 0.0
        title_text = ""
        for block in blocks:
            if block["type"] == 0:
                for line in block["lines"]:
                    for span in line["spans"]:
                        if span["size"] > max_size and span["text"].strip():
                            max_size = span["size"]
                            title_text = span["text"].strip()
        return title_text or "Untitled"

    def _extract_authors(self, doc: fitz.Document) -> list[str]:
        if not doc.page_count:
            return []
        text = doc[0].get_text() or ""
        lines = text.split("\n")
        authors: list[str] = []
        for line in lines[1:10]:
            line = line.strip()
            if not line:
                continue
            if any(kw in line.lower() for kw in ["university", "department", "abstract", "@", "http"]):
                break
            if re.match(r"^[A-Za-z\s,.\-]+$", line) and len(line) < 200:
                authors.extend(a.strip() for a in line.split(",") if a.strip())
                if authors:
                    break
        return authors

    # ── main section + figure extraction ──────────────────────────────────

    def _extract_sections(
        self,
        doc: fitz.Document,
        size_map: dict,
        images_dir: Path,
    ) -> tuple[list[PaperSection], str]:
        sections: list[PaperSection] = []
        abstract = ""
        current_section: PaperSection | None = None
        content_lines: list[str] = []
        img_counter = [0]

        for page_idx, page in enumerate(doc):
            caption_map = self._collect_captions(page)
            page_figures = self._extract_page_figures(
                doc, page, page_idx, images_dir, caption_map, img_counter
            )
            page_tables = self._extract_page_tables(page)
            page_equations = self._extract_page_equations(page, page_idx)

            blocks = page.get_text("dict")["blocks"]
            for block in blocks:
                if block["type"] != 0:
                    continue
                for line in block["lines"]:
                    line_text = ""
                    line_size = 0.0
                    for span in line["spans"]:
                        line_text += span["text"]
                        line_size = max(line_size, span["size"])
                    line_text = line_text.strip()
                    if not line_text:
                        continue

                    heading_level = self._get_heading_level(line_size, size_map, line_text)

                    if heading_level > 0:
                        if current_section:
                            current_section.content = "\n".join(content_lines).strip()
                            sections.append(current_section)
                            content_lines = []

                        if line_text.lower().startswith("abstract"):
                            current_section = PaperSection(
                                title="Abstract",
                                level=heading_level,
                                content="",
                            )
                        else:
                            current_section = PaperSection(
                                title=line_text,
                                level=heading_level,
                                content="",
                                figures=page_figures[:],
                                tables=page_tables[:],
                                equations=page_equations[:],
                            )
                            page_figures = []
                            page_tables = []
                            page_equations = []
                    else:
                        content_lines.append(line_text)

            if page_figures and current_section:
                current_section.figures.extend(page_figures)
            if page_tables and current_section:
                current_section.tables.extend(page_tables)
            if page_equations and current_section:
                current_section.equations.extend(page_equations)

        if current_section:
            current_section.content = "\n".join(content_lines).strip()
            sections.append(current_section)

        new_sections: list[PaperSection] = []
        for section in sections:
            if section.title.lower() == "abstract":
                abstract = section.content
            else:
                new_sections.append(section)

        return new_sections, abstract

    # ── caption collection ────────────────────────────────────────────────

    def _collect_captions(self, page: fitz.Page) -> dict[str, tuple[str, fitz.Rect]]:
        result: dict[str, tuple[str, fitz.Rect]] = {}
        blocks = page.get_text("dict")["blocks"]
        for block in blocks:
            if block["type"] != 0:
                continue
            block_text = " ".join(
                span["text"]
                for line in block["lines"]
                for span in line["spans"]
            ).strip()
            m = _CAPTION_RE.search(block_text)
            if m:
                key = re.sub(r"\s+", "", m.group(0).lower())
                rect = fitz.Rect(block["bbox"])
                result[key] = (block_text, rect)
        return result

    def _nearest_caption(
        self,
        img_rect: fitz.Rect,
        caption_map: dict[str, tuple[str, fitz.Rect]],
    ) -> str:
        best_dist = float("inf")
        best_cap = ""
        for _key, (cap_text, cap_rect) in caption_map.items():
            dist = abs(cap_rect.y0 - img_rect.y1)
            if dist < best_dist and dist < CAPTION_SEARCH_PX * 2:
                best_dist = dist
                best_cap = cap_text
        return best_cap

    # ── raster + vector figure extraction ─────────────────────────────────

    def _extract_page_figures(
        self,
        doc: fitz.Document,
        page: fitz.Page,
        page_idx: int,
        images_dir: Path,
        caption_map: dict[str, tuple[str, fitz.Rect]],
        counter: list[int],
    ) -> list[PaperFigure]:
        figures: list[PaperFigure] = []
        seen_hashes: set[str] = set()

        page_rect = page.rect
        text_blocks = self._collect_text_blocks(page)
        drawing_rects = self._collect_drawing_rects(page)

        # Pass A: embedded raster images
        raster_rects: list[fitz.Rect] = []
        image_infos = page.get_image_info(xrefs=True) if hasattr(page, "get_image_info") else []
        for img_info in image_infos:
            bbox = fitz.Rect(img_info.get("bbox", (0, 0, 0, 0)))
            xref = int(img_info.get("xref", 0) or 0)
            if xref <= 0:
                continue
            if not self._is_meaningful_image_candidate(page_rect, bbox, img_info):
                continue

            try:
                pix = fitz.Pixmap(doc, xref)
                if pix.width < MIN_IMG_PX or pix.height < MIN_IMG_PX:
                    continue
                if pix.n > 4:
                    pix = fitz.Pixmap(fitz.csRGB, pix)

                img_bytes = pix.tobytes("png")
                h = hashlib.md5(img_bytes).hexdigest()[:12]
                if h in seen_hashes:
                    continue
                seen_hashes.add(h)

                raster_rects.append(bbox)
                counter[0] += 1
                img_path = images_dir / f"fig_{counter[0]:03d}_p{page_idx + 1}.png"
                img_path.write_bytes(img_bytes)

                caption = self._nearest_caption(bbox, caption_map)
                score, flags = self._score_figure_region(
                    bbox, page_rect, text_blocks, drawing_rects + raster_rects, caption_map,
                )
                figures.append(
                    PaperFigure(
                        path=img_path,
                        caption=caption,
                        available=True,
                        page_number=page_idx + 1,
                        bbox=self._rect_tuple(bbox),
                        extraction_method="embedded_image",
                        quality_score=score,
                        review_flags=flags,
                        natural_width=int(pix.width),
                        natural_height=int(pix.height),
                    )
                )
            except Exception:
                continue

        # Pass A.5: render tables as figures
        for tab in self._iter_page_tables(page):
            try:
                tab_bbox = fitz.Rect(tab.bbox)
            except Exception:
                continue
            if tab_bbox.width < MIN_RENDERED_FIGURE_SIDE or tab_bbox.height < MIN_RENDERED_FIGURE_SIDE:
                continue
            clip = fitz.Rect(
                max(page_rect.x0, tab_bbox.x0 - 6),
                max(page_rect.y0, tab_bbox.y0 - 6),
                min(page_rect.x1, tab_bbox.x1 + 6),
                min(page_rect.y1, tab_bbox.y1 + 6),
            )
            if self._is_region_already_covered(clip, raster_rects):
                continue
            try:
                mat = fitz.Matrix(REGION_DPI / 72, REGION_DPI / 72)
                pix = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
                img_bytes = pix.tobytes("png")
                h = hashlib.md5(img_bytes).hexdigest()[:12]
                if h in seen_hashes:
                    continue
                seen_hashes.add(h)

                counter[0] += 1
                img_path = images_dir / f"fig_{counter[0]:03d}_p{page_idx + 1}_table.png"
                img_path.write_bytes(img_bytes)

                caption = self._nearest_caption(clip, caption_map)
                figures.append(
                    PaperFigure(
                        path=img_path,
                        caption=caption,
                        available=True,
                        page_number=page_idx + 1,
                        bbox=self._rect_tuple(clip),
                        extraction_method="table_region",
                        quality_score=0.92,
                        review_flags=[],
                        natural_width=int(pix.width),
                        natural_height=int(pix.height),
                    )
                )
                raster_rects.append(clip)
            except Exception:
                continue

        graphic_rects = drawing_rects + raster_rects

        # Pass B: caption-anchored object-region render
        for cap_text, cap_rect in caption_map.values():
            clip = self._build_caption_anchored_clip(
                page_rect, cap_rect, text_blocks, graphic_rects,
            )
            if clip is None:
                continue
            if self._is_region_already_covered(clip, raster_rects):
                continue

            try:
                mat = fitz.Matrix(REGION_DPI / 72, REGION_DPI / 72)
                pix = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
                img_bytes = pix.tobytes("png")
                h = hashlib.md5(img_bytes).hexdigest()[:12]
                if h in seen_hashes:
                    continue
                seen_hashes.add(h)

                counter[0] += 1
                img_path = images_dir / f"fig_{counter[0]:03d}_p{page_idx + 1}_vec.png"
                img_path.write_bytes(img_bytes)
                score, flags = self._score_figure_region(
                    clip, page_rect, text_blocks, graphic_rects, caption_map,
                )
                figures.append(
                    PaperFigure(
                        path=img_path,
                        caption=cap_text,
                        available=True,
                        page_number=page_idx + 1,
                        bbox=self._rect_tuple(clip),
                        extraction_method="caption_region",
                        quality_score=score,
                        review_flags=flags,
                        natural_width=int(pix.width),
                        natural_height=int(pix.height),
                    )
                )
            except Exception:
                continue

        # Pass C: whole-page fallback
        reliable_methods = {"embedded_image", "table_region", "caption_region"}
        has_reliable = any(
            fig.extraction_method in reliable_methods and fig.quality_score >= 0.45
            for fig in figures
        )
        if caption_map and not has_reliable and not any(fig.quality_score >= 0.45 for fig in figures):
            try:
                mat = fitz.Matrix(RENDER_DPI / 72, RENDER_DPI / 72)
                pix = page.get_pixmap(matrix=mat, alpha=False)
                img_bytes = pix.tobytes("png")
                h = hashlib.md5(img_bytes).hexdigest()[:12]
                if h not in seen_hashes:
                    seen_hashes.add(h)
                    counter[0] += 1
                    img_path = images_dir / f"fig_{counter[0]:03d}_p{page_idx + 1}_page.png"
                    img_path.write_bytes(img_bytes)
                    first_cap = next(iter(caption_map.values()))[0]
                    score, flags = self._score_figure_region(
                        page_rect, page_rect, text_blocks, graphic_rects, caption_map,
                    )
                    figures.append(
                        PaperFigure(
                            path=img_path,
                            caption=first_cap,
                            available=True,
                            page_number=page_idx + 1,
                            bbox=self._rect_tuple(page_rect),
                            extraction_method="page_fallback",
                            quality_score=score,
                            review_flags=flags + ["page_level_fallback"],
                            natural_width=int(pix.width),
                            natural_height=int(pix.height),
                        )
                    )
            except Exception:
                pass

        return figures

    def _collect_text_blocks(self, page: fitz.Page) -> list[dict]:
        blocks: list[dict] = []
        for block in page.get_text("dict").get("blocks", []):
            if block.get("type") != 0:
                continue
            text = " ".join(
                span["text"]
                for line in block.get("lines", [])
                for span in line.get("spans", [])
                if span.get("text", "").strip()
            ).strip()
            if not text:
                continue
            blocks.append({
                "rect": fitz.Rect(block["bbox"]),
                "text": text,
                "char_count": len(text),
                "line_count": len(block.get("lines", [])),
            })
        return blocks

    def _collect_drawing_rects(self, page: fitz.Page) -> list[fitz.Rect]:
        rects: list[fitz.Rect] = []
        if not hasattr(page, "get_drawings"):
            return rects
        try:
            for drawing in page.get_drawings():
                raw_rect = drawing.get("rect")
                if not raw_rect:
                    continue
                rect = fitz.Rect(raw_rect)
                if rect.width < MIN_RENDERED_FIGURE_SIDE / 2 or rect.height < MIN_RENDERED_FIGURE_SIDE / 2:
                    continue
                rects.append(rect)
        except Exception:
            return rects
        return rects

    def _is_meaningful_image_candidate(
        self, page_rect: fitz.Rect, bbox: fitz.Rect, img_info: dict,
    ) -> bool:
        if bbox.is_empty or bbox.width < MIN_RENDERED_FIGURE_SIDE or bbox.height < MIN_RENDERED_FIGURE_SIDE:
            return False
        area_ratio = (bbox.get_area() / page_rect.get_area()) if page_rect.get_area() else 0
        if area_ratio < MIN_GRAPHIC_AREA_RATIO:
            return False
        width = float(img_info.get("width", 0) or 0)
        height = float(img_info.get("height", 0) or 0)
        if width and height:
            if width < MIN_IMG_PX or height < MIN_IMG_PX:
                return False
        return True

    def _build_caption_anchored_clip(
        self,
        page_rect: fitz.Rect,
        caption_rect: fitz.Rect,
        text_blocks: list[dict],
        graphic_rects: list[fitz.Rect],
    ) -> fitz.Rect | None:
        search_top = max(page_rect.y0, caption_rect.y0 - MAX_CAPTION_GAP_PX)
        graphic_candidates = [
            rect for rect in graphic_rects
            if rect.y1 <= caption_rect.y0 + 12
            and rect.y0 >= search_top
            and rect.width >= MIN_RENDERED_FIGURE_SIDE
            and rect.height >= MIN_RENDERED_FIGURE_SIDE / 2
        ]
        if not graphic_candidates:
            return None

        caption_center_x = (caption_rect.x0 + caption_rect.x1) / 2
        seed = min(
            graphic_candidates,
            key=lambda rect: abs(rect.y1 - caption_rect.y0) + abs(rect.x0 + rect.width / 2 - caption_center_x) * 0.35,
        )
        cluster = [seed]
        cluster_rect = fitz.Rect(seed)

        expanded = True
        while expanded:
            expanded = False
            proximity = fitz.Rect(
                cluster_rect.x0 - 32,
                cluster_rect.y0 - 32,
                cluster_rect.x1 + 32,
                min(caption_rect.y0 - 4, cluster_rect.y1 + 32),
            )
            for rect in graphic_candidates:
                if rect in cluster:
                    continue
                if proximity.intersects(rect):
                    cluster.append(rect)
                    cluster_rect.include_rect(rect)
                    expanded = True

        label_zone = fitz.Rect(
            max(page_rect.x0, cluster_rect.x0 - 28),
            max(page_rect.y0, cluster_rect.y0 - 28),
            min(page_rect.x1, cluster_rect.x1 + 28),
            min(caption_rect.y0 - 4, cluster_rect.y1 + 28),
        )
        for block in text_blocks:
            rect = block["rect"]
            if not label_zone.intersects(rect):
                continue
            if block["char_count"] > LABEL_TEXT_CHARS_THRESHOLD and block["line_count"] > 3:
                continue
            cluster_rect.include_rect(rect)

        clip = fitz.Rect(
            max(page_rect.x0, cluster_rect.x0 - 18),
            max(page_rect.y0, cluster_rect.y0 - 18),
            min(page_rect.x1, cluster_rect.x1 + 18),
            min(caption_rect.y0 - 4, cluster_rect.y1 + 18),
        )
        if clip.width < MIN_RENDERED_FIGURE_SIDE or clip.height < MIN_RENDERED_FIGURE_SIDE:
            return None
        return clip

    def _is_region_already_covered(self, region: fitz.Rect, raster_rects: list[fitz.Rect]) -> bool:
        for rect in raster_rects:
            if not region.intersects(rect):
                continue
            overlap = region.intersect(rect).get_area()
            if overlap >= region.get_area() * 0.78:
                return True
        return False

    def _score_figure_region(
        self,
        rect: fitz.Rect,
        page_rect: fitz.Rect,
        text_blocks: list[dict],
        graphic_rects: list[fitz.Rect],
        caption_map: dict[str, tuple[str, fitz.Rect]],
    ) -> tuple[float, list[str]]:
        flags: list[str] = []
        body_text_chars = 0
        graphics_overlap_area = 0.0

        for block in text_blocks:
            block_rect = block["rect"]
            if not rect.intersects(block_rect):
                continue
            overlap = rect.intersect(block_rect).get_area()
            if overlap <= 0:
                continue
            if block["char_count"] >= BODY_TEXT_CHARS_THRESHOLD and block["line_count"] >= 3:
                body_text_chars += block["char_count"]

        for graphic_rect in graphic_rects:
            if not rect.intersects(graphic_rect):
                continue
            graphics_overlap_area += rect.intersect(graphic_rect).get_area()

        graphic_coverage = graphics_overlap_area / rect.get_area() if rect.get_area() else 0.0
        if body_text_chars > 120:
            flags.append("body_text_intrusion")
        if graphic_coverage < 0.08:
            flags.append("low_graphic_coverage")

        score = 0.9
        score -= min(body_text_chars / 500, 0.45)
        score += min(graphic_coverage, 0.25)
        if "low_graphic_coverage" in flags:
            score -= 0.2
        return max(0.0, min(1.0, score)), sorted(set(flags))

    @staticmethod
    def _rect_tuple(rect: fitz.Rect) -> tuple[float, float, float, float]:
        return (round(rect.x0, 2), round(rect.y0, 2), round(rect.x1, 2), round(rect.y1, 2))

    def _write_figure_review_manifest(self, paper: ParsedPaper, images_dir: Path) -> None:
        records = []
        for figure in paper.all_figures():
            records.append({
                "path": str(figure.path),
                "page_number": figure.page_number,
                "caption": figure.caption,
                "bbox": figure.bbox,
                "extraction_method": figure.extraction_method,
                "quality_score": figure.quality_score,
                "review_flags": figure.review_flags,
                "natural_width": figure.natural_width,
                "natural_height": figure.natural_height,
            })
        if not records:
            return
        manifest_path = images_dir / "figure_review.json"
        manifest_path.write_text(
            json.dumps(records, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ── table extraction ──────────────────────────────────────────────────

    def _extract_page_equations(self, page: fitz.Page, page_idx: int) -> list[PaperEquation]:
        """Extract display equations from a PDF page.

        Strategy: scan text blocks for lines that look like numbered or standalone
        equations (centered math, equation numbers like (1), (2), etc.). Convert
        common math symbols to LaTeX approximation.
        """
        equations: list[PaperEquation] = []
        blocks = page.get_text("dict")["blocks"]

        # Patterns for equation numbering: (1), (2), [1], Eq. 1, etc.
        eq_num_re = re.compile(r'\(\s*\d+\s*\)|\[\s*\d+\s*\]|Eq\.?\s*\d+', re.IGNORECASE)

        for block in blocks:
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue

                line_text = "".join(s.get("text", "") for s in spans).strip()
                if not line_text:
                    continue

                # Heuristics for identifying display equations:
                # 1. Has equation number like (1), (2)
                # 2. Contains common math operators/patterns and is short-ish
                # 3. Has italic font (common for math variables in PDFs)
                has_eq_num = bool(eq_num_re.search(line_text))

                # Count italic spans (math variables are typically italic)
                italic_count = sum(1 for s in spans if "Italic" in s.get("font", ""))
                total_spans = len(spans)
                italic_ratio = italic_count / total_spans if total_spans > 0 else 0

                # Math-like content indicators
                math_chars = set("=±×÷∑∏∫∂√∞≈≠≤≥∝∇∈∉⊂⊃∪∩∧∨⟨⟩→←↔αβγδεζηθικλμνξπρστυφχψω")
                math_count = sum(1 for c in line_text if c in math_chars)
                has_operators = any(op in line_text for op in ["=", "+", "−", "-", "×", "÷", "∑", "∫", "∂", "√", "∝"])
                has_fractions = "/" in line_text and any(c.isdigit() for c in line_text)

                is_equation = (
                    has_eq_num
                    or (has_operators and italic_ratio > 0.5 and len(line_text) < 200)
                    or (math_count >= 3 and len(line_text) < 200)
                    or (has_fractions and italic_ratio > 0.3 and len(line_text) < 150)
                )

                if is_equation:
                    # Convert to LaTeX approximation
                    latex = self._text_to_latex(line_text)
                    # Get surrounding context from the page
                    context = ""
                    page_text = page.get_text()
                    idx = page_text.find(line_text[:30]) if len(line_text) > 30 else page_text.find(line_text)
                    if idx >= 0:
                        start = max(0, idx - 80)
                        end = min(len(page_text), idx + len(line_text) + 80)
                        context = page_text[start:end].replace("\n", " ").strip()

                    equations.append(PaperEquation(
                        latex=latex,
                        page_number=page_idx + 1,
                        context=context,
                    ))

        return equations

    @staticmethod
    def _text_to_latex(text: str) -> str:
        """Best-effort conversion of PDF-extracted equation text to LaTeX.

        This is a heuristic approach — perfect LaTeX requires the original source.
        """
        result = text
        # Replace common math symbols with LaTeX
        replacements = [
            ("±", "\\pm "), ("×", "\\times "), ("÷", "\\div "),
            ("∑", "\\sum "), ("∏", "\\prod "), ("∫", "\\int "),
            ("∂", "\\partial "), ("√", "\\sqrt "),
            ("∞", "\\infty "), ("≈", "\\approx "), ("≠", "\\neq "),
            ("≤", "\\leq "), ("≥", "\\geq "), ("∝", "\\propto "),
            ("∇", "\\nabla "), ("∈", "\\in "), ("∉", "\\notin "),
            ("⊂", "\\subset "), ("⊃", "\\supset "),
            ("∪", "\\cup "), ("∩", "\\cap "),
            ("∧", "\\wedge "), ("∨", "\\vee "),
            ("⟨", "\\langle "), ("⟩", "\\rangle "),
            ("→", "\\rightarrow "), ("←", "\\leftarrow "), ("↔", "\\leftrightarrow "),
            # Greek letters
            ("α", "\\alpha "), ("β", "\\beta "), ("γ", "\\gamma "),
            ("δ", "\\delta "), ("ε", "\\epsilon "), ("ζ", "\\zeta "),
            ("η", "\\eta "), ("θ", "\\theta "), ("ι", "\\iota "),
            ("κ", "\\kappa "), ("λ", "\\lambda "), ("μ", "\\mu "),
            ("ν", "\\nu "), ("ξ", "\\xi "), ("π", "\\pi "),
            ("ρ", "\\rho "), ("σ", "\\sigma "), ("τ", "\\tau "),
            ("υ", "\\upsilon "), ("φ", "\\phi "), ("χ", "\\chi "),
            ("ψ", "\\psi "), ("ω", "\\omega "),
        ]
        for old, new in replacements:
            result = result.replace(old, new)
        # Wrap in $...$ for inline or $...$ for display
        # Wrap in $...$ for inline or $$...$$ for display
        eq_num_re = re.compile(r'\(\s*\d+\s*\)\s*$')
        has_num = bool(eq_num_re.search(result))
        result = eq_num_re.sub("", result).strip()
        if has_num:
            return f"$${result}$$"
        return f"${result}$"

    def _extract_page_tables(self, page: fitz.Page) -> list[PaperTable]:
        tables: list[PaperTable] = []
        if not hasattr(page, "find_tables"):
            return tables
        try:
            for tab in tab_finder.tables:
                rows = tab.extract()
                if not rows:
                    continue
                md = self._rows_to_markdown(rows)
                if md:
                    tables.append(PaperTable(markdown=md))
        except Exception:
            pass
        return tables

    def _iter_page_tables(self, page: fitz.Page):
        if not hasattr(page, "find_tables"):
            return
        try:
            tab_finder = page.find_tables()
            for tab in tab_finder.tables:
                if getattr(tab, "bbox", None) is None:
                    continue
                yield tab
        except Exception:
            return

    @staticmethod
    def _rows_to_markdown(rows: list[list[str | None]]) -> str:
        if not rows:
            return ""
        header = [str(c or "") for c in rows[0]]
        sep = ["---"] * len(header)
        lines = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(sep) + " |",
        ]
        for row in rows[1:]:
            cells = [str(c or "") for c in row]
            while len(cells) < len(header):
                cells.append("")
            lines.append("| " + " | ".join(cells[:len(header)]) + " |")
        return "\n".join(lines)

    def _extract_page_tables(self, page: fitz.Page) -> list[PaperTable]:
        tables: list[PaperTable] = []
        if not hasattr(page, "find_tables"):
            return tables
        try:
            tab_finder = page.find_tables()
            for tab in tab_finder.tables:
                rows = tab.extract()
                if not rows:
                    continue
                md = self._rows_to_markdown(rows)
                if md:
                    tables.append(PaperTable(markdown=md))
        except Exception:
            pass
        return tables

    def _iter_page_tables(self, page: fitz.Page):
        if not hasattr(page, "find_tables"):
            return
        try:
            tab_finder = page.find_tables()
            for tab in tab_finder.tables:
                if getattr(tab, "bbox", None) is None:
                    continue
                yield tab
        except Exception:
            return

    @staticmethod
    def _rows_to_markdown(rows: list[list[str | None]]) -> str:
        if not rows:
            return ""
        header = [str(c or "") for c in rows[0]]
        sep = ["---"] * len(header)
        lines = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(sep) + " |",
        ]
        for row in rows[1:]:
            cells = [str(c or "") for c in row]
            while len(cells) < len(header):
                cells.append("")
            lines.append("| " + " | ".join(cells[:len(header)]) + " |")
        return "\n".join(lines)

    def _extract_page_tables(self, page: fitz.Page) -> list[PaperTable]:
        tables: list[PaperTable] = []
        if not hasattr(page, "find_tables"):
            return tables
        try:
            tab_finder = page.find_tables()
            for tab in tab_finder.tables:
                rows = tab.extract()
                if not rows:
                    continue
                md = self._rows_to_markdown(rows)
                if md:
                    tables.append(PaperTable(markdown=md))
        except Exception:
            pass
        return tables

    def _iter_page_tables(self, page: fitz.Page):
        if not hasattr(page, "find_tables"):
            return
        try:
            tab_finder = page.find_tables()
            for tab in tab_finder.tables:
                if getattr(tab, "bbox", None) is None:
                    continue
                yield tab
        except Exception:
            return

    @staticmethod
    def _rows_to_markdown(rows: list[list[str | None]]) -> str:
        if not rows:
            return ""
        header = [str(c or "") for c in rows[0]]
        sep = ["---"] * len(header)
        lines = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(sep) + " |",
        ]
        for row in rows[1:]:
            cells = [str(c or "") for c in row]
            while len(cells) < len(header):
                cells.append("")
            lines.append("| " + " | ".join(cells[:len(header)]) + " |")
        return "\n".join(lines)

    def _extract_page_tables(self, page: fitz.Page) -> list[PaperTable]:
        tables: list[PaperTable] = []
        if not hasattr(page, "find_tables"):
            return tables
        try:
            tab_finder = page.find_tables()
            for tab in tab_finder.tables:
                rows = tab.extract()
                if not rows:
                    continue
                md = self._rows_to_markdown(rows)
                if md:
                    tables.append(PaperTable(markdown=md))
        except Exception:
            pass
        return tables

    def _iter_page_tables(self, page: fitz.Page):
        if not hasattr(page, "find_tables"):
            return
        try:
            tab_finder = page.find_tables()
            for tab in tab_finder.tables:
                if getattr(tab, "bbox", None) is None:
                    continue
                yield tab
        except Exception:
            return

    @staticmethod
    def _rows_to_markdown(rows: list[list[str | None]]) -> str:
        if not rows:
            return ""
        header = [str(c or "") for c in rows[0]]
        sep = ["---"] * len(header)
        lines = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(sep) + " |",
        ]
        for row in rows[1:]:
            cells = [str(c or "") for c in row]
            while len(cells) < len(header):
                cells.append("")
            lines.append("| " + " | ".join(cells[:len(header)]) + " |")
        return "\n".join(lines)

    def _extract_page_tables(self, page: fitz.Page) -> list[PaperTable]:
        tables: list[PaperTable] = []
        if not hasattr(page, "find_tables"):
            return tables
        try:
            tab_finder = page.find_tables()
            for tab in tab_finder.tables:
                rows = tab.extract()
                if not rows:
                    continue
                md = self._rows_to_markdown(rows)
                if md:
                    tables.append(PaperTable(markdown=md))
        except Exception:
            pass
        return tables

    def _iter_page_tables(self, page: fitz.Page):
        if not hasattr(page, "find_tables"):
            return
        try:
            tab_finder = page.find_tables()
            for tab in tab_finder.tables:
                if getattr(tab, "bbox", None) is None:
                    continue
                yield tab
        except Exception:
            return

    @staticmethod
    def _rows_to_markdown(rows: list[list[str | None]]) -> str:
        if not rows:
            return ""
        header = [str(c or "") for c in rows[0]]
        sep = ["---"] * len(header)
        lines = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(sep) + " |",
        ]
        for row in rows[1:]:
            cells = [str(c or "") for c in row]
            while len(cells) < len(header):
                cells.append("")
            lines.append("| " + " | ".join(cells[:len(header)]) + " |")
        return "\n".join(lines)

    def _extract_page_tables(self, page: fitz.Page) -> list[PaperTable]:
        tables: list[PaperTable] = []
        if not hasattr(page, "find_tables"):
            return tables
        try:
            tab_finder = page.find_tables()
            for tab in tab_finder.tables:
                rows = tab.extract()
                if not rows:
                    continue
                md = self._rows_to_markdown(rows)
                if md:
                    tables.append(PaperTable(markdown=md))
        except Exception:
            pass
        return tables

    def _iter_page_tables(self, page: fitz.Page):
        if not hasattr(page, "find_tables"):
            return
        try:
            tab_finder = page.find_tables()
            for tab in tab_finder.tables:
                if getattr(tab, "bbox", None) is None:
                    continue
                yield tab
        except Exception:
            return

    @staticmethod
    def _rows_to_markdown(rows: list[list[str | None]]) -> str:
        if not rows:
            return ""
        header = [str(c or "") for c in rows[0]]
        sep = ["---"] * len(header)
        lines = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(sep) + " |",
        ]
        for row in rows[1:]:
            cells = [str(c or "") for c in row]
            while len(cells) < len(header):
                cells.append("")
            lines.append("| " + " | ".join(cells[:len(header)]) + " |")
        return "\n".join(lines)

    def _extract_page_tables(self, page: fitz.Page) -> list[PaperTable]:
        tables: list[PaperTable] = []
        if not hasattr(page, "find_tables"):
            return tables
        try:
            tab_finder = page.find_tables()
            for tab in tab_finder.tables:
                rows = tab.extract()
                if not rows:
                    continue
                md = self._rows_to_markdown(rows)
                if md:
                    tables.append(PaperTable(markdown=md))
        except Exception:
            pass
        return tables

    def _iter_page_tables(self, page: fitz.Page):
        if not hasattr(page, "find_tables"):
            return
        try:
            tab_finder = page.find_tables()
            for tab in tab_finder.tables:
                if getattr(tab, "bbox", None) is None:
                    continue
                yield tab
        except Exception:
            return

    @staticmethod
    def _rows_to_markdown(rows: list[list[str | None]]) -> str:
        if not rows:
            return ""
        header = [str(c or "") for c in rows[0]]
        sep = ["---"] * len(header)
        lines = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(sep) + " |",
        ]
        for row in rows[1:]:
            cells = [str(c or "") for c in row]
            while len(cells) < len(header):
                cells.append("")
            lines.append("| " + " | ".join(cells[:len(header)]) + " |")
        return "\n".join(lines)

    def _extract_page_tables(self, page: fitz.Page) -> list[PaperTable]:
        tables: list[PaperTable] = []
        if not hasattr(page, "find_tables"):
            return tables
        try:
            tab_finder = page.find_tables()
            for tab in tab_finder.tables:
                rows = tab.extract()
                if not rows:
                    continue
                md = self._rows_to_markdown(rows)
                if md:
                    tables.append(PaperTable(markdown=md))
        except Exception:
            pass
        return tables

    def _iter_page_tables(self, page: fitz.Page):
        if not hasattr(page, "find_tables"):
            return
        try:
            tab_finder = page.find_tables()
            for tab in tab_finder.tables:
                if getattr(tab, "bbox", None) is None:
                    continue
                yield tab
        except Exception:
            return

    @staticmethod
    def _rows_to_markdown(rows: list[list[str | None]]) -> str:
        if not rows:
            return ""
        header = [str(c or "") for c in rows[0]]
        sep = ["---"] * len(header)
        lines = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(sep) + " |",
        ]
        for row in rows[1:]:
            cells = [str(c or "") for c in row]
            while len(cells) < len(header):
                cells.append("")
            lines.append("| " + " | ".join(cells[:len(header)]) + " |")
        return "\n".join(lines)

    def _extract_page_tables(self, page: fitz.Page) -> list[PaperTable]:
        tables: list[PaperTable] = []
        if not hasattr(page, "find_tables"):
            return tables
        try:
            tab_finder = page.find_tables()
            for tab in tab_finder.tables:
                rows = tab.extract()
                if not rows:
                    continue
                md = self._rows_to_markdown(rows)
                if md:
                    tables.append(PaperTable(markdown=md))
        except Exception:
            pass
        return tables

    def _iter_page_tables(self, page: fitz.Page):
        if not hasattr(page, "find_tables"):
            return
        try:
            tab_finder = page.find_tables()
            for tab in tab_finder.tables:
                if getattr(tab, "bbox", None) is None:
                    continue
                yield tab
        except Exception:
            return

    @staticmethod
    def _rows_to_markdown(rows: list[list[str | None]]) -> str:
        if not rows:
            return ""
        header = [str(c or "") for c in rows[0]]
        sep = ["---"] * len(header)
        lines = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(sep) + " |",
        ]
        for row in rows[1:]:
            cells = [str(c or "") for c in row]
            while len(cells) < len(header):
                cells.append("")
            lines.append("| " + " | ".join(cells[:len(header)]) + " |")
        return "\n".join(lines)

    def _extract_page_tables(self, page: fitz.Page) -> list[PaperTable]:
        tables: list[PaperTable] = []
        if not hasattr(page, "find_tables"):
            return tables
        try:
            tab_finder = page.find_tables()
            for tab in tab_finder.tables:
                rows = tab.extract()
                if not rows:
                    continue
                md = self._rows_to_markdown(rows)
                if md:
                    tables.append(PaperTable(markdown=md))
        except Exception:
            pass
        return tables

    def _iter_page_tables(self, page: fitz.Page):
        if not hasattr(page, "find_tables"):
            return
        try:
            tab_finder = page.find_tables()
            for tab in tab_finder.tables:
                if getattr(tab, "bbox", None) is None:
                    continue
                yield tab
        except Exception:
            return

    @staticmethod
    def _rows_to_markdown(rows: list[list[str | None]]) -> str:
        if not rows:
            return ""
        header = [str(c or "") for c in rows[0]]
        sep = ["---"] * len(header)
        lines = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(sep) + " |",
        ]
        for row in rows[1:]:
            cells = [str(c or "") for c in row]
            while len(cells) < len(header):
                cells.append("")
            lines.append("| " + " | ".join(cells[:len(header)]) + " |")
        return "\n".join(lines)

    def _extract_page_tables(self, page: fitz.Page) -> list[PaperTable]:
        tables: list[PaperTable] = []
        if not hasattr(page, "find_tables"):
            return tables
        try:
            tab_finder = page.find_tables()
            for tab in tab_finder.tables:
                rows = tab.extract()
                if not rows:
                    continue
                md = self._rows_to_markdown(rows)
                if md:
                    tables.append(PaperTable(markdown=md))
        except Exception:
            pass
        return tables

    def _iter_page_tables(self, page: fitz.Page):
        if not hasattr(page, "find_tables"):
            return
        try:
            tab_finder = page.find_tables()
            for tab in tab_finder.tables:
                if getattr(tab, "bbox", None) is None:
                    continue
                yield tab
        except Exception:
            return

    @staticmethod
    def _rows_to_markdown(rows: list[list[str | None]]) -> str:
        if not rows:
            return ""
        header = [str(c or "") for c in rows[0]]
        sep = ["---"] * len(header)
        lines = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(sep) + " |",
        ]
        for row in rows[1:]:
            cells = [str(c or "") for c in row]
            while len(cells) < len(header):
                cells.append("")
            lines.append("| " + " | ".join(cells[:len(header)]) + " |")
        return "\n".join(lines)

    def _extract_page_tables(self, page: fitz.Page) -> list[PaperTable]:
        tables: list[PaperTable] = []
        if not hasattr(page, "find_tables"):
            return tables
        try:
            tab_finder = page.find_tables()
            for tab in tab_finder.tables:
                rows = tab.extract()
                if not rows:
                    continue
                md = self._rows_to_markdown(rows)
                if md:
                    tables.append(PaperTable(markdown=md))
        except Exception:
            pass
        return tables

    def _iter_page_tables(self, page: fitz.Page):
        if not hasattr(page, "find_tables"):
            return
        try:
            tab_finder = page.find_tables()
            for tab in tab_finder.tables:
                if getattr(tab, "bbox", None) is None:
                    continue
                yield tab
        except Exception:
            return

    @staticmethod
    def _rows_to_markdown(rows: list[list[str | None]]) -> str:
        if not rows:
            return ""
        header = [str(c or "") for c in rows[0]]
        sep = ["---"] * len(header)
        lines = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(sep) + " |",
        ]
        for row in rows[1:]:
            cells = [str(c or "") for c in row]
            while len(cells) < len(header):
                cells.append("")
            lines.append("| " + " | ".join(cells[:len(header)]) + " |")
        return "\n".join(lines)

    def _extract_page_tables(self, page: fitz.Page) -> list[PaperTable]:
        tables: list[PaperTable] = []
        if not hasattr(page, "find_tables"):
            return tables
        try:
            tab_finder = page.find_tables()
            for tab in tab_finder.tables:
                rows = tab.extract()
                if not rows:
                    continue
                md = self._rows_to_markdown(rows)
                if md:
                    tables.append(PaperTable(markdown=md))
        except Exception:
            pass
        return tables

    def _iter_page_tables(self, page: fitz.Page):
        if not hasattr(page, "find_tables"):
            return
        try:
            tab_finder = page.find_tables()
            for tab in tab_finder.tables:
                if getattr(tab, "bbox", None) is None:
                    continue
                yield tab
        except Exception:
            return

    @staticmethod
    def _rows_to_markdown(rows: list[list[str | None]]) -> str:
        if not rows:
            return ""
        header = [str(c or "") for c in rows[0]]
        sep = ["---"] * len(header)
        lines = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(sep) + " |",
        ]
        for row in rows[1:]:
            cells = [str(c or "") for c in row]
            while len(cells) < len(header):
                cells.append("")
            lines.append("| " + " | ".join(cells[:len(header)]) + " |")
        return "\n".join(lines)

    def _extract_page_tables(self, page: fitz.Page) -> list[PaperTable]:
        tables: list[PaperTable] = []
        if not hasattr(page, "find_tables"):
            return tables
        try:
            tab_finder = page.find_tables()
            for tab in tab_finder.tables:
                rows = tab.extract()
                if not rows:
                    continue
                md = self._rows_to_markdown(rows)
                if md:
                    tables.append(PaperTable(markdown=md))
        except Exception:
            pass
        return tables

    def _iter_page_tables(self, page: fitz.Page):
        if not hasattr(page, "find_tables"):
            return
        try:
            tab_finder = page.find_tables()
            for tab in tab_finder.tables:
                if getattr(tab, "bbox", None) is None:
                    continue
                yield tab
        except Exception:
            return

    @staticmethod
    def _rows_to_markdown(rows: list[list[str | None]]) -> str:
        if not rows:
            return ""
        header = [str(c or "") for c in rows[0]]
        sep = ["---"] * len(header)
        lines = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(sep) + " |",
        ]
        for row in rows[1:]:
            cells = [str(c or "") for c in row]
            while len(cells) < len(header):
                cells.append("")
            lines.append("| " + " | ".join(cells[:len(header)]) + " |")
        return "\n".join(lines)

    def _extract_page_tables(self, page: fitz.Page) -> list[PaperTable]:
        tables: list[PaperTable] = []
        if not hasattr(page, "find_tables"):
            return tables
        try:
            tab_finder = page.find_tables()
            for tab in tab_finder.tables:
                rows = tab.extract()
                if not rows:
                    continue
                md = self._rows_to_markdown(rows)
                if md:
                    tables.append(PaperTable(markdown=md))
        except Exception:
            pass
        return tables

    def _iter_page_tables(self, page: fitz.Page):
        if not hasattr(page, "find_tables"):
            return
        try:
            tab_finder = page.find_tables()
            for tab in tab_finder.tables:
                if getattr(tab, "bbox", None) is None:
                    continue
                yield tab
        except Exception:
            return

    @staticmethod
    def _rows_to_markdown(rows: list[list[str | None]]) -> str:
        if not rows:
            return ""
        header = [str(c or "") for c in rows[0]]
        sep = ["---"] * len(header)
        lines = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(sep) + " |",
        ]
        for row in rows[1:]:
            cells = [str(c or "") for c in row]
            while len(cells) < len(header):
                cells.append("")
            lines.append("| " + " | ".join(cells[:len(header)]) + " |")
        return "\n".join(lines)

    def _extract_page_tables(self, page: fitz.Page) -> list[PaperTable]:
        tables: list[PaperTable] = []
        if not hasattr(page, "find_tables"):
            return tables
        try:
            tab_finder = page.find_tables()
            for tab in tab_finder.tables:
                rows = tab.extract()
                if not rows:
                    continue
                md = self._rows_to_markdown(rows)
                if md:
                    tables.append(PaperTable(markdown=md))
        except Exception:
            pass
        return tables

    def _iter_page_tables(self, page: fitz.Page):
        if not hasattr(page, "find_tables"):
            return
        try:
            tab_finder = page.find_tables()
            for tab in tab_finder.tables:
                if getattr(tab, "bbox", None) is None:
                    continue
                yield tab
        except Exception:
            return

    @staticmethod
    def _rows_to_markdown(rows: list[list[str | None]]) -> str:
        if not rows:
            return ""
        header = [str(c or "") for c in rows[0]]
        sep = ["---"] * len(header)
        lines = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(sep) + " |",
        ]
        for row in rows[1:]:
            cells = [str(c or "") for c in row]
            while len(cells) < len(header):
                cells.append("")
            lines.append("| " + " | ".join(cells[:len(header)]) + " |")
        return "\n".join(lines)
