#!/usr/bin/env python3
"""Trusted paths and per-session bindings for the readiness edit gate."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

MARKER_DIR = ".readiness-sessions"


def marker_path(root: Path, session_id: str) -> Path:
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return root / "_workspace" / MARKER_DIR / f"{digest}.json"


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


def _marker_location(
    root: Path, session_id: str, *, create: bool
) -> tuple[str, Path | None]:
    if not isinstance(session_id, str) or not session_id:
        return "absent", None
    workspace = root / "_workspace"
    marker_dir = workspace / MARKER_DIR
    if workspace.is_symlink() or marker_dir.is_symlink():
        return "unsafe", None
    try:
        if create:
            marker_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return "unsafe", None
    if not marker_dir.is_dir():
        return "absent", None

    marker = marker_path(root, session_id)
    if marker.is_symlink():
        return "unsafe", None
    if not marker.exists():
        return "absent", marker
    if not marker.is_file():
        return "unsafe", None
    return "bound", marker


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
    state, marker = _marker_location(root, session_id, create=True)
    if state == "unsafe" or marker is None:
        return False

    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=".binding-", suffix=".tmp", dir=marker.parent
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"task_dir": relative.as_posix()}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, marker)
        return True
    except OSError:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        return False


def load_binding(root: Path, session_id: str) -> tuple[str, Path | None]:
    """Return (bound|absent|unsafe, verified task directory)."""
    state, marker = _marker_location(root, session_id, create=False)
    if state != "bound" or marker is None:
        return state, None
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
        raw_task = value.get("task_dir") if isinstance(value, dict) else None
        if not isinstance(raw_task, str):
            return "unsafe", None
        task_dir = root / raw_task
        if _task_relative(task_dir, root) is None:
            return "unsafe", None
        return "bound", task_dir.resolve(strict=True)
    except (OSError, RuntimeError, UnicodeError, json.JSONDecodeError):
        return "unsafe", None
