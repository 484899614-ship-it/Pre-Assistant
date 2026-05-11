"""SVG Executor agent: generates SVG page code from design spec.

The executor runs a page-by-page generation loop. Each page is checked by
the static svg_critic before being accepted. If the critic finds violations,
a targeted repair prompt is fed back to the LLM (bounded retries, with
slightly lower temperature on each retry).

After all pages are generated, a batch speaker-notes pass produces per-page
notes saved as ``notes.json`` in the project directory.
"""

from __future__ import annotations

import json
import logging
import re
import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

from backend.config import settings

logger = logging.getLogger(__name__)
from backend.generator.svg_critic import CriticConfig, CriticReport, Violation, check_svg
from backend.llm import LLMMessage, LLMProvider, LLMResponse
from backend.orchestrator.manuscript import split_manuscript_pages
from backend.orchestrator.provider_guidance import (
    deepseek_executor_guidance,
    is_deepseek_provider,
)

FIG_TOKEN_RE = re.compile(r"\[\[FIG:([A-Za-z0-9_\-]+)\]\]")
IMAGE_HREF_RE = re.compile(r"<image\b[^>]*\bhref=[\"']([^\"']+)[\"']", re.IGNORECASE)
FIGURE_LABEL_RE = re.compile(
    r"\b(fig(?:ure)?|table)\s*\.?\s*(\d+)\b|([图表])\s*(\d+)",
    re.IGNORECASE,
)

PROMPT_PATH = Path(__file__).parent / "prompts" / "executor.md"

MAX_REPAIR_ATTEMPTS = 2
MAX_PRIOR_PAGES_IN_CONTEXT = 2
MAX_SVG_EXTRACTION_ATTEMPTS = 3
MAX_CONCURRENT_PAGES = 3  # parallel generation concurrency limit

CriticCallback = Callable[[int, int, CriticReport], Awaitable[None]]


def _resolve_fig_tokens(
    page_content: str,
    figure_inventory: list[dict] | None,
) -> tuple[str, list[dict], list[str]]:
    if not figure_inventory:
        return page_content, [], []

    by_id: dict[str, dict] = {}
    for fig in figure_inventory:
        path = str(fig.get("path") or "")
        if path:
            by_id[Path(path).stem] = fig

    used: list[dict] = []
    seen: set[str] = set()
    rejected: list[str] = []

    def _replace(match: re.Match) -> str:
        fig_id = match.group(1)
        fig = by_id.get(fig_id)
        if fig is None:
            return f"[[MISSING_FIG:{fig_id}]]"
        line = _line_containing(page_content, match.start())
        mismatch = _figure_label_mismatch(line, str(fig.get("caption") or ""))
        if mismatch:
            rejected.append(f"{fig_id}: {mismatch}")
            return f"[[REJECTED_FIG:{fig_id} — {mismatch}]]"
        if fig_id not in seen:
            seen.add(fig_id)
            used.append(fig)
        path = fig.get("path") or ""
        cap = (fig.get("caption") or "").strip().replace("\n", " ")
        if len(cap) > 160:
            cap = cap[:157] + "..."
        return f'[PAPER FIGURE — id={fig_id}, href="{path}", caption: {cap}]'

    return FIG_TOKEN_RE.sub(_replace, page_content), used, rejected


def _figure_guidance_block(used: list[dict], rejected: list[str] | None = None) -> str:
    rejected = rejected or []
    if not used:
        lines = [
            "## Paper Figure Guidance\n"
            "- This slide does not contain an explicit paper-figure token. "
            "Do not invent a paper-figure `<image href>` path."
        ]
        for item in rejected:
            lines.append(f"- Rejected paper figure token: {item}.")
        return "\n".join(lines)

    lines = ["## Paper Figure Guidance"]
    for fig in used:
        path = fig.get("path") or ""
        cap = (fig.get("caption") or "").strip().replace("\n", " ")
        if len(cap) > 160:
            cap = cap[:157] + "..."
        lines.append(f'- Allowed paper figure href: "{path}"; caption: {cap}')
    lines.append(
        "Use only the listed hrefs for extracted paper figures. Never substitute "
        "a different paper-figure href, reuse one from another slide, or invent one."
    )
    lines.append(
        "IMPORTANT: Paper figures must NOT overlap or cover text. "
        "Use a side-by-side (image left, text right) or top-bottom (image top, text below) layout. "
        "Text must always be readable and not hidden behind images."
    )
    for item in rejected:
        lines.append(f"Rejected paper figure token: {item}.")
    return "\n".join(lines)


