#!/usr/bin/env python3
"""Stop hook that enforces fresh scenario evidence for a bound task."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from readiness_state import load_binding  # noqa: E402
from scenario_gate import load_policy, validate_completion  # noqa: E402
from transcript import note, read_hook_input  # noqa: E402

LABEL = "scenario-gate"


def _block(reason: str) -> int:
    print(json.dumps({"decision": "block", "reason": f"[agent-gate] {reason}"}))
    return 0


def _project_root(event: dict[str, Any]) -> Path | None:
    raw = event.get("cwd")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        root = Path(raw).resolve(strict=True)
        return root if root.is_dir() else None
    except (OSError, RuntimeError):
        return None


def run_stop(event: dict[str, Any]) -> int:
    root = _project_root(event)
    if root is None:
        note(LABEL, "project directory is unavailable, fail-open")
        return 0
    policy, policy_errors = load_policy(root)
    if policy is None and not policy_errors:
        return 0
    if policy is None:
        return _block("scenario completion policy is invalid: " + "; ".join(policy_errors[:3]))

    try:
        session_id = event.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            note(LABEL, "session identifier is unavailable, fail-open")
            return 0
        state, task_dir = load_binding(root, session_id)
        if state == "absent":
            return 0
        if state != "bound" or task_dir is None:
            return _block("scenario completion has an unsafe readiness session marker")
        result = validate_completion(task_dir, root)
        if not result.allowed:
            reason = "; ".join(result.errors[:3]) or "required scenario evidence is missing"
            return _block(f"scenario completion is not ready: {reason}")
        return 0
    except Exception as exc:
        return _block(f"scenario completion failed safely: {type(exc).__name__}")


def main() -> int:
    event = read_hook_input(LABEL)
    if event is None:
        return 0
    return run_stop(event)


if __name__ == "__main__":
    sys.exit(main())
