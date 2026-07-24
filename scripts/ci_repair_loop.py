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
)
from loop_runtime import (
    ManagedLoopDefinition,
    load_managed_run,
    resolve_managed_run,
    start_managed_run,
    terminate_managed_run,
    transition_managed_run,
)
from scenario_gate import source_fingerprint, validate_completion
from subloop_contract import PackProfile, validate_result

FAILURE_SCHEMA_VERSION = 1
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
ACTIVE_RUN_FILENAME = ".active-run"
FAILURE_FILENAME = "ci-failure.json"
STATE_FILENAME = "ci-repair-state.json"
CI_REPAIR_RUN = ManagedLoopDefinition(
    loop=CI_REPAIR_DEFINITION,
    input_filename=FAILURE_FILENAME,
    state_filename=STATE_FILENAME,
    active_pointer_filename=ACTIVE_RUN_FILENAME,
    input_hash_field="failure_sha256",
    initial_status="inspect",
    interrupt_terminals=frozenset({"needs-clarification", "blocked"}),
)
PACK_PROFILE = PackProfile(
    name="ci-repair-loop",
    supported_modes=frozenset({"standalone", "subloop"}),
)
SUBLOOP_OUTCOME_FIELDS = frozenset(
    {
        "status",
        "summary",
        "finding_refs",
        "changed_paths",
        "evidence_refs",
        "iterations_used",
        "scenario_result_sha256",
        "decision",
    }
)
SUBLOOP_OUTCOME_STATUSES = frozenset(
    {
        "checks-green",
        "changes-requested",
        "needs-decision",
        "blocked",
        "budget-exhausted",
    }
)


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


def _active_ci_repair_task(
    project_root: Path,
) -> tuple[Path | None, tuple[str, ...]]:
    return resolve_managed_run(CI_REPAIR_RUN, project_root)


def start_run(
    task_dir: Path,
    failure: Any,
    max_iterations: int = 3,
) -> LoopResult:
    validation = validate_failure(failure)
    if not validation.allowed:
        return LoopResult(False, validation.errors, {})
    return start_managed_run(
        CI_REPAIR_RUN,
        Path(task_dir),
        validation.failure,
        max_iterations,
    )


def _persist_transition(
    task_dir: Path,
    next_phase: str,
    *,
    completion_authorized: bool = False,
) -> LoopResult:
    loaded = load_managed_run(CI_REPAIR_RUN, task_dir)
    if not loaded.allowed:
        return loaded
    if next_phase == "checks-green" and not completion_authorized:
        return LoopResult(
            False,
            ("checks-green requires current Completion evidence",),
            loaded.state,
        )
    return transition_managed_run(CI_REPAIR_RUN, task_dir, next_phase)


def transition_run(task_dir: Path, next_phase: str) -> LoopResult:
    return _persist_transition(Path(task_dir), next_phase)


def complete_run(task_dir: Path, project_root: Path) -> LoopResult:
    task = Path(task_dir)
    loaded = load_managed_run(CI_REPAIR_RUN, task)
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
    return terminate_managed_run(CI_REPAIR_RUN, Path(task_dir), status)


