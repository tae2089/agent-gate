#!/usr/bin/env python3
"""Trusted handoff paths and per-session handoff markers.

This module owns the handoff path convention: `handoff.md` at the project
root, or `_workspace/<task>/handoff.md`.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from session_marker import marker_path as session_marker_path
from session_marker import read_marker, write_marker

MARKER_DIR = ".handoff-sessions"
HANDOFF_NAME = "handoff.md"


def iter_handoff_candidates(cwd: Path) -> Iterator[Path]:
    """Every path where the convention allows a handoff file to live."""
    yield from cwd.glob(f"_workspace/*/{HANDOFF_NAME}")
    yield cwd / HANDOFF_NAME


def resolve_handoff(root: Path, raw_path: str | Path) -> Path | None:
    """Return an existing, regular handoff file inside root, or None.

    `root` must already be fully resolved (`cwd.resolve(strict=True)`);
    callers resolve once instead of per candidate.
    """
    try:
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = root / candidate
        if candidate.is_symlink():
            return None
        resolved = candidate.resolve(strict=True)
        relative = resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None

    parts = relative.parts
    allowed = parts == (HANDOFF_NAME,) or (
        len(parts) == 3
        and parts[0] == "_workspace"
        and bool(parts[1])
        and not parts[1].startswith(".")
        and parts[2] == HANDOFF_NAME
    )
    if not allowed or not resolved.is_file():
        return None
    return resolved


def marker_path(root: Path, session_id: str) -> Path:
    """Marker file location for a session id (pure computation, no checks)."""
    return session_marker_path(root, MARKER_DIR, session_id)


def record_session_handoff(cwd: Path, session_id: str, handoff: Path) -> bool:
    """Persist the verified handoff selected for one Claude session.

    `handoff` must already be resolved (as returned by resolve_handoff).
    """
    try:
        root = cwd.resolve(strict=True)
        relative = handoff.relative_to(root)
        return write_marker(
            root, MARKER_DIR, session_id, {"path": relative.as_posix()}
        )
    except (OSError, RuntimeError, ValueError):
        return False


def load_session_handoff(cwd: Path, session_id: str) -> tuple[bool, Path | None]:
    """Return (marker exists, verified handoff) for a Claude session."""
    try:
        root = cwd.resolve(strict=True)
        state, data = read_marker(root, MARKER_DIR, session_id)
        if state == "unsafe":
            return True, None
        if state == "absent":
            return False, None
        raw_path = data.get("path") if data is not None else None
        if not isinstance(raw_path, str):
            return True, None
        return True, resolve_handoff(root, raw_path)
    except (OSError, RuntimeError, TypeError, ValueError):
        return True, None
