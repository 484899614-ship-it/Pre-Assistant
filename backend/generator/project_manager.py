"""Project workspace management."""

from __future__ import annotations

import json
import shutil
from pathlib import Path


def init_project(project_dir: Path) -> Path:
    """Create a project workspace directory structure."""
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "svg_output").mkdir(exist_ok=True)
    (project_dir / "svg_final").mkdir(exist_ok=True)
    return project_dir


def get_svg_files(project_dir: Path, subdir: str = "output") -> list[Path]:
    """Get sorted SVG files from a project subdirectory."""
    svg_dir = project_dir / f"svg_{subdir}"
    if not svg_dir.exists():
        return []
    return sorted(svg_dir.glob("*.svg"))


def get_notes(project_dir: Path, svg_files: list[Path]) -> dict[str, str]:
    """Load speaker notes for the given SVG files."""
    notes_path = project_dir / "notes.json"
    if not notes_path.exists():
        return {}
    try:
        data = json.loads(notes_path.read_text(encoding="utf-8"))
        return data
    except (OSError, json.JSONDecodeError):
        return {}


def clone_project(source_dir: Path, dest_dir: Path) -> Path:
    """Clone a project workspace for refinement."""
    if dest_dir.exists():
        shutil.rmtree(dest_dir, ignore_errors=True)
    shutil.copytree(source_dir, dest_dir)
    return dest_dir


def cleanup_project(project_dir: Path) -> None:
    """Remove a project workspace."""
    if project_dir.exists():
        shutil.rmtree(project_dir, ignore_errors=True)


def prepare_for_finalize(project_dir: Path) -> None:
    """Copy svg_output/ to svg_final/ for post-processing."""
    src = project_dir / "svg_output"
    dst = project_dir / "svg_final"
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    if src.exists():
        shutil.copytree(src, dst)
