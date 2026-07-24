#!/usr/bin/env python3
"""PreToolUse adapter for the project-local structural Design Gate."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from scenario_gate import resolve_active_task, validate_design  # noqa: E402
from transcript import note, read_hook_input, run_fail_open  # noqa: E402

LABEL = "design-gate"
EDIT_TOOLS = {"write", "edit", "apply_patch"}
UNGUARDED_SUFFIXES = {".md", ".rst", ".txt"}
UNGUARDED_FILENAMES = {
    "authors",
    "changelog",
    "code_of_conduct",
    "contributors",
    "contributing",
    "license",
    "notice",
    "readme",
}
_PATCH_PATH = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.MULTILINE)


def _tool_name(event: dict[str, Any]) -> str:
    value = event.get("tool_name")
    if not isinstance(value, str):
        return ""
    return re.split(r"[.:]", value.lower())[-1]


def _patch_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        strings = []
        for key in ("command", "raw", "patch", "input"):
            strings.extend(_patch_strings(value.get(key)))
        return strings
    return []


def edit_paths(event: dict[str, Any]) -> list[str]:
    tool_input = event.get("tool_input")
    paths: list[str] = []
    if isinstance(tool_input, dict):
        for key in ("file_path", "filePath", "path"):
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                paths.append(value.strip())
    for raw in _patch_strings(tool_input):
        paths.extend(path.strip() for path in _PATCH_PATH.findall(raw) if path.strip())
    return list(dict.fromkeys(paths))


def _project_root(event: dict[str, Any]) -> Path | None:
    raw = event.get("cwd")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        root = Path(raw).resolve(strict=True)
        return root if root.is_dir() else None
    except (OSError, RuntimeError):
        return None


def _project_relative(root: Path, raw_path: str) -> Path | None:
    try:
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = root / candidate
        if candidate.is_symlink():
            return None
        return candidate.resolve(strict=False).relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None


def _pretool_block(reason: str) -> int:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": f"[agent-loop] {reason}",
                }
            }
        )
    )
    return 0


def _unguarded(relative: Path) -> bool:
    return (
        relative.parts[:1] == ("_workspace",)
        or relative.suffix.lower() in UNGUARDED_SUFFIXES
        or relative.name.lower() in UNGUARDED_FILENAMES
    )


def run_pre(event: dict[str, Any]) -> int:
    if _tool_name(event) not in EDIT_TOOLS:
        return 0
    paths = edit_paths(event)
    if not paths:
        note(LABEL, "direct edit target is unavailable, fail-open")
        return 0
    root = _project_root(event)
    if root is None:
        note(LABEL, "project directory is unavailable, fail-open")
        return 0

    guarded = []
    for raw_path in paths:
        relative = _project_relative(root, raw_path)
        if relative is None:
            return _pretool_block(
                f"guarded edit target is outside the project or unsafe: {raw_path}"
            )
        if not _unguarded(relative):
            guarded.append(relative)
    if not guarded:
        return 0

    try:
        task, active_errors = resolve_active_task(root)
        if active_errors:
            return _pretool_block("active design is unsafe: " + "; ".join(active_errors[:3]))
        if task is None:
            return _pretool_block(
                "no active design; run scenario_gate.py design "
                "_workspace/<task> --project-root . --activate"
            )
        design = validate_design(task, root)
        if not design.allowed:
            return _pretool_block(
                "active design is invalid: " + "; ".join(design.errors[:3])
            )
        return 0
    except Exception as exc:
        return _pretool_block(f"design gate failed safely: {type(exc).__name__}")


def main() -> int:
    event = read_hook_input(LABEL)
    if event is None:
        return 0
    return run_fail_open(LABEL, lambda: run_pre(event))


if __name__ == "__main__":
    sys.exit(main())
