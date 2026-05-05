"""Data models for parsed academic papers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass
class PaperFigure:
    """A figure extracted from a paper."""

    path: Path
    caption: str = ""
    label: str | None = None
    available: bool = True
    page_number: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    extraction_method: str = "unknown"
    quality_score: float = 0.0
    review_flags: list[str] = field(default_factory=list)
    natural_width: int = 0
    natural_height: int = 0

    @property
    def aspect_ratio(self) -> float:
        if self.natural_width > 0 and self.natural_height > 0:
            return self.natural_width / self.natural_height
        return 0.0

    @property
    def fig_id(self) -> str:
        """Stable identifier used in `[[FIG:id]]` tokens."""
        return self.path.stem


@dataclass
class PaperEquation:
    """An equation extracted from a paper."""

    latex: str  # LaTeX source (best effort extraction)
    page_number: int = 0
    context: str = ""  # Surrounding text for disambiguation


@dataclass
class PaperTable:
    """A table extracted from a paper."""

    markdown: str
    caption: str = ""


@dataclass
class PaperSection:
    """A section of an academic paper."""

    title: str
    level: int  # 1=section, 2=subsection, 3=subsubsection
    content: str = ""
    figures: list[PaperFigure] = field(default_factory=list)
    tables: list[PaperTable] = field(default_factory=list)
    equations: list[PaperEquation] = field(default_factory=list)


@dataclass
class ParsedPaper:
    """A fully parsed academic paper."""

    title: str
    authors: list[str] = field(default_factory=list)
    abstract: str = ""
    sections: list[PaperSection] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    source_type: Literal["pdf", "latex"] = "pdf"
    figures_dir: Path | None = None

    def all_figures(self) -> list[PaperFigure]:
        figs = []
        for section in self.sections:
            figs.extend(section.figures)
        return figs

    @staticmethod
    def _should_include_figure(fig: PaperFigure) -> bool:
        if not fig.available:
            return False
        suspicious = {"body_text_intrusion", "low_graphic_coverage", "page_level_fallback"}
        if fig.review_flags and suspicious.intersection(fig.review_flags) and fig.quality_score < 0.55:
            return False
        return True

    def to_markdown(self) -> str:
        """Convert the parsed paper to a single Markdown document."""
        parts = []

        parts.append(f"# {self.title}\n")
        if self.authors:
            parts.append(f"**Authors:** {', '.join(self.authors)}\n")
        if self.abstract:
            parts.append(f"## Abstract\n\n{self.abstract}\n")

        for section in self.sections:
            prefix = "#" * (section.level + 1)
            parts.append(f"{prefix} {section.title}\n")
            if section.content:
                parts.append(section.content)

            for fig in section.figures:
                if not self._should_include_figure(fig):
                    continue
                caption = (fig.caption or "Figure").replace("\n", " ").strip()
                size_hint = ""
                if fig.natural_width > 0 and fig.natural_height > 0:
                    size_hint = (
                        f" (natural {fig.natural_width}×{fig.natural_height}px,"
                        f" ratio {fig.aspect_ratio:.3f})"
                    )
                parts.append(
                    f"\n[[FIG:{fig.fig_id}]] — {caption}{size_hint}\n"
                )

            for table in section.tables:
                if table.caption:
                    parts.append(f"\n*{table.caption}*\n")
                parts.append(f"\n{table.markdown}\n")

            for eq in section.equations:
                label = f" (p{eq.page_number})" if eq.page_number else ""
                parts.append(f"\n[[EQ:{eq.latex}]]{label}\n")
                if eq.context:
                    parts.append(f"> Context: {eq.context}\n")

        if self.references:
            parts.append("## References\n")
            for i, ref in enumerate(self.references, 1):
                parts.append(f"{i}. {ref}")

        return "\n".join(parts)