def build_subloop_result(
    invocation: Mapping[str, Any],
    outcome: Any,
    *,
    source_snapshot_after_sha256: str,
) -> Mapping[str, Any]:
    if (
        not isinstance(invocation, dict)
        or invocation.get("pack") != PACK_PROFILE.name
        or invocation.get("mode") != "subloop"
    ):
        raise ValueError("CI Repair Subloop requires a bound CI invocation")
    if not isinstance(outcome, dict):
        raise ValueError("CI Repair Subloop outcome must be an object")
    unknown = sorted(outcome.keys() - SUBLOOP_OUTCOME_FIELDS)
    missing = sorted(SUBLOOP_OUTCOME_FIELDS - outcome.keys())
    if unknown or missing:
        details = []
        if unknown:
            details.append("unknown fields: " + ", ".join(unknown))
        if missing:
            details.append("missing fields: " + ", ".join(missing))
        raise ValueError("CI Repair Subloop outcome " + "; ".join(details))
    status = outcome["status"]
    if status not in SUBLOOP_OUTCOME_STATUSES:
        raise ValueError("CI Repair Subloop outcome status is unsupported")
    mapped_status = "completed" if status == "checks-green" else status
    scenario_sha = outcome["scenario_result_sha256"]
    completion_receipt = (
        {
            "task_ref": invocation["completion_task_ref"],
            "scenario_result_sha256": scenario_sha,
        }
        if status == "checks-green"
        else None
    )
    return {
        "schema_version": 1,
        "invocation_id": invocation["invocation_id"],
        "invocation_sha256": content_sha256(canonical_json(invocation)),
        "pack": PACK_PROFILE.name,
        "status": mapped_status,
        "summary": outcome["summary"],
        "finding_refs": outcome["finding_refs"],
        "changed_paths": outcome["changed_paths"],
        "evidence_refs": outcome["evidence_refs"],
        "budget_usage": {"iterations_used": outcome["iterations_used"]},
        "completion_receipt": completion_receipt,
        "decision": outcome["decision"],
        "source_snapshot_after_sha256": source_snapshot_after_sha256,
    }


def _persist_once_or_match(
    path: Path,
    content: bytes,
    label: str,
) -> tuple[str, ...]:
    if path.is_symlink():
        return (f"{label} must not be a symlink",)
    try:
        existing = path.read_bytes()
    except FileNotFoundError:
        try:
            atomic_write(path, content)
        except OSError as exc:
            return (f"cannot persist {label}: {exc}",)
        return ()
    except OSError as exc:
        return (f"cannot read {label}: {exc}",)
    if existing != content:
        return (f"{label} already exists with different content",)
    return ()


def prepare_subloop_result(
    task_dir: Path,
    project_root: Path,
    outcome: Any,
) -> LoopResult:
    task = Path(task_dir)
    invocation, errors = _read_json_artifact(
        task / "invocation.json",
        "CI Repair Subloop invocation",
    )
    if errors:
        return LoopResult(False, errors, {})
    fingerprint, fingerprint_errors = source_fingerprint(Path(project_root))
    if fingerprint is None:
        return LoopResult(False, fingerprint_errors, {})
    try:
        result = build_subloop_result(
            invocation,
            outcome,
            source_snapshot_after_sha256=fingerprint,
        )
    except ValueError as exc:
        return LoopResult(False, (str(exc),), {})
    validation = validate_result(
        result,
        invocation,
        current_source_snapshot_sha256=fingerprint,
    )
    if not validation.allowed:
        return LoopResult(False, validation.errors, {})
    for path, content, label in (
        (
            task / "ci-repair-report.json",
            canonical_json(outcome),
            "CI Repair Subloop report",
        ),
        (
            task / "result-input.json",
            canonical_json(validation.value),
            "CI Repair Subloop result",
        ),
    ):
        persist_errors = _persist_once_or_match(path, content, label)
        if persist_errors:
            return LoopResult(False, persist_errors, {})
    return LoopResult(True, (), validation.value)


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

    subloop = subparsers.add_parser("prepare-subloop-result")
    subloop.add_argument("task", type=Path)
    subloop.add_argument("--outcome", required=True, type=Path)
    subloop.add_argument("--project-root", required=True, type=Path)
    subloop.add_argument("--json", action="store_true")

    args = parser.parse_args()
    if args.operation == "prepare-subloop-result":
        outcome, errors = _read_json_artifact(
            args.outcome,
            "CI Repair Subloop outcome",
        )
        result = (
            LoopResult(False, errors, {})
            if errors
            else prepare_subloop_result(args.task, args.project_root, outcome)
        )
        _print_payload(result, args.json)
        return 0 if result.allowed else 1
    if args.operation == "status" and args.task is None:
        task, errors = _active_ci_repair_task(args.project_root)
        result = (
            LoopResult(False, errors, {})
            if task is None
            else load_managed_run(CI_REPAIR_RUN, task)
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
        result = load_managed_run(CI_REPAIR_RUN, task)

    _print_payload(result, args.json)
    return 0 if result.allowed else 1


if __name__ == "__main__":
    sys.exit(main())
