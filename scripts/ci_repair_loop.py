#!/usr/bin/env python3
"""Deterministic contracts for the Agent Loop CI repair pack."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from loop_engine import (
    LoopDefinition,
    LoopResult,
    atomic_write,
    canonical_json,
    content_sha256,
    direct_workspace_task,
    resolve_active_run,
    transition,
)
from scenario_gate import validate_completion

FAILURE_SCHEMA_VERSION = 1
STATE_SCHEMA_VERSION = 1
FAILURE_FIELDS = frozenset(
    {
        "schema_version",
        "source",
        "source_ref",
        "title",
        "failing_checks",
        "evidence",
        "request",
    }
)
STATE_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "iteration",
        "max_iterations",
        "failure_sha256",
    }
)
PHASE_TRANSITIONS = {
    "inspect": frozenset({"repair"}),
    "repair": frozenset({"verify"}),
    "verify": frozenset({"repair", "checks-green"}),
}
TERMINAL_STATUSES = frozenset(
    {
        "checks-green",
        "needs-clarification",
        "blocked",
        "budget-exhausted",
    }
)
CI_REPAIR_DEFINITION = LoopDefinition(
    name="CI repair",
    transitions=PHASE_TRANSITIONS,
    terminal_statuses=TERMINAL_STATUSES,
    iteration_transitions=frozenset({("verify", "repair")}),
    budget_terminal="budget-exhausted",
)
ACTIVE_CI_REPAIR_FILENAME = ".active-ci-repair"
FAILURE_FILENAME = "ci-failure.json"
STATE_FILENAME = "ci-repair-state.json"


@dataclass(frozen=True)
class FailureValidation:
    allowed: bool
    errors: tuple[str, ...]
    failure: Mapping[str, Any]


def _non_empty_string(
    value: Any, label: str, errors: list[str]
) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{label} must be a non-empty string")
        return None
    return value


def _string_list(value: Any, label: str, errors: list[str]) -> list[str]:
    if not isinstance(value, list) or not value:
        errors.append(f"{label} must be a non-empty list")
        return []
    if any(not isinstance(item, str) or not item.strip() for item in value):
        errors.append(f"{label} contains an invalid string")
        return []
    return list(value)


def validate_failure(value: Any) -> FailureValidation:
    errors: list[str] = []
    if not isinstance(value, dict):
        return FailureValidation(False, ("CI failure must be an object",), {})

    unknown = sorted(value.keys() - FAILURE_FIELDS)
    missing = sorted(FAILURE_FIELDS - value.keys())
    if unknown:
        errors.append(f"CI failure has unknown fields: {', '.join(unknown)}")
    if missing:
        errors.append(f"CI failure is missing fields: {', '.join(missing)}")
    if value.get("schema_version") != FAILURE_SCHEMA_VERSION:
        errors.append(
            f"CI failure schema_version must be {FAILURE_SCHEMA_VERSION}"
        )
    source = _non_empty_string(value.get("source"), "CI failure source", errors)
    _non_empty_string(value.get("source_ref"), "CI failure source_ref", errors)
    _non_empty_string(value.get("title"), "CI failure title", errors)
    _string_list(value.get("failing_checks"), "CI failing_checks", errors)
    _string_list(value.get("evidence"), "CI failure evidence", errors)
    _non_empty_string(value.get("request"), "user request", errors)
    if source is not None and source != "manual":
        errors.append("CI failure source must be manual")

    normalized = {
        field: value[field]
        for field in FAILURE_FIELDS
        if field in value
    }
    return FailureValidation(
        not errors,
        tuple(dict.fromkeys(errors)),
        normalized,
    )


def _write_state(task_dir: Path, state: Mapping[str, Any]) -> None:
    atomic_write(task_dir / STATE_FILENAME, canonical_json(state))


def _load_state(task_dir: Path) -> LoopResult:
    path = task_dir / STATE_FILENAME
    if path.is_symlink():
        return LoopResult(False, ("CI repair state must not be a symlink",), {})
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return LoopResult(False, (f"cannot read CI repair state: {exc}",), {})
    if not isinstance(value, dict):
        return LoopResult(False, ("CI repair state must be an object",), {})

    errors: list[str] = []
    unknown = sorted(value.keys() - STATE_FIELDS)
    missing = sorted(STATE_FIELDS - value.keys())
    if unknown:
        errors.append(
            f"CI repair state has unknown fields: {', '.join(unknown)}"
        )
    if missing:
        errors.append(
            f"CI repair state is missing fields: {', '.join(missing)}"
        )
    if value.get("schema_version") != STATE_SCHEMA_VERSION:
        errors.append(
            f"CI repair state schema_version must be {STATE_SCHEMA_VERSION}"
        )
    if value.get("status") not in set(PHASE_TRANSITIONS) | TERMINAL_STATUSES:
        errors.append(f"unsupported CI repair status: {value.get('status')}")
    iteration = value.get("iteration")
    max_iterations = value.get("max_iterations")
    if (
        isinstance(iteration, bool)
        or not isinstance(iteration, int)
        or iteration < 1
    ):
        errors.append("CI repair state iteration must be a positive integer")
    if (
        isinstance(max_iterations, bool)
        or not isinstance(max_iterations, int)
        or not 1 <= max_iterations <= 10
    ):
        errors.append(
            "CI repair state max_iterations must be from 1 through 10"
        )
    if (
        isinstance(iteration, int)
        and not isinstance(iteration, bool)
        and isinstance(max_iterations, int)
        and not isinstance(max_iterations, bool)
        and iteration > max_iterations
    ):
        errors.append("CI repair state iteration exceeds max_iterations")
    failure_hash = value.get("failure_sha256")
    if (
        not isinstance(failure_hash, str)
        or len(failure_hash) != 64
        or any(character not in "0123456789abcdef" for character in failure_hash)
    ):
        errors.append(
            "CI repair state failure_sha256 must be a lowercase SHA-256"
        )
    return LoopResult(not errors, tuple(errors), value)


def _active_ci_repair_task(
    project_root: Path,
) -> tuple[Path | None, tuple[str, ...]]:
    return resolve_active_run(
        project_root,
        ACTIVE_CI_REPAIR_FILENAME,
        "CI repair",
    )


def start_run(
    task_dir: Path,
    failure: Any,
    max_iterations: int = 3,
) -> LoopResult:
    validation = validate_failure(failure)
    if not validation.allowed:
        return LoopResult(False, validation.errors, {})
    if (
        isinstance(max_iterations, bool)
        or not isinstance(max_iterations, int)
        or not 1 <= max_iterations <= 10
    ):
        return LoopResult(
            False,
            ("max_iterations must be an integer from 1 through 10",),
            {},
        )
    task = Path(task_dir)
    if not task.is_dir() or task.is_symlink():
        return LoopResult(
            False,
            ("task directory must be a real directory",),
            {},
        )
    if task.parent.name != "_workspace":
        return LoopResult(
            False,
            ("task must be a direct _workspace task",),
            {},
        )
    state_path = task / STATE_FILENAME
    if state_path.exists() or state_path.is_symlink():
        return LoopResult(False, ("CI repair state already exists",), {})

    root = task.parent.parent.resolve(strict=True)
    pointer = task.parent / ACTIVE_CI_REPAIR_FILENAME
    if pointer.exists() or pointer.is_symlink():
        active_task, active_errors = _active_ci_repair_task(root)
        if active_task is None:
            return LoopResult(False, active_errors, {})
        active = _load_state(active_task)
        if not active.allowed:
            return active
        if active.state["status"] in set(PHASE_TRANSITIONS):
            return LoopResult(
                False,
                ("another CI repair run is active",),
                active.state,
            )

    failure_content = canonical_json(validation.failure)
    state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "status": "inspect",
        "iteration": 1,
        "max_iterations": max_iterations,
        "failure_sha256": content_sha256(failure_content),
    }
    try:
        atomic_write(task / FAILURE_FILENAME, failure_content)
        _write_state(task, state)
        relative = task.resolve(strict=True).relative_to(root)
        atomic_write(
            pointer,
            (relative.as_posix() + "\n").encode("utf-8"),
        )
    except OSError as exc:
        return LoopResult(False, (f"cannot start CI repair run: {exc}",), {})
    return LoopResult(True, (), state)


def _persist_transition(
    task_dir: Path,
    next_phase: str,
    *,
    completion_authorized: bool = False,
) -> LoopResult:
    loaded = _load_state(task_dir)
    if not loaded.allowed:
        return loaded
    if next_phase == "checks-green" and not completion_authorized:
        return LoopResult(
            False,
            ("checks-green requires current Completion evidence",),
            loaded.state,
        )
    decision = transition(CI_REPAIR_DEFINITION, loaded.state, next_phase)
    if not decision.allowed:
        return decision
    try:
        _write_state(task_dir, decision.state)
    except OSError as exc:
        return LoopResult(
            False,
            (f"cannot persist CI repair state: {exc}",),
            loaded.state,
        )
    return decision


def transition_run(task_dir: Path, next_phase: str) -> LoopResult:
    return _persist_transition(Path(task_dir), next_phase)


def complete_run(task_dir: Path, project_root: Path) -> LoopResult:
    task = Path(task_dir)
    loaded = _load_state(task)
    if not loaded.allowed:
        return loaded
    if loaded.state["status"] != "verify":
        return LoopResult(
            False,
            ("CI repair run must be in verify phase",),
            loaded.state,
        )
    completion = validate_completion(task, Path(project_root))
    if not completion.allowed:
        return LoopResult(False, completion.errors, loaded.state)
    return _persist_transition(
        task,
        "checks-green",
        completion_authorized=True,
    )


def terminate_run(task_dir: Path, status: str) -> LoopResult:
    task = Path(task_dir)
    loaded = _load_state(task)
    if not loaded.allowed:
        return loaded
    state = dict(loaded.state)
    if state["status"] not in PHASE_TRANSITIONS:
        return LoopResult(
            False,
            ("CI repair run is already terminal",),
            state,
        )
    if status not in {"needs-clarification", "blocked"}:
        return LoopResult(
            False,
            (f"unsupported CI repair terminal status: {status}",),
            state,
        )
    state["status"] = status
    try:
        _write_state(task, state)
    except OSError as exc:
        return LoopResult(
            False,
            (f"cannot persist CI repair state: {exc}",),
            loaded.state,
        )
    return LoopResult(True, (), state)


def _read_json_artifact(path: Path, label: str) -> tuple[Any, tuple[str, ...]]:
    if path.is_symlink():
        return None, (f"{label} must not be a symlink",)
    try:
        return json.loads(path.read_text(encoding="utf-8")), ()
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, (f"cannot read {label}: {exc}",)


def _payload(result: LoopResult) -> dict[str, Any]:
    return {
        "allowed": result.allowed,
        "errors": list(result.errors),
        "state": dict(result.state),
    }


def _print_payload(result: LoopResult, as_json: bool) -> None:
    payload = _payload(result)
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    print("PASS" if result.allowed else "BLOCK")
    for error in result.errors:
        print(f"  error: {error}")
    if result.state:
        print(f"  status: {result.state.get('status')}")


def _task_or_result(
    raw_task: Path,
    project_root: Path,
) -> tuple[Path | None, LoopResult | None]:
    task, errors = direct_workspace_task(raw_task, project_root)
    if task is None:
        return None, LoopResult(False, errors, {})
    return task, None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)

    start = subparsers.add_parser("start")
    start.add_argument("task", type=Path)
    start.add_argument("--failure", required=True, type=Path)
    start.add_argument("--project-root", required=True, type=Path)
    start.add_argument("--max-iterations", type=int, default=3)
    start.add_argument("--json", action="store_true")

    transition_parser = subparsers.add_parser("transition")
    transition_parser.add_argument("task", type=Path)
    transition_parser.add_argument("next_phase", choices=("repair", "verify"))
    transition_parser.add_argument("--project-root", required=True, type=Path)
    transition_parser.add_argument("--json", action="store_true")

    complete = subparsers.add_parser("complete")
    complete.add_argument("task", type=Path)
    complete.add_argument("--project-root", required=True, type=Path)
    complete.add_argument("--json", action="store_true")

    terminate = subparsers.add_parser("terminate")
    terminate.add_argument("task", type=Path)
    terminate.add_argument(
        "status",
        choices=("needs-clarification", "blocked"),
    )
    terminate.add_argument("--project-root", required=True, type=Path)
    terminate.add_argument("--json", action="store_true")

    status = subparsers.add_parser("status")
    status.add_argument("task", type=Path, nargs="?")
    status.add_argument("--project-root", required=True, type=Path)
    status.add_argument("--json", action="store_true")

    args = parser.parse_args()
    if args.operation == "status" and args.task is None:
        task, errors = _active_ci_repair_task(args.project_root)
        result = (
            LoopResult(False, errors, {})
            if task is None
            else _load_state(task)
        )
        _print_payload(result, args.json)
        return 0 if result.allowed else 1

    task, error = _task_or_result(args.task, args.project_root)
    if error is not None:
        _print_payload(error, args.json)
        return 1
    assert task is not None

    if args.operation == "start":
        failure, errors = _read_json_artifact(args.failure, "CI failure")
        result = (
            LoopResult(False, errors, {})
            if errors
            else start_run(task, failure, args.max_iterations)
        )
    elif args.operation == "transition":
        result = transition_run(task, args.next_phase)
    elif args.operation == "complete":
        result = complete_run(task, args.project_root)
    elif args.operation == "terminate":
        result = terminate_run(task, args.status)
    else:
        result = _load_state(task)

    _print_payload(result, args.json)
    return 0 if result.allowed else 1


if __name__ == "__main__":
    sys.exit(main())
