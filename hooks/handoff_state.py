#!/usr/bin/env python3
"""Trusted handoff paths and per-session handoff markers.

This module owns the handoff path convention: `handoff.md` at the project
root, or `_workspace/<task>/handoff.md`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

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
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return root / "_workspace" / MARKER_DIR / f"{digest}.json"


def _marker_path(root: Path, session_id: str, *, create: bool) -> tuple[str, Path | None]:
    """Validate the marker location for an already-resolved root.

    Returns (state, path): "valid" — an existing regular marker file;
    "absent" — usable location but no marker yet (path set when writable);
    "suspicious" — symlinks or non-regular files where none belong.
    """
    if not isinstance(session_id, str) or not session_id:
        return "absent", None
    workspace = root / "_workspace"
    marker_dir = workspace / MARKER_DIR
    if workspace.is_symlink() or marker_dir.is_symlink():
        return "suspicious", None
    if create:
        marker_dir.mkdir(parents=True, exist_ok=True)
    if not marker_dir.is_dir():
        return "absent", None
    marker = marker_path(root, session_id)
    if marker.is_symlink():
        return "suspicious", None
    if not marker.exists():
        return "absent", marker
    if not marker.is_file():
        return "suspicious", None
    return "valid", marker


def record_session_handoff(cwd: Path, session_id: str, handoff: Path) -> bool:
    """Persist the verified handoff selected for one Claude session.

    `handoff` must already be resolved (as returned by resolve_handoff).
    """
    try:
        root = cwd.resolve(strict=True)
        state, marker = _marker_path(root, session_id, create=True)
        if state == "suspicious" or marker is None:
            return False
        relative = handoff.relative_to(root)
        marker.write_text(json.dumps({"path": relative.as_posix()}), encoding="utf-8")
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def load_session_handoff(cwd: Path, session_id: str) -> tuple[bool, Path | None]:
    """Return (marker exists, verified handoff) for a Claude session."""
    try:
        root = cwd.resolve(strict=True)
        state, marker = _marker_path(root, session_id, create=False)
        if state == "suspicious":
            return True, None
        if state == "absent":
            return False, None
        data = json.loads(marker.read_text(encoding="utf-8"))
        raw_path = data.get("path") if isinstance(data, dict) else None
        if not isinstance(raw_path, str):
            return True, None
        return True, resolve_handoff(root, raw_path)
    except (OSError, RuntimeError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        return True, None
