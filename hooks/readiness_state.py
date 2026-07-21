#!/usr/bin/env python3
"""Trusted paths and per-session bindings for the readiness edit gate."""

from __future__ import annotations

from pathlib import Path

from session_marker import marker_path as session_marker_path
from session_marker import read_marker, write_marker

MARKER_DIR = ".readiness-sessions"


def marker_path(root: Path, session_id: str) -> Path:
    return session_marker_path(root, MARKER_DIR, session_id)


def project_relative(root: Path, raw_path: str | Path) -> Path | None:
    """Resolve an edit target under root without accepting target symlinks."""
    try:
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = root / candidate
        if candidate.is_symlink():
            return None
        resolved = candidate.resolve(strict=False)
        return resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None


def _task_relative(task_dir: Path, root: Path) -> Path | None:
    try:
        if task_dir.is_symlink():
            return None
        resolved = task_dir.resolve(strict=True)
        relative = resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    parts = relative.parts
    if (
        len(parts) != 2
        or parts[0] != "_workspace"
        or not parts[1]
        or parts[1].startswith(".")
        or not resolved.is_dir()
    ):
        return None
    return relative


def record_binding(root: Path, session_id: str, task_dir: Path) -> bool:
    """Atomically bind a session to a verified project-local task directory."""
    relative = _task_relative(task_dir, root)
    if relative is None:
        return False
    return write_marker(
        root, MARKER_DIR, session_id, {"task_dir": relative.as_posix()}
    )


def load_binding(root: Path, session_id: str) -> tuple[str, Path | None]:
    """Return (bound|absent|unsafe, verified task directory)."""
    state, value = read_marker(root, MARKER_DIR, session_id)
    if state == "absent":
        return "absent", None
    if state != "valid" or value is None:
        return "unsafe", None
    try:
        raw_task = value.get("task_dir")
        if not isinstance(raw_task, str):
            return "unsafe", None
        task_dir = root / raw_task
        if _task_relative(task_dir, root) is None:
            return "unsafe", None
        return "bound", task_dir.resolve(strict=True)
    except (OSError, RuntimeError):
        return "unsafe", None