EQ_TOKEN_RE = re.compile(r"\[\[EQ:(.*?)\]\](?:\s*\(p\d+\))?", re.IGNORECASE)


def _resolve_eq_tokens(page_content: str) -> tuple[str, list[str]]:
    """Replace [[EQ:latex]] tokens with inline guidance for the SVG generator.

    Returns (rewritten_content, list_of_latex_strings_found).
    """
    found: list[str] = []

    def _replace(match: re.Match) -> str:
        latex = match.group(1).strip()
        found.append(latex)
        return f'[EQUATION — LaTeX: {latex}]'

    rewritten = EQ_TOKEN_RE.sub(_replace, page_content)
    return rewritten, found


def _equation_guidance_block(equations: list[str]) -> str:
    """Build a guidance block telling the SVG generator how to render equations."""
    if not equations:
        return (
            "## Equation Guidance\n"
            "- This slide has no extracted equations. Do not add any."
        )
    lines = [
        "## Equation Guidance",
        "- This slide contains extracted equations from the paper. Render them as readable text.",
        "- For simple formulas, use Unicode math characters (×, ÷, ±, ≤, ≥, ≠, →, ∞, Σ, etc.).",
        "- For complex formulas, display the LaTeX notation as styled text with a light background box.",
        "- Place each equation in a visually distinct area (e.g., a rounded rect with light fill).",
        "- Equations found on this slide:",
    ]
    for eq in equations:
        lines.append(f"  - {eq}")
    return "\n".join(lines)
    start = text.rfind("\n", 0, offset) + 1
    end = text.find("\n", offset)
    if end == -1:
        end = len(text)
    return text[start:end]


def _extract_figure_label(text: str) -> tuple[str, str] | None:
    match = FIGURE_LABEL_RE.search(text)
    if not match:
        return None
    if match.group(1):
        kind_raw = match.group(1).lower()
        kind = "table" if kind_raw == "table" else "figure"
        return kind, match.group(2)
    kind = "figure" if match.group(3) == "图" else "table"
    return kind, match.group(4)


def _figure_label_mismatch(reference_line: str, caption: str) -> str | None:
    requested = _extract_figure_label(reference_line)
    actual = _extract_figure_label(caption)
    if not requested or not actual:
        return None
    if requested != actual:
        req_kind, req_num = requested
        actual_kind, actual_num = actual
        return f"requested {req_kind} {req_num}, but inventory caption is {actual_kind} {actual_num}"
    return None


def _paper_figure_key_from_href(href: str) -> str | None:
    if href.startswith("data:"):
        return None
    normalized = href.replace("\\", "/")
    stem = Path(normalized).stem
    if "/sources/images/" in normalized or stem.startswith("fig_"):
        return stem
    return None


def _validate_paper_figure_refs(
    svg_content: str,
    *,
    allowed_figures: list[dict],
    used_paper_figures: dict[str, int],
) -> CriticReport:
    allowed_keys = {
        Path(str(fig.get("path") or "")).stem
        for fig in allowed_figures
        if fig.get("path")
    }
    hrefs = IMAGE_HREF_RE.findall(svg_content)
    paper_keys = [key for href in hrefs if (key := _paper_figure_key_from_href(href)) is not None]
    violations: list[Violation] = []

    for key in sorted(set(paper_keys)):
        if key not in allowed_keys:
            violations.append(Violation(
                rule="paper_figure_not_allowed",
                severity="error",
                detail=f'Paper figure "{key}" is not allowed for this slide.',
            ))
        if paper_keys.count(key) > 1:
            violations.append(Violation(
                rule="paper_figure_duplicate_on_slide",
                severity="error",
                detail=f'Paper figure "{key}" appears multiple times.',
            ))
        previous_page = used_paper_figures.get(key)
        if previous_page is not None:
            violations.append(Violation(
                rule="paper_figure_reused_from_previous_slide",
                severity="error",
                detail=f'Paper figure "{key}" was already used on slide {previous_page}.',
            ))

    return CriticReport(passed=not violations, violations=violations)


