"""Main generation pipeline: 6-stage paper-to-PPT workflow.

Stages: parse → research → (optional pause) → strategy → generation → postprocess → export
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.config import CANVAS_FORMATS, settings


@dataclass
class ProgressEvent:
    """Emitted at every stage transition."""

    stage: str
    status: str  # "started" | "progress" | "complete" | "error"
    message: str
    progress: float  # 0.0–1.0
    data: dict | None = None


@dataclass
class GenerationRequest:
    """Input parameters for a full paper-to-PPT run."""

    file_path: Path
    source_type: str = "pdf"
    provider: str = "doubao"
    model: str = "doubao-seed-2-0-lite-260215"
    api_key: str = ""
    base_url: str | None = None
    canvas_format: str = "ppt169"
    style: str = "academic"
    num_pages: int | None = None
    instruction: str = ""
    language: str = "zh"
    detail_level: str = "normal"
    timeout_seconds: int | None = None
    style_overrides: dict | None = None
    enable_visual_critic: bool = False
    confirm_outline: bool = True
    quick_mode: bool = False
    speech_minutes: int | None = None
    theme_color: str | None = None


@dataclass
class RefineRequest:
    """Parameters for a feedback-driven iteration run."""

    project_dir: Path
    feedback: str
    feedback_history: list[str]
    job_id: str
    parent_job_id: str | None = None
    provider: str = "doubao"
    model: str = "doubao-seed-2-0-lite-260215"
    api_key: str = ""
    base_url: str | None = None
    canvas_format: str = "ppt169"
    style: str = "academic"
    language: str = "zh"
    detail_level: str = "normal"
    instruction: str = ""
    style_overrides: dict | None = None
    target_pages: list[int] | None = None
    allow_structure_changes: bool = False
    enable_visual_critic: bool = False


async def run_pipeline(request: GenerationRequest) -> AsyncIterator[ProgressEvent]:
    """Run the full 6-stage pipeline: parse → research → strategy → generation → postprocess → export."""
    from backend.llm import create_provider
    from backend.orchestrator.research_agent import analyze_paper
    from backend.orchestrator.strategist_agent import create_design_spec
    from backend.orchestrator.svg_executor import generate_svg_pages
    from backend.parser.pdf_parser import PDFParser
    from backend.parser.word_parser import WordParser

    suffix = request.file_path.suffix.lower()

    # Stage 1: Parse
    if suffix == ".docx":
        yield ProgressEvent("parsing", "started", "Parsing Word document...", 0.05)
        parser = WordParser()
    else:
        yield ProgressEvent("parsing", "started", "Parsing PDF...", 0.05)
        parser = PDFParser()
    project_dir = settings.workspaces_dir / f"job_{id(request)}"
    from backend.generator.project_manager import init_project
    init_project(project_dir)

    try:
        paper = await parser.parse(request.file_path, project_dir)
    except Exception as exc:
        yield ProgressEvent("parsing", "error", f"Parse failed: {exc}", 0.05)
        return

    yield ProgressEvent("parsing", "complete", f"Parsed: {paper.title}, {len(paper.sections)} sections", 0.15)

    # Stage 2: Research
    yield ProgressEvent("research", "started", "Analyzing paper and generating manuscript...", 0.2)
    llm = create_provider(request.provider, request.api_key, base_url=request.base_url)

    try:
        manuscript = await analyze_paper(
            paper, llm, request.model,
            instruction=request.instruction,
            num_pages=request.num_pages,
            language=request.language,
            detail_level=request.detail_level,
        )
    except Exception as exc:
        yield ProgressEvent("research", "error", f"Research failed: {exc}", 0.2)
        return

    # Save manuscript
    (project_dir / "manuscript.md").write_text(manuscript, encoding="utf-8")

    # If outline confirmation is requested, pause here
    if request.confirm_outline:
        yield ProgressEvent(
            "research", "complete",
            "Manuscript generated. Waiting for user confirmation...",
            0.35,
            data={"manuscript": manuscript, "paused": True},
        )
        return

    # Continue with stages 3-6
    async for event in _run_stages_3_to_6(
        manuscript=manuscript,
        paper=paper,
        project_dir=project_dir,
        llm=llm,
        request=request,
    ):
        yield event


async def continue_pipeline(
    job_id: str,
    manuscript: str,
    request: GenerationRequest,
) -> AsyncIterator[ProgressEvent]:
    """Resume pipeline after user confirms/edits the outline."""
    from backend.llm import create_provider
    from backend.parser.pdf_parser import PDFParser
    from backend.parser.word_parser import WordParser

    llm = create_provider(request.provider, request.api_key, base_url=request.base_url)
    project_dir = settings.workspaces_dir / f"job_{job_id}"

    # Re-parse paper for figure inventory
    try:
        suffix = request.file_path.suffix.lower()
        parser = WordParser() if suffix == ".docx" else PDFParser()
        paper = await parser.parse(request.file_path, project_dir)
    except Exception:
        paper = None

    async for event in _run_stages_3_to_6(
        manuscript=manuscript,
        paper=paper,
        project_dir=project_dir,
        llm=llm,
        request=request,
    ):
        yield event


async def _run_stages_3_to_6(
    manuscript: str,
    paper: Any,
    project_dir: Path,
    llm: Any,
    request: GenerationRequest,
    target_pages: set[int] | None = None,
) -> AsyncIterator[ProgressEvent]:
    """Shared stages 3-6: strategy → generation → postprocess → export."""
    from backend.orchestrator.strategist_agent import create_design_spec
    from backend.orchestrator.svg_executor import generate_svg_pages, generate_speaker_notes
    from backend.orchestrator.manuscript import split_manuscript_pages

    # Stage 3: Strategy (reuse existing design_spec if only refining specific pages)
    existing_spec_path = project_dir / "design_spec.md"
    if target_pages and existing_spec_path.exists():
        design_spec = existing_spec_path.read_text(encoding="utf-8")
        yield ProgressEvent("strategy", "complete", "Reusing existing design specification", 0.5)
    else:
        yield ProgressEvent("strategy", "started", "Creating design specification...", 0.4)

        try:
            design_spec = await create_design_spec(
                manuscript, llm, request.model,
                canvas_format=request.canvas_format,
                style=request.style,
                language=request.language,
                detail_level=request.detail_level,
                style_overrides=request.style_overrides,
            )
        except Exception as exc:
            yield ProgressEvent("strategy", "error", f"Strategy failed: {exc}", 0.4)
            return

        (project_dir / "design_spec.md").write_text(design_spec, encoding="utf-8")
        yield ProgressEvent("strategy", "complete", "Design specification created", 0.5)

    # Stage 4: SVG Generation
    pages = split_manuscript_pages(manuscript)
    total_pages = len(pages)
    gen_count = len(target_pages) if target_pages else total_pages
    page_label = f"{gen_count} slides" + (f" (pages {sorted(target_pages)})" if target_pages else "")
    mode_label = "in parallel" if request.quick_mode else "sequentially"
    yield ProgressEvent("generation", "started", f"Regenerating {page_label} {mode_label}...", 0.55)

    # Build figure inventory
    figure_inventory = None
    if paper:
        figure_inventory = [
            {
                "path": str(fig.path),
                "caption": fig.caption,
                "page_number": fig.page_number,
                "natural_width": fig.natural_width,
                "natural_height": fig.natural_height,
            }
            for fig in paper.all_figures()
            if fig.available
        ]

    generated_pages = 0
    try:
        async for page_num, svg_content in generate_svg_pages(
            design_spec, manuscript, project_dir, llm, request.model,
            style=request.style,
            language=request.language,
            detail_level=request.detail_level,
            figure_inventory=figure_inventory,
            quick_mode=request.quick_mode,
            target_pages=target_pages,
        ):
            generated_pages += 1
            progress = 0.55 + 0.35 * (generated_pages / total_pages)
            yield ProgressEvent(
                "generation", "progress",
                f"Generated slide {generated_pages}/{total_pages}",
                progress,
            )
    except Exception as exc:
        yield ProgressEvent("generation", "error", f"Generation failed: {exc}", 0.55)
        return

    yield ProgressEvent("generation", "complete", f"Generated {generated_pages} slides", 0.9)

    # Stage 4.5 + 5: Speaker Notes and PostProcess run in parallel
    _paper_text = paper.to_markdown() if paper and hasattr(paper, 'to_markdown') else ""
    notes_task = asyncio.create_task(
        generate_speaker_notes(
            manuscript, project_dir, llm, request.model,
            language=request.language,
            paper_text=_paper_text,
            speech_minutes=request.speech_minutes,
        )
    ) if not request.quick_mode else None

    # Stage 5: PostProcess (runs while notes are being generated)
    yield ProgressEvent("postprocess", "started", "Post-processing slides...", 0.92)
    from backend.generator.project_manager import prepare_for_finalize
    prepare_for_finalize(project_dir)

    try:
        from backend.generator.svg_finalize.finalize import finalize_project
        stats = finalize_project(project_dir)
        yield ProgressEvent("postprocess", "complete", f"Post-processing complete (steps: {stats})", 0.95)
    except Exception as exc:
        yield ProgressEvent("postprocess", "complete", f"Post-processing skipped ({exc})", 0.95)

    # Await notes result (may already be done if postprocess took longer)
    yield ProgressEvent("notes", "started", "Generating speaker notes...", 0.91)
    if notes_task:
        try:
            notes = await notes_task
            yield ProgressEvent("notes", "complete", f"Generated notes for {len(notes)} slides", 0.93)
        except Exception as exc:
            yield ProgressEvent("notes", "complete", f"Notes generation skipped ({exc})", 0.93)
    else:
        # In quick mode, generate notes after postprocess
        try:
            notes = await generate_speaker_notes(
                manuscript, project_dir, llm, request.model,
                language=request.language,
                paper_text=_paper_text,
                speech_minutes=request.speech_minutes,
            )
            yield ProgressEvent("notes", "complete", f"Generated notes for {len(notes)} slides", 0.93)
        except Exception as exc:
            yield ProgressEvent("notes", "complete", f"Notes generation skipped ({exc})", 0.93)

    # Stage 6: Export
    yield ProgressEvent("export", "started", "Exporting PPTX...", 0.96)
    try:
        output_path = await _export_pptx(project_dir, request.canvas_format)
        yield ProgressEvent(
            "export", "complete",
            "Presentation ready!",
            1.0,
            data={"output_path": str(output_path), "project_dir": str(project_dir)},
        )
    except Exception as exc:
        yield ProgressEvent("export", "error", f"Export failed: {exc}", 0.96)


async def _export_pptx(project_dir: Path, canvas_format: str = "ppt169") -> Path:
    """Export SVG files to PPTX."""
    import sys

    # Ensure svg_to_pptx is importable
    project_root = str(Path(__file__).resolve().parent.parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    from backend.config import CANVAS_FORMATS
    from backend.generator.project_manager import get_svg_files, get_notes
    from backend.generator.svg_to_pptx import create_pptx

    svg_files = get_svg_files(project_dir, "final") or get_svg_files(project_dir, "output")
    if not svg_files:
        raise RuntimeError("No SVG files found for export")

    notes = get_notes(project_dir, svg_files)
    output_path = project_dir / "output.pptx"

    fmt = CANVAS_FORMATS.get(canvas_format, CANVAS_FORMATS["ppt169"])
    create_pptx(
        svg_files,
        output_path,
        notes=notes,
        width_px=fmt["width"],
        height_px=fmt["height"],
    )
    return output_path


async def run_refine_pipeline(request: RefineRequest) -> AsyncIterator[ProgressEvent]:
    """Run a refine iteration on an existing project."""
    from backend.llm import create_provider
    from backend.orchestrator.research_agent import revise_manuscript
    from backend.orchestrator.strategist_agent import create_design_spec
    from backend.orchestrator.svg_executor import generate_svg_pages, generate_speaker_notes
    from backend.orchestrator.manuscript import split_manuscript_pages

    llm = create_provider(request.provider, request.api_key, base_url=request.base_url)
    project_dir = request.project_dir

    # Load existing manuscript, or synthesize one from SVG filenames
    manuscript_path = project_dir / "manuscript.md"
    if manuscript_path.exists():
        original_manuscript = manuscript_path.read_text(encoding="utf-8")
    else:
        # Build a minimal manuscript from existing SVG files
        from backend.generator.project_manager import get_svg_files
        svg_files = get_svg_files(project_dir, "final") or get_svg_files(project_dir, "output")
        if not svg_files:
            yield ProgressEvent("refine", "error", "No slides found to refine", 0.0)
            return
        pages = []
        for svg_path in svg_files:
            parts = svg_path.stem.split("_", 1)
            title = parts[1].replace("_", " ") if len(parts) > 1 else svg_path.stem
            pages.append(f"## {title}\n\n(Slide content)")
        original_manuscript = "\n\n---\n\n".join(pages)
        # Save it so next refine doesn't need to rebuild
        manuscript_path.write_text(original_manuscript, encoding="utf-8")

    # Revise manuscript
    yield ProgressEvent("research", "started", "Revising manuscript with feedback...", 0.2)
    try:
        revised_manuscript = await revise_manuscript(
            original_manuscript, llm, request.model,
            feedback_history=request.feedback_history,
            language=request.language,
            detail_level=request.detail_level,
            target_pages=request.target_pages or [],
            allow_structure_changes=request.allow_structure_changes,
        )
    except Exception as exc:
        yield ProgressEvent("research", "error", f"Manuscript revision failed: {exc}", 0.2)
        return

    # Save revised manuscript
    (project_dir / "manuscript.md").write_text(revised_manuscript, encoding="utf-8")
    yield ProgressEvent("research", "complete", "Manuscript revised", 0.35)

    # Run stages 3-6 with revised manuscript
    gen_request = GenerationRequest(
        file_path=Path(""),
        provider=request.provider,
        model=request.model,
        api_key=request.api_key,
        base_url=request.base_url,
        canvas_format=request.canvas_format,
        style=request.style,
        language=request.language,
        detail_level=request.detail_level,
        style_overrides=request.style_overrides,
        confirm_outline=False,
    )

    # Only regenerate pages that were targeted by feedback
    refine_target_pages = set(request.target_pages) if request.target_pages else None

    async for event in _run_stages_3_to_6(
        manuscript=revised_manuscript,
        paper=None,
        project_dir=project_dir,
        llm=llm,
        request=gen_request,
        target_pages=refine_target_pages,
    ):
        yield event
