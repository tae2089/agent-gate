#!/usr/bin/env python3
"""Trusted, atomic JSON storage for project-local session markers."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

MarkerState = Literal["valid", "absent", "unsafe"]


def marker_path(root: Path, namespace: str, session_id: str) -> Path:
    """Return the marker path without touching the filesystem."""
    digest = sha256(session_id.encode("utf-8")).hexdigest()
    return root / "_workspace" / namespace / f"{digest}.json"


def _valid_namespace(namespace: str) -> bool:
    return (
        isinstance(namespace, str)
        and bool(namespace)
        and namespace not in {".", ".."}
        and Path(namespace).name == namespace
    )


def _marker_location(
    root: Path, namespace: str, session_id: str, *, create: bool
) -> tuple[MarkerState, Path | None]:
    if not _valid_namespace(namespace):
        return "unsafe", None
    if not isinstance(session_id, str) or not session_id:
        return "absent", None

    workspace = root / "_workspace"
    marker_dir = workspace / namespace
    if workspace.is_symlink() or marker_dir.is_symlink():
        return "unsafe", None
    try:
        if create:
            marker_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return "unsafe", None
    if not marker_dir.is_dir():
        return "absent", None

    marker = marker_path(root, namespace, session_id)
    if marker.is_symlink():
        return "unsafe", None
    if not marker.exists():
        return "absent", marker
    if not marker.is_file():
        return "unsafe", None
    return "valid", marker


def read_marker(
    root: Path, namespace: str, session_id: str
) -> tuple[MarkerState, dict[str, Any] | None]:
    """Read one JSON-object marker after validating its trust boundary."""
    state, marker = _marker_location(root, namespace, session_id, create=False)
    if state != "valid" or marker is None:
        return state, None
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            return "unsafe", None
        return "valid", value
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "unsafe", None


def write_marker(
    root: Path,
    namespace: str,
    session_id: str,
    payload: Mapping[str, Any],
) -> bool:
    """Atomically replace one trusted marker with a JSON object."""
    if not isinstance(payload, Mapping):
        return False
    state, marker = _marker_location(root, namespace, session_id, create=True)
    if state == "unsafe" or marker is None:
        return False

    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=".session-marker-", suffix=".tmp", dir=marker.parent
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(dict(payload), stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, marker)
        return True
    except (OSError, TypeError, ValueError):
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        return False