def _merge_reports(*reports: CriticReport) -> CriticReport:
    violations: list[Violation] = []
    canvas = None
    for report in reports:
        violations.extend(report.violations)
        canvas = canvas or report.canvas
    return CriticReport(
        passed=all(report.passed for report in reports),
        violations=violations,
        canvas=canvas,
    )


def _make_page_name(num: int, content: str) -> str:
    match = re.match(r"^##?\s+(.+)$", content, re.MULTILINE)
    if match:
        name = match.group(1).strip()
        name = re.sub(r"[^\w\s-]", "", name)
        name = re.sub(r"\s+", "_", name)
        return name[:40].lower()
    return f"page_{num}"


def _extract_svg(text: str) -> str | None:
    match = re.search(r"```(?:svg|xml)?\s*\n(.*?)\n```", text, re.DOTALL)
    if match:
        svg = match.group(1).strip()
        if svg.startswith("<svg"):
            return svg
    match = re.search(r"(<svg[^>]*>.*?</svg>)", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return None


async def _generate_single_page(
    page_num: int,
    page_content: str,
    page_name: str,
    total_pages: int,
    design_spec: str,
    project_dir: Path,
    llm: LLMProvider,
    model: str,
    *,
    style: str = "academic",
    language: str = "zh",
    detail_level: str = "normal",
    extra_block: str = "",
    figure_inventory: list[dict] | None = None,
    critic_config: CriticConfig | None = None,
    on_critic: CriticCallback | None = None,
    max_repairs: int = 1,
    quick_mode: bool = False,
    enable_visual_critic: bool = False,
) -> tuple[int, str]:
    """Generate SVG for a single page with its own independent conversation.

    Returns (page_num, svg_content).
    """
    from backend.generator.visual_critic import visual_check

    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")

    # Build independent conversation for this page
    conversation: list[LLMMessage] = [
        LLMMessage.system(system_prompt),
        LLMMessage.user(
            f"## Design Specification\n\n{design_spec}\n\n"
            f"## Fixed Runtime Configuration\n\n"
            f"- Selected style preset: {style}\n"
            f"- Selected language: {language}\n"
            f"- Selected detail level: {detail_level}\n\n"
            f"You are generating page {page_num} of {total_pages}.\n\n"
            f"{extra_block}"
        ),
        LLMMessage.assistant(
            "Understood. I have the design specification and technical constraints. "
            "Please provide the content for this page."
        ),
    ]

    rewritten_content, used_figures, rejected_figures = _resolve_fig_tokens(
        page_content, figure_inventory,
    )
    eq_content, found_equations = _resolve_eq_tokens(rewritten_content)
    rewritten_content = eq_content
    figure_guidance = _figure_guidance_block(used_figures, rejected_figures)
    equation_guidance = _equation_guidance_block(found_equations)

    conversation.append(
        LLMMessage.user(
            f"## Page {page_num}/{total_pages}: {page_name}\n\n"
            f"{rewritten_content}\n\n"
            f"## Runtime Reminders\n"
            f"- Style preset: {style}\n"
            f"- Language: {language}\n"
            f"- Detail level: {detail_level}\n\n"
            f"{figure_guidance}\n\n"
            f"{equation_guidance}\n\n"
            f"Generate the complete SVG code for this page. "
            f"Output ONLY the SVG code, wrapped in ```svg code block."
        )
    )

    max_tokens = 8192 if quick_mode else 16384
    response: LLMResponse = await llm.chat(
        conversation, model, temperature=0.3, max_tokens=max_tokens
    )

    svg_content = _extract_svg(response.content)
    for extraction_attempt in range(2, MAX_SVG_EXTRACTION_ATTEMPTS + 1):
        if svg_content:
            break
        conversation.append(LLMMessage.assistant(response.content))
        conversation.append(
            LLMMessage.user(
                "The previous response did not contain a parseable SVG. "
                "Regenerate the complete SVG for this page only, wrapped in ```svg code block."
            )
        )
        response = await llm.chat(
            conversation, model, temperature=0.2, max_tokens=max_tokens
        )
        svg_content = _extract_svg(response.content)

    if not svg_content:
        raise RuntimeError(
            f"Failed to generate parseable SVG for page {page_num}/{total_pages} "
            f"({page_name}) after {MAX_SVG_EXTRACTION_ATTEMPTS} attempts"
        )

    # Repair loop
    best_svg = svg_content
    visual_attempted = False
    for attempt in range(2, max_repairs + 2):
        report = _merge_reports(
            check_svg(svg_content, critic_config),
            _validate_paper_figure_refs(
                svg_content,
                allowed_figures=used_figures,
                used_paper_figures={},  # independent page, no cross-page tracking
            ),
        )
        if on_critic is not None:
            await on_critic(page_num, attempt - 1, report)

        if report.passed:
            if enable_visual_critic and not quick_mode and not visual_attempted:
                visual_attempted = True
                try:
                    visual_outcome = await visual_check(
                        svg_content, llm=llm, model=model,
                        page_num=page_num, page_title=page_name, style=style,
                    )
                except Exception:
                    visual_outcome = None
                if on_critic is not None and visual_outcome:
                    await on_critic(page_num, attempt - 1, visual_outcome.report)
                if visual_outcome and visual_outcome.rendered and not visual_outcome.report.passed:
                    report = visual_outcome.report
                else:
                    best_svg = svg_content
                    break
            else:
                best_svg = svg_content
                break

        # Repair
        conversation.append(
            LLMMessage.user(
                report.to_prompt_block()
                + "\n\nReturn the complete corrected SVG only, "
                "wrapped in a ```svg code block."
            )
        )
        repair_temp = max(0.1, 0.3 - 0.1 * (attempt - 1))
        response = await llm.chat(
            conversation, model, temperature=repair_temp, max_tokens=max_tokens
        )

        repaired = _extract_svg(response.content)
        if repaired:
            svg_content = repaired
            best_svg = repaired
        else:
            break

    return page_num, best_svg


async def generate_svg_pages(
    design_spec: str,
    manuscript: str,
    project_dir: Path,
    llm: LLMProvider,
    model: str,
    *,
    style: str = "academic",
    language: str = "zh",
    detail_level: str = "normal",
    extra_instruction: str = "",
    target_pages: set[int] | None = None,
    critic_config: CriticConfig | None = None,
    on_critic: CriticCallback | None = None,
    figure_inventory: list[dict] | None = None,
    enable_visual_critic: bool = False,
    quick_mode: bool = False,
) -> AsyncIterator[tuple[int, str]]:
    """Generate SVG code for each slide page sequentially with critic validation.

    In *quick_mode* the static critic repair loop is limited to 1 attempt,
    the visual critic is always skipped, and pages are generated in parallel
    (up to MAX_CONCURRENT_PAGES at a time) for faster completion.
    """
    from backend.generator.visual_critic import visual_check, VisualCriticConfig

    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")

    # Quick mode: fewer repairs, no visual critic
    max_repairs = 1 if quick_mode else MAX_REPAIR_ATTEMPTS

    pages = split_manuscript_pages(manuscript)
    svg_output_dir = project_dir / "svg_output"
    svg_output_dir.mkdir(parents=True, exist_ok=True)
    used_paper_figures: dict[str, int] = {}

    extra_sections = []
    if extra_instruction:
        extra_sections.append(extra_instruction)
    if is_deepseek_provider(llm, model):
        extra_sections.append(deepseek_executor_guidance(detail_level))
    extra_block = "\n\n" + "\n\n".join(extra_sections) if extra_sections else ""

    # ── Parallel path (quick_mode) ──
    if quick_mode and (target_pages is None or len(target_pages) > 1):
        _sem = asyncio.Semaphore(MAX_CONCURRENT_PAGES)

        async def _gen_page(i: int, page_content: str) -> tuple[int, str]:
            page_num = i + 1
            if target_pages is not None and page_num not in target_pages:
                return page_num, ""
            page_name = _make_page_name(page_num, page_content)
            async with _sem:
                return await _generate_single_page(
                    page_num, page_content, page_name, len(pages),
                    design_spec, project_dir, llm, model,
                    style=style, language=language, detail_level=detail_level,
                    extra_block=extra_block,
                    figure_inventory=figure_inventory,
                    critic_config=critic_config,
                    on_critic=on_critic,
                    max_repairs=max_repairs,
                    quick_mode=True,
                    enable_visual_critic=False,
                )

        tasks = [_gen_page(i, pc) for i, pc in enumerate(pages)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                raise result
            page_num, svg_content = result
            if not svg_content:
                continue
            page_name = _make_page_name(page_num, pages[page_num - 1])
            svg_path = svg_output_dir / f"{page_num:02d}_{page_name}.svg"
            svg_path.write_text(svg_content, encoding="utf-8")
            for href in IMAGE_HREF_RE.findall(svg_content):
                key = _paper_figure_key_from_href(href)
                if key is not None:
                    used_paper_figures.setdefault(key, page_num)
            yield page_num, svg_content
        return

    # ── Sequential path (normal mode) ──

    conversation: list[LLMMessage] = [
        LLMMessage.system(system_prompt),
        LLMMessage.user(
            f"## Design Specification\n\n{design_spec}\n\n"
            f"## Fixed Runtime Configuration\n\n"
            f"- Selected style preset: {style}\n"
            f"- Selected language: {language}\n"
            f"- Selected detail level: {detail_level}\n\n"
            f"Total pages to generate: {len(pages)}\n\n"
            f"You will generate SVG code for each page sequentially. "
            f"I will provide the content for each page one at a time."
            f"{extra_block}"
        ),
        LLMMessage.assistant(
            "Understood. I have the design specification and technical constraints. "
            "Please provide the content for page 1."
        ),
    ]

    _preamble_len = len(conversation)

    for i, page_content in enumerate(pages):
        page_num = i + 1
        if target_pages is not None and page_num not in target_pages:
            continue

        # Sliding window
        _max_context_msgs = MAX_PRIOR_PAGES_IN_CONTEXT * (1 + max_repairs) * 2
        _beyond_preamble = len(conversation) - _preamble_len
        if _beyond_preamble > _max_context_msgs:
            _trim = _beyond_preamble - _max_context_msgs
            conversation[:] = conversation[:_preamble_len] + conversation[_preamble_len + _trim:]

        page_name = _make_page_name(page_num, page_content)
        rewritten_content, used_figures, rejected_figures = _resolve_fig_tokens(
            page_content, figure_inventory,
        )
        eq_content, found_equations = _resolve_eq_tokens(rewritten_content)
        rewritten_content = eq_content
        figure_guidance = _figure_guidance_block(used_figures, rejected_figures)
        equation_guidance = _equation_guidance_block(found_equations)

        conversation.append(
            LLMMessage.user(
                f"## Page {page_num}/{len(pages)}: {page_name}\n\n"
                f"{rewritten_content}\n\n"
                f"## Runtime Reminders\n"
                f"- Style preset: {style}\n"
                f"- Language: {language}\n"
                f"- Detail level: {detail_level}\n\n"
                f"{figure_guidance}\n\n"
                f"{equation_guidance}\n\n"
                f"Generate the complete SVG code for this page. "
                f"Output ONLY the SVG code, wrapped in ```svg code block."
            )
        )

        max_tokens = 8192 if quick_mode else 16384
        response: LLMResponse = await llm.chat(
            conversation, model, temperature=0.3, max_tokens=max_tokens
        )

        svg_content = _extract_svg(response.content)
        for extraction_attempt in range(2, MAX_SVG_EXTRACTION_ATTEMPTS + 1):
            if svg_content:
                break
            conversation.append(LLMMessage.assistant(response.content))
            conversation.append(
                LLMMessage.user(
                    "The previous response did not contain a parseable SVG. "
                    "Regenerate the complete SVG for this page only, wrapped in ```svg code block."
                )
            )
            response = await llm.chat(
                conversation, model, temperature=0.2, max_tokens=max_tokens
            )
            svg_content = _extract_svg(response.content)

        if svg_content:
            conversation.append(LLMMessage.assistant(f"```svg\n{svg_content}\n```"))

            best_svg = svg_content
            visual_attempted = False
            for attempt in range(2, max_repairs + 2):
                report = _merge_reports(
                    check_svg(svg_content, critic_config),
                    _validate_paper_figure_refs(
                        svg_content,
                        allowed_figures=used_figures,
                        used_paper_figures=used_paper_figures,
                    ),
                )
                if on_critic is not None:
                    await on_critic(page_num, attempt - 1, report)

                if report.passed:
                    # Run visual critic if enabled (never in quick mode)
                    if enable_visual_critic and not quick_mode and not visual_attempted:
                        visual_attempted = True
                        try:
                            visual_outcome = await visual_check(
                                svg_content,
                                llm=llm,
                                model=model,
                                page_num=page_num,
                                page_title=page_name,
                                style=style,
                            )
                        except Exception:
                            visual_outcome = None
                        if on_critic is not None and visual_outcome:
                            await on_critic(page_num, attempt - 1, visual_outcome.report)
                        if visual_outcome and visual_outcome.rendered and not visual_outcome.report.passed:
                            report = visual_outcome.report
                        else:
                            best_svg = svg_content
                            break
                    else:
                        best_svg = svg_content
                        break

                # Repair
                conversation.append(
                    LLMMessage.user(
                        report.to_prompt_block()
                        + "\n\nReturn the complete corrected SVG only, "
                        "wrapped in a ```svg code block."
                    )
                )
                repair_temp = max(0.1, 0.3 - 0.1 * (attempt - 1))
                response = await llm.chat(
                    conversation, model, temperature=repair_temp, max_tokens=max_tokens
                )

                repaired = _extract_svg(response.content)
                if repaired:
                    svg_content = repaired
                    best_svg = repaired
                    conversation.append(LLMMessage.assistant(f"```svg\n{repaired}\n```"))
                else:
                    conversation.append(LLMMessage.assistant(response.content))
                    break

            svg_path = svg_output_dir / f"{page_num:02d}_{page_name}.svg"
            svg_path.write_text(best_svg, encoding="utf-8")
            for href in IMAGE_HREF_RE.findall(best_svg):
                key = _paper_figure_key_from_href(href)
                if key is not None:
                    used_paper_figures.setdefault(key, page_num)
            yield page_num, best_svg
        else:
            conversation.append(LLMMessage.assistant(response.content))
            raise RuntimeError(
                f"Failed to generate parseable SVG for page {page_num}/{len(pages)} "
                f"({page_name}) after {MAX_SVG_EXTRACTION_ATTEMPTS} attempts"
            )


# ---------------------------------------------------------------------------
# Speaker notes generation
# ---------------------------------------------------------------------------

_NOTES_SYSTEM_PROMPT = """\
You are a professional presentation speaker-notes writer. Given the slide
outline and the original paper text, produce detailed speaker notes.

## Content Rules
- Write notes in the same language as the presentation.
- Each slide's notes must ONLY discuss THAT slide's own content.
- Every slide (except the first) starts with a [过渡] transition phrase.
- Use [停顿] after key points and [数据] before mentioning statistics.
- CRITICAL: When citing ANY number from the paper, you MUST use the EXACT
  same number format as in the original paper. Do NOT round, approximate,
  or reformat numbers. For example, if the paper says 0.292, write 0.292,
  NOT 0.30 or 0.29. If the paper says 1,555,806, write 1,555,806, NOT
  1.56 million. Exact numbers are essential for source tracking.

## Output Format
Output ONLY a JSON object: {"speaker_notes": {"slide_key": "notes text...", ...}}
Values must be plain strings, NOT nested objects.
"""

_NOTES_JSON_RE = re.compile(r'\{[\s\S]*"speaker_notes"[\s\S]*\}')


async def generate_speaker_notes(
    manuscript: str,
    project_dir: Path,
    llm: LLMProvider,
    model: str,
    *,
    language: str = "zh",
    paper_text: str = "",
    speech_minutes: int | None = None,
) -> dict[str, str]:
    """Generate speaker notes for all slides in one batch call.

    Returns a dict mapping SVG-file stem (e.g. ``01_cover``) to notes text.
    The result is also saved as ``notes.json`` in *project_dir*.
    """
    pages = split_manuscript_pages(manuscript)

    # Build a compact outline for the notes prompt
    outline_parts: list[str] = []
    for i, page_content in enumerate(pages):
        page_name = _make_page_name(i + 1, page_content)
        stem = f"{i + 1:02d}_{page_name}"
        # Trim page content — use more context for longer notes
        trimmed = page_content.strip()[:1200]
        outline_parts.append(f"### Slide {stem}\n{trimmed}")

    outline_text = "\n\n".join(outline_parts)

    # Include original paper text for accurate [来源] citations
    paper_section = ""
    if paper_text:
        # Truncate to avoid exceeding context window
        paper_section = f"\n\n## Original Paper Text (for citations)\n\n{paper_text[:12000]}\n"

    locale_marker = "中文" if language.startswith("zh") else "English"
    lang_instruction = "请用中文撰写所有演讲讲稿。" if language.startswith("zh") else "Write all speaker notes in English."

    # Build notes length instruction based on speech_minutes
    notes_length_instruction = ""
    if speech_minutes and speech_minutes > 0:
        total_chars = speech_minutes * 130
        num_pages = len(pages)
        chars_per_slide = total_chars // max(num_pages, 1)
        notes_length_instruction = (
            f"\nLENGTH REQUIREMENT: The total speech duration is {speech_minutes} minutes. "
            f"At ~130 characters per minute, write approximately {chars_per_slide} characters "
            f"of notes per slide (total ~{total_chars} characters across {num_pages} slides). "
            f"Expand each slide's notes with sufficient detail, explanations, and evidence "
            f"to reach this target. Do NOT be overly concise.\n"
        )
    else:
        notes_length_instruction = (
            "\nWrite detailed speaker notes for each slide. Each slide should have "
            "approximately 300-500 characters of notes, enough for a speaker to present "
            "for about 2-3 minutes. Expand with explanations, evidence, and transitions. "
            "Do NOT be overly concise — the notes should be a near-complete script.\n"
        )

    user_msg = (
        f"{lang_instruction}\n\n"
        f"Below is the slide-by-slide content outline ({locale_marker}).\n\n"
        f"{notes_length_instruction}"
        f"Each slide's notes must ONLY discuss THAT slide's own content.\n\n"
        f"--- SLIDE OUTLINE ---\n\n"
        f"{outline_text}\n\n"
        f'Respond with ONLY a JSON object: {{"speaker_notes": {{"page_stem": "notes..."}}}}'
    )

    conversation = [
        LLMMessage.system(_NOTES_SYSTEM_PROMPT),
        LLMMessage.user(user_msg),
    ]

    response = await llm.chat(conversation, model, temperature=0.5, max_tokens=16384)
    notes: dict[str, str] = {}

    # Try to parse JSON from response
    json_match = _NOTES_JSON_RE.search(response.content)
    if json_match:
        try:
            data = json.loads(json_match.group())
            raw_notes = data.get("speaker_notes", {})
            for key, val in raw_notes.items():
                if isinstance(val, dict):
                    # Handle {"notes": "...", "citations": [...]} format
                    notes[key] = val.get("notes", "")
                elif isinstance(val, str):
                    notes[key] = val
        except json.JSONDecodeError:
            pass

    # Auto-correct numbers in notes to match paper
    # LLM may round/reformat numbers (e.g. 0.292 -> 0.30). We find each
    # number in the notes and replace it with the closest-matching number from
    # the paper text so that exact substring matching works.
    if paper_text and notes:
        paper_numbers = [
            (m.group(), float(m.group().replace(',', '')))
            for m in re.finditer(r'\d[\d,]*\.?\d*', paper_text)
        ]
        if paper_numbers:
            def _correct_numbers(text):
                def _replace_one(m):
                    raw = m.group()
                    try:
                        val = float(raw.replace(',', ''))
                    except ValueError:
                        return raw
                    if val < 0.001 or (val == int(val) and val > 100):
                        return raw
                    best_raw = None
                    best_diff = float('inf')
                    for p_raw, p_val in paper_numbers:
                        diff = abs(val - p_val)
                        if diff < best_diff and diff < 0.05 and (p_val == 0 or diff / abs(p_val) < 0.05):
                            best_diff = diff
                            best_raw = p_raw
                    if best_raw is not None and best_raw != raw:
                        return best_raw
                    return raw
                return re.sub(r'\d[\d,]*\.?\d*', _replace_one, text)

            for key in list(notes.keys()):
                notes[key] = _correct_numbers(notes[key])

    # Fallback: if JSON parsing failed, create simple notes from outline
    if not notes:
        for i, page_content in enumerate(pages):
            page_name = _make_page_name(i + 1, page_content)
            stem = f"{i + 1:02d}_{page_name}"
            heading_match = re.match(r"^##?\s+(.+)$", page_content, re.MULTILINE)
            if heading_match:
                notes[stem] = f"[过渡] 接下来介绍{heading_match.group(1).strip()}。"
            else:
                notes[stem] = f"第{i + 1}页演讲讲稿。"

    # ── Auto-match notes sentences to paper text ──
    # For each sentence in the notes, find the most relevant sentence from the
    # original paper text.  Store results as a sidecar mapping that the
    # frontend can use to show tooltips on hover.
    paper_sentences: list[str] = []
    if paper_text:
        # Pre-process: rejoin hyphenated line breaks (e.g. "percent-\nage" → "percentage")
        clean_text = re.sub(r'(\w)-\s*\n\s*(\w)', r'\1\2', paper_text)
        # Remove figure/table/equation markers -- only match body text for source citations
        clean_text = re.sub(r'\[\[FIG:[^\]]*\]\][^\n]*', '', clean_text)
        clean_text = re.sub(r'\[\[EQ:[^\]]*\]\]\([^)]*\)', '', clean_text)
        clean_text = re.sub(r'\[\[EQ:[^\]]*\]\]', '', clean_text)
        clean_text = re.sub(r'^\|.*\|$', '', clean_text, count=0, flags=re.MULTILINE)
        clean_text = re.sub(r'^\|[-:| ]+$', '', clean_text, count=0, flags=re.MULTILINE)
        clean_text = re.sub(r'^>\s*Context:.*$', '', clean_text, count=0, flags=re.MULTILINE)
        # Replace remaining newlines with spaces (newlines are just formatting, not sentence boundaries)
        clean_text = re.sub(r'\n+', ' ', clean_text)
        # Split into sentences by sentence-ending punctuation, avoiding decimal points
        paper_sentences = re.split(r'(?<=[.。！？；])(?!\d)\s+', clean_text)
        # Merge fragments that are too short (<50 chars) with the previous sentence
        merged = []
        for s in paper_sentences:
            s = s.strip()
            if not s:
                continue
            if merged and (len(s) < 50 or not re.search(r'[.。！？；]$', s)):
                merged[-1] = merged[-1] + ' ' + s
            else:
                merged.append(s)
        paper_sentences = [s for s in merged if len(s.strip()) > 15]

    def _find_paper_match(sent: str) -> str | None:
        """Find the best-matching paper sentence for a notes sentence."""
        if not paper_sentences or len(sent) < 6:
            return None
        # Extract numbers (including decimals) — these are the strongest matching signals
        numbers = re.findall(r'\d+\.?\d*', re.sub(r'(\d)%', r'\1', sent))
        has_numbers = len(numbers) > 0

        # Extract English keywords (Chinese chars don't match English paper text)
        tokens = re.findall(r'[a-zA-Z_]\w*', sent.lower())
        tokens = [t for t in tokens if len(t) > 2]
        if not tokens and not numbers:
            return None

        best_score = 0.0
        best_match = None
        for ps in paper_sentences:
            ps_lower = ps.lower()
            kw_score = sum(1 for t in tokens if t in ps_lower)
            # Number match: exact substring match (works because we now
            # preserve original number format in notes)
            num_score = sum(3 for n in numbers if n in ps)
            total_score = kw_score + num_score
            if total_score <= 0:
                continue
            if has_numbers:
                if num_score > 0 or kw_score >= 2:
                    if total_score > best_score:
                        best_score = total_score
                        best_match = ps
            else:
                if kw_score >= 2 and total_score > best_score:
                    best_score = total_score
                    best_match = ps
        return best_match

    # Build sentence-level source mapping
    notes_sources: dict[str, list[dict]] = {}
    for stem, note_text in notes.items():
        # Split notes into sentences by Chinese/English sentence boundaries
        # Avoid splitting on decimal points (e.g. 0.292) — use negative lookbehind for digits
        raw_sents = re.split(r'(?<=[。！？])(?!\d)\s*|(?<=[.]\s)(?=\S)', note_text)
        refined = []
        for s in raw_sents:
            s = s.strip()
            if not s:
                continue
            if len(s) > 80:
                # Split on ；(semicolon) for long sentences
                subs = re.split(r'(?<=[；;])\s*', s)
                refined.extend([p.strip() for p in subs if p.strip()])
            else:
                refined.append(s)
        sources = []
        for s in refined:
            match = _find_paper_match(s)
            sources.append({
                "text": s,
                "source": match,  # None if no match found
            })
        notes_sources[stem] = sources

    # Save notes.json (plain string values, compatible with old format)
    notes_path = project_dir / "notes.json"
    notes_path.write_text(json.dumps(notes, ensure_ascii=False, indent=2), encoding="utf-8")

    # Save notes_sources.json (sentence-level source mapping)
    sources_path = project_dir / "notes_sources.json"
    sources_path.write_text(json.dumps(notes_sources, ensure_ascii=False, indent=2), encoding="utf-8")

    return notes
