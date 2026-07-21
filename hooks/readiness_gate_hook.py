#!/usr/bin/env python3
"""Bind validated task readiness and guard direct source-code edits.

``--mode bind`` is a PostToolUse hook for assessment writes. ``--mode pre``
is a PreToolUse hook that blocks guarded source edits unless the current
session's bound task remains ready.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from readiness_gate import validate_task_dir  # noqa: E402
from readiness_state import load_binding, project_relative, record_binding  # noqa: E402

LABEL = "readiness-gate"
EDIT_TOOLS = {"write", "edit", "apply_patch"}
GUARDED_SUFFIXES = {
    ".bash", ".c", ".cc", ".cpp", ".cs", ".cxx", ".dart", ".ex", ".exs",
    ".fish", ".fs", ".fsx", ".go", ".h", ".hpp", ".hrl", ".java", ".js",
    ".jsx", ".kt", ".kts", ".lua", ".m", ".mjs", ".mm", ".php", ".ps1",
    ".py", ".pyi", ".rb", ".rs", ".scala", ".sh", ".swift", ".svelte",
    ".ts", ".tsx", ".vue", ".zsh",
}
_PATCH_PATH = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.MULTILINE)


def _tool_name(event: dict[str, Any]) -> str:
    value = event.get("tool_name")
    if not isinstance(value, str):
        return ""
    return re.split(r"[.:]", value.lower())[-1]


def _tool_input(event: dict[str, Any]) -> Any:
    return event.get("tool_input")


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
    """Duck-type Claude/Codex direct paths and free-form apply_patch payloads."""
    tool_input = _tool_input(event)
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


def _session_id(event: dict[str, Any]) -> str:
    value = event.get("session_id")
    return value if isinstance(value, str) else ""


def _tool_succeeded(event: dict[str, Any]) -> bool:
    if event.get("is_error") is True:
        return False
    response = event.get("tool_response")
    if not isinstance(response, dict):
        return True
    if response.get("is_error") is True or response.get("error"):
        return False
    exit_code = response.get("exit_code")
    return not (type(exit_code) is int and exit_code != 0)


def _block(reason: str) -> int:
    print(json.dumps({"decision": "block", "reason": f"[agent-gate] {reason}"}))
    return 0


def _workspace_path(relative: Path) -> bool:
    return bool(relative.parts) and relative.parts[0] == "_workspace"


def run_pre(event: dict[str, Any]) -> int:
    if _tool_name(event) not in EDIT_TOOLS:
        return 0
    paths = edit_paths(event)
    if not paths:
        return _block("readiness gate could not determine the direct edit target")

    root = _project_root(event)
    if root is None:
        return _block("readiness gate could not resolve the project directory")

    guarded: list[Path] = []
    for raw_path in paths:
        suffix = Path(raw_path).suffix.lower()
        relative = project_relative(root, raw_path)
        if relative is not None and _workspace_path(relative):
            continue
        if suffix not in GUARDED_SUFFIXES:
            continue
        if relative is None:
            return _block(f"guarded edit target is outside the project or unsafe: {raw_path}")
        guarded.append(relative)
    if not guarded:
        return 0

    state, task_dir = load_binding(root, _session_id(event))
    if state == "absent":
        return _block(
            "no readiness task is bound to this session; write and validate "
            "_workspace/<task>/assessment.json with the artifact-judge readiness "
            "procedure before editing source"
        )
    if state != "bound" or task_dir is None:
        return _block("unsafe readiness session marker; remove or repair the local marker")

    result = validate_task_dir(task_dir)
    if not result.ready:
        details = "; ".join(result.errors[:3]) or "assessment is not ready"
        return _block(f"bound task is not ready: {details}")
    return 0


def _assessment_task(root: Path, raw_path: str) -> Path | None:
    relative = project_relative(root, raw_path)
    if relative is None:
        return None
    parts = relative.parts
    if (
        len(parts) != 3
        or parts[0] != "_workspace"
        or not parts[1]
        or parts[1].startswith(".")
        or parts[2] != "assessment.json"
    ):
        return None
    task_dir = root / Path(*parts[:2])
    try:
        if task_dir.is_symlink() or not task_dir.resolve(strict=True).is_dir():
            return None
    except (OSError, RuntimeError):
        return None
    return task_dir


def run_bind(event: dict[str, Any]) -> int:
    if _tool_name(event) not in EDIT_TOOLS or not _tool_succeeded(event):
        return 0
    root = _project_root(event)
    session_id = _session_id(event)
    if root is None or not session_id:
        return 0
    for raw_path in edit_paths(event):
        task_dir = _assessment_task(root, raw_path)
        if task_dir is None:
            continue
        result = validate_task_dir(task_dir)
        if result.ready:
            record_binding(root, session_id, task_dir)
            return 0
        details = "; ".join(result.errors[:3]) or "assessment is not ready"
        return _block(f"assessment is not ready: {details}")
    return 0


def _read_event(mode: str) -> dict[str, Any] | None:
    try:
        value = json.load(sys.stdin)
        return value if isinstance(value, dict) else None
    except (UnicodeError, json.JSONDecodeError):
        if mode == "pre":
            _block("readiness gate could not parse hook input")
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("pre", "bind"), required=True)
    args = parser.parse_args()
    event = _read_event(args.mode)
    if event is None:
        return 0
    try:
        return run_pre(event) if args.mode == "pre" else run_bind(event)
    except Exception as exc:  # fail closed only on the pre-edit enforcement boundary
        if args.mode == "pre":
            return _block(f"readiness gate failed safely: {type(exc).__name__}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
