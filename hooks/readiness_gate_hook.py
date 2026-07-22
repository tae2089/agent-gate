#!/usr/bin/env python3
"""Bind validated task readiness and guard direct project edits.

``--mode bind`` is a PostToolUse hook for assessment writes. ``--mode pre``
is a PreToolUse hook that blocks guarded project edits unless the current
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

from readiness_gate import INHERITANCE_FILENAME, validate_task_dir  # noqa: E402
from scenario_gate import (  # noqa: E402
    CONTRACT_FILENAME as SCENARIO_CONTRACT_FILENAME,
    validate_readiness as validate_scenario_readiness,
)
from readiness_state import load_binding, project_relative, record_binding  # noqa: E402
from transcript import note, read_hook_input, run_fail_open  # noqa: E402

LABEL = "readiness-gate"
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
    """Duck-type Claude/Codex direct paths and free-form apply_patch payloads."""
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


def _pretool_block(reason: str) -> int:
    # Claude PreToolUse decisions use the event-specific permission envelope:
    # https://code.claude.com/docs/en/hooks#pretooluse-decision-control
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": f"[agent-gate] {reason}",
                }
            }
        )
    )
    return 0


def _not_ready_reason(result: Any) -> str:
    return "; ".join(result.errors[:3]) or "assessment is not ready"


def _workspace_path(relative: Path) -> bool:
    return bool(relative.parts) and relative.parts[0] == "_workspace"


def _unguarded_document(relative: Path) -> bool:
    return (
        relative.suffix.lower() in UNGUARDED_SUFFIXES
        or relative.name.lower() in UNGUARDED_FILENAMES
    )


def _enforce_guarded_edit(root: Path, event: dict[str, Any]) -> int:
    """Fail closed after target classification establishes protected scope."""
    try:
        state, task_dir = load_binding(root, _session_id(event))
        if state == "absent":
            return _pretool_block(
                "no readiness task is bound to this session; bind either a Full "
                "assessment.json from the artifact-judge procedure or an "
                "inherited-readiness.json that references a ready Full parent before "
                "editing protected project files"
            )
        if state != "bound" or task_dir is None:
            return _pretool_block(
                "unsafe readiness session marker; remove or repair the local marker"
            )

        result = validate_task_dir(task_dir)
        if not result.ready:
            return _pretool_block(
                f"bound task is not ready: {_not_ready_reason(result)}"
            )
        scenario = validate_scenario_readiness(task_dir, root)
        if scenario.enabled and not scenario.allowed:
            reason = "; ".join(scenario.errors[:3]) or "scenario proof is not ready"
            return _pretool_block(f"bound scenario proof is not ready: {reason}")
        return 0
    except Exception as exc:
        return _pretool_block(
            f"readiness gate failed safely: {type(exc).__name__}"
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

    guarded: list[Path] = []
    for raw_path in paths:
        relative = project_relative(root, raw_path)
        if relative is None:
            return _pretool_block(
                f"guarded edit target is outside the project or unsafe: {raw_path}"
            )
        if _workspace_path(relative) or _unguarded_document(relative):
            continue
        guarded.append(relative)
    if not guarded:
        return 0
    return _enforce_guarded_edit(root, event)


def _readiness_task(root: Path, raw_path: str) -> Path | None:
    relative = project_relative(root, raw_path)
    if relative is None:
        return None
    parts = relative.parts
    if (
        len(parts) != 3
        or parts[0] != "_workspace"
        or not parts[1]
        or parts[1].startswith(".")
        or parts[2]
        not in ("assessment.json", INHERITANCE_FILENAME, SCENARIO_CONTRACT_FILENAME)
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
        task_dir = _readiness_task(root, raw_path)
        if task_dir is None:
            continue
        result = validate_task_dir(task_dir)
        if result.ready:
            scenario = validate_scenario_readiness(task_dir, root)
            if scenario.enabled and not scenario.allowed:
                reason = "; ".join(scenario.errors[:3]) or "scenario proof is not ready"
                return _block(f"scenario proof is not ready: {reason}")
            record_binding(root, session_id, task_dir)
            return 0
        label = (
            "assessment"
            if Path(raw_path).name == "assessment.json"
            else "readiness proof"
        )
        return _block(f"{label} is not ready: {_not_ready_reason(result)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("pre", "bind"), required=True)
    args = parser.parse_args()
    event = read_hook_input(LABEL)
    if event is None:
        return 0
    if args.mode == "bind":
        return run_fail_open(LABEL, lambda: run_bind(event))
    return run_fail_open(LABEL, lambda: run_pre(event))


if __name__ == "__main__":
    sys.exit(main())
