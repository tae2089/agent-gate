#!/usr/bin/env python3
"""Bounded diagnosis and repair workflow for the Agent Loop debug pack."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

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
    attach_managed_subloop,
    load_managed_run,
    resolve_managed_run,
    start_managed_run,
    terminate_managed_run,
    transition_managed_run,
)
from scenario_gate import source_fingerprint, validate_completion
from subloop_contract import PackProfile, SUBLOOP_PERMISSIONS

REQUEST_SCHEMA_VERSION = 1
DIAGNOSIS_SCHEMA_VERSION = 1
REQUEST_FIELDS = frozenset(
    {
        "schema_version",
        "source",
        "source_ref",
        "request",
        "symptom",
        "scope",
        "permissions",
        "evidence",
    }
)
DIAGNOSIS_FIELDS = frozenset(
    {
        "schema_version",
        "request_sha256",
        "resolution",
        "root_cause",
        "reproduction",
        "evidence",
        "proposed_fix",
    }
)
RESOLUTIONS = frozenset(
    {"diagnosed", "fix-required", "needs-decision", "blocked"}
)
PHASE_TRANSITIONS = {
    "frame": frozenset({"reproduce"}),
    "reproduce": frozenset({"diagnose"}),
    "diagnose": frozenset(
        {"fix", "completed", "needs-decision", "blocked"}
    ),
    "fix": frozenset({"verify"}),
    "verify": frozenset({"fix", "completed"}),
}
TERMINAL_STATUSES = frozenset(
    {
        "completed",
        "changes-requested",
        "needs-decision",
        "blocked",
        "budget-exhausted",
    }
)
DEBUG_DEFINITION = LoopDefinition(
    name="debug",
    transitions=PHASE_TRANSITIONS,
    terminal_statuses=TERMINAL_STATUSES,
    iteration_transitions=frozenset({("verify", "fix")}),
    budget_terminal="budget-exhausted",
)
ACTIVE_RUN_FILENAME = ".active-run"
REQUEST_FILENAME = "debug-request.json"
STATE_FILENAME = "debug-state.json"
DEBUG_RUN = ManagedLoopDefinition(
    loop=DEBUG_DEFINITION,
    input_filename=REQUEST_FILENAME,
    state_filename=STATE_FILENAME,
    active_pointer_filename=ACTIVE_RUN_FILENAME,
    input_hash_field="request_sha256",
    initial_status="frame",
    interrupt_terminals=frozenset({"needs-decision", "blocked"}),
)
DEBUG_SUBLOOP_RUN = ManagedLoopDefinition(
    loop=DEBUG_DEFINITION,
    input_filename="invocation.json",
    state_filename=STATE_FILENAME,
    active_pointer_filename=None,
    input_hash_field="request_sha256",
    initial_status="frame",
    interrupt_terminals=frozenset({"needs-decision", "blocked"}),
)
PACK_PROFILE = PackProfile(
    name="debug-loop",
    supported_modes=frozenset({"standalone", "subloop"}),
)


@dataclass(frozen=True)
class ArtifactValidation:
    allowed: bool
    errors: tuple[str, ...]
    value: Mapping[str, Any]


def _exact_fields(
    value: Mapping[str, Any],
    expected: frozenset[str],
    label: str,
    errors: list[str],
) -> None:
    unknown = sorted(value.keys() - expected)
    missing = sorted(expected - value.keys())
    if unknown:
        errors.append(f"{label} has unknown fields: {', '.join(unknown)}")
    if missing:
        errors.append(f"{label} is missing fields: {', '.join(missing)}")


def _non_empty_string(value: Any, label: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{label} must be a non-empty string")


def _string_list(
    value: Any,
    label: str,
    errors: list[str],
    *,
    allow_empty: bool = False,
) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        qualifier = "a list" if allow_empty else "a non-empty list"
        errors.append(f"{label} must be {qualifier}")
        return []
    if any(not isinstance(item, str) or not item.strip() for item in value):
        errors.append(f"{label} contains an invalid string")
        return []
    if len(value) != len(set(value)):
        errors.append(f"{label} must not contain duplicates")
    return list(value)


def _valid_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def validate_request(value: Any) -> ArtifactValidation:
    if not isinstance(value, dict):
        return ArtifactValidation(False, ("debug request must be an object",), {})
    errors: list[str] = []
    _exact_fields(value, REQUEST_FIELDS, "debug request", errors)
    if value.get("schema_version") != REQUEST_SCHEMA_VERSION:
        errors.append(
            f"debug request schema_version must be {REQUEST_SCHEMA_VERSION}"
        )
    if value.get("source") != "manual":
        errors.append("debug request source must be manual")
    for field, label in (
        ("source_ref", "debug request source_ref"),
        ("request", "debug request"),
        ("symptom", "debug symptom"),
    ):
        _non_empty_string(value.get(field), label, errors)
    _string_list(value.get("scope"), "debug scope", errors)
    permissions = _string_list(
        value.get("permissions"),
        "debug permissions",
        errors,
        allow_empty=True,
    )
    unsupported = sorted(set(permissions) - SUBLOOP_PERMISSIONS)
    if unsupported:
        errors.append(
            "debug request has unsupported permissions: " + ", ".join(unsupported)
        )
    _string_list(value.get("evidence"), "debug evidence", errors)
    normalized = {field: value[field] for field in REQUEST_FIELDS if field in value}
    return ArtifactValidation(not errors, tuple(dict.fromkeys(errors)), normalized)


def validate_diagnosis(value: Any) -> ArtifactValidation:
    if not isinstance(value, dict):
        return ArtifactValidation(False, ("diagnosis must be an object",), {})
    errors: list[str] = []
    _exact_fields(value, DIAGNOSIS_FIELDS, "diagnosis", errors)
    if value.get("schema_version") != DIAGNOSIS_SCHEMA_VERSION:
        errors.append(
            f"diagnosis schema_version must be {DIAGNOSIS_SCHEMA_VERSION}"
        )
    if not _valid_sha256(value.get("request_sha256")):
        errors.append("diagnosis request_sha256 must be a lowercase SHA-256")
    if value.get("resolution") not in RESOLUTIONS:
        errors.append(
            "diagnosis resolution must be diagnosed, fix-required, "
            "needs-decision, or blocked"
        )
    _non_empty_string(value.get("root_cause"), "diagnosis root_cause", errors)
    _string_list(value.get("reproduction"), "diagnosis reproduction", errors)
    _string_list(value.get("evidence"), "diagnosis evidence", errors)
    _non_empty_string(value.get("proposed_fix"), "diagnosis proposed_fix", errors)
    normalized = {
        field: value[field] for field in DIAGNOSIS_FIELDS if field in value
    }
    return ArtifactValidation(not errors, tuple(dict.fromkeys(errors)), normalized)


def _definition(task_dir: Path) -> ManagedLoopDefinition:
    return (
        DEBUG_SUBLOOP_RUN
        if (Path(task_dir) / "invocation.json").is_file()
        else DEBUG_RUN
    )


def _read_json(
    path: Path,
    label: str,
) -> tuple[Any, tuple[str, ...]]:
    if path.is_symlink():
        return None, (f"{label} must not be a symlink",)
    try:
        return json.loads(path.read_text(encoding="utf-8")), ()
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, (f"cannot read {label}: {exc}",)


def _permissions(task_dir: Path) -> tuple[list[str], tuple[str, ...]]:
    task = Path(task_dir)
    definition = _definition(task)
    value, errors = _read_json(task / definition.input_filename, "debug input")
    if errors:
        return [], errors
    if definition is DEBUG_RUN:
        validation = validate_request(value)
        if not validation.allowed:
            return [], validation.errors
        return list(validation.value["permissions"]), ()
    permissions = value.get("permissions") if isinstance(value, dict) else None
    if not isinstance(permissions, list):
        return [], ("debug Subloop invocation permissions must be a list",)
    return list(permissions), ()


def load_run(task_dir: Path) -> LoopResult:
    task = Path(task_dir)
    return load_managed_run(_definition(task), task)


def start_run(
    task_dir: Path,
    request: Any,
    max_iterations: int = 3,
) -> LoopResult:
    validation = validate_request(request)
    if not validation.allowed:
        return LoopResult(False, validation.errors, {})
    return start_managed_run(
        DEBUG_RUN,
        Path(task_dir),
        validation.value,
        max_iterations,
    )


def attach_subloop(
    task_dir: Path,
    project_root: Path,
) -> LoopResult:
    task = Path(task_dir)
    invocation, errors = _read_json(
        task / "invocation.json",
        "debug Subloop invocation",
    )
    if errors:
        return LoopResult(False, errors, {})
    if (
        not isinstance(invocation, dict)
        or invocation.get("pack") != PACK_PROFILE.name
        or invocation.get("mode") != "subloop"
    ):
        return LoopResult(
            False,
            ("debug Subloop invocation has the wrong pack or mode",),
            {},
        )
    budget = invocation.get("budget")
    max_iterations = (
        budget.get("iteration_limit") if isinstance(budget, dict) else None
    )
    if isinstance(max_iterations, bool) or not isinstance(max_iterations, int):
        return LoopResult(
            False,
            ("debug Subloop invocation has an invalid iteration budget",),
            {},
        )
    return attach_managed_subloop(
        DEBUG_SUBLOOP_RUN,
        task,
        Path(project_root),
        max_iterations,
    )


def transition_run(task_dir: Path, next_phase: str) -> LoopResult:
    task = Path(task_dir)
    loaded = load_run(task)
    if not loaded.allowed:
        return loaded
    allowed_edges = {
        ("frame", "reproduce"),
        ("reproduce", "diagnose"),
        ("fix", "verify"),
    }
    edge = (loaded.state["status"], next_phase)
    if edge not in allowed_edges:
        return LoopResult(
            False,
            (
                f"debug transition {edge[0]} -> {edge[1]} "
                "requires a guarded command",
            ),
            loaded.state,
        )
    return transition_managed_run(_definition(task), task, next_phase)


def submit_diagnosis(task_dir: Path, diagnosis: Any) -> LoopResult:
    task = Path(task_dir)
    definition = _definition(task)
    loaded = load_managed_run(definition, task)
    if not loaded.allowed:
        return loaded
    if loaded.state["status"] != "diagnose":
        return LoopResult(
            False,
            ("debug run must be in diagnose phase",),
            loaded.state,
        )
    validation = validate_diagnosis(diagnosis)
    if not validation.allowed:
        return LoopResult(False, validation.errors, loaded.state)
    value = validation.value
    if value["request_sha256"] != loaded.state["request_sha256"]:
        return LoopResult(
            False,
            ("diagnosis request_sha256 does not match the active run",),
            loaded.state,
        )
    diagnosis_path = (
        task
        / "iterations"
        / f"{loaded.state['iteration']:03d}"
        / "diagnosis.json"
    )
    try:
        atomic_write(diagnosis_path, canonical_json(value))
    except OSError as exc:
        return LoopResult(
            False,
            (f"cannot persist diagnosis: {exc}",),
            loaded.state,
        )
    resolution = value["resolution"]
    if resolution == "needs-decision":
        target = "needs-decision"
    elif resolution == "blocked":
        target = "blocked"
    elif resolution == "fix-required":
        permissions, errors = _permissions(task)
        if errors:
            return LoopResult(False, errors, loaded.state)
        target = "fix" if "modify-worktree" in permissions else "completed"
    else:
        target = "completed"
    return transition_managed_run(definition, task, target)


def _completion_task(task_dir: Path, project_root: Path) -> tuple[Path | None, tuple[str, ...]]:
    task = Path(task_dir)
    if _definition(task) is DEBUG_RUN:
        return task, ()
    invocation, errors = _read_json(
        task / "invocation.json",
        "debug Subloop invocation",
    )
    if errors:
        return None, errors
    ref = invocation.get("completion_task_ref") if isinstance(invocation, dict) else None
    if not isinstance(ref, str) or not ref:
        return None, ("debug Subloop completion_task_ref is invalid",)
    try:
        root = Path(project_root).resolve(strict=True)
        completion_task = (root / ref).resolve(strict=True)
        completion_task.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        return None, (f"cannot resolve debug Completion task: {exc}",)
    return completion_task, ()


def complete_run(task_dir: Path, project_root: Path) -> LoopResult:
    task = Path(task_dir)
    definition = _definition(task)
    loaded = load_managed_run(definition, task)
    if not loaded.allowed:
        return loaded
    if loaded.state["status"] != "verify":
        return LoopResult(
            False,
            ("debug run must be in verify phase",),
            loaded.state,
        )
    completion_task, errors = _completion_task(task, Path(project_root))
    if errors:
        return LoopResult(False, errors, loaded.state)
    assert completion_task is not None
    completion = validate_completion(completion_task, Path(project_root))
    if not completion.allowed:
        return LoopResult(False, completion.errors, loaded.state)
    return transition_managed_run(definition, task, "completed")


def terminate_run(task_dir: Path, status: str) -> LoopResult:
    task = Path(task_dir)
    return terminate_managed_run(_definition(task), task, status)


def build_subloop_result(
    invocation: Mapping[str, Any],
    diagnosis: Any,
    *,
    source_snapshot_after_sha256: str,
) -> Mapping[str, Any]:
    if (
        not isinstance(invocation, dict)
        or invocation.get("pack") != PACK_PROFILE.name
        or invocation.get("mode") != "subloop"
    ):
        raise ValueError("Debug Subloop requires a bound debug invocation")
    validation = validate_diagnosis(diagnosis)
    if not validation.allowed:
        raise ValueError("; ".join(validation.errors))
    value = validation.value
    resolution = value["resolution"]
    if resolution in {"diagnosed", "fix-required"}:
        status = "completed"
        decision = None
    elif resolution == "needs-decision":
        status = "needs-decision"
        decision = {
            "question": value["root_cause"],
            "options": [value["proposed_fix"]],
        }
    else:
        status = "blocked"
        decision = None
    return {
        "schema_version": 1,
        "invocation_id": invocation["invocation_id"],
        "invocation_sha256": content_sha256(canonical_json(invocation)),
        "pack": PACK_PROFILE.name,
        "status": status,
        "summary": value["root_cause"],
        "finding_refs": ["diagnosis.json"],
        "changed_paths": [],
        "evidence_refs": ["diagnosis.json"],
        "budget_usage": {"iterations_used": 1},
        "completion_receipt": None,
        "decision": decision,
        "source_snapshot_after_sha256": source_snapshot_after_sha256,
    }


def prepare_subloop_result(
    task_dir: Path,
    project_root: Path,
    diagnosis: Any,
) -> LoopResult:
    task = Path(task_dir)
    invocation, errors = _read_json(
        task / "invocation.json",
        "debug Subloop invocation",
    )
    if errors:
        return LoopResult(False, errors, {})
    fingerprint, fingerprint_errors = source_fingerprint(Path(project_root))
    if fingerprint is None:
        return LoopResult(False, fingerprint_errors, {})
    try:
        result = build_subloop_result(
            invocation,
            diagnosis,
            source_snapshot_after_sha256=fingerprint,
        )
        atomic_write(task / "diagnosis.json", canonical_json(diagnosis))
        atomic_write(task / "result-input.json", canonical_json(result))
    except (OSError, ValueError) as exc:
        return LoopResult(False, (f"cannot prepare Debug Subloop result: {exc}",), {})
    return LoopResult(True, (), result)


def _print_payload(
    result: LoopResult,
    as_json: bool,
    task: Path | None = None,
) -> None:
    payload = {
        "allowed": result.allowed,
        "errors": list(result.errors),
        "state": dict(result.state),
    }
    if task is not None:
        payload["task"] = str(task.resolve())
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    print("PASS" if result.allowed else "BLOCK")
    for error in result.errors:
        print(f"  error: {error}")
    if result.state:
        print(f"  status: {result.state.get('status')}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)

    start = subparsers.add_parser("start")
    start.add_argument("task", type=Path)
    start.add_argument("--request", required=True, type=Path)
    start.add_argument("--project-root", required=True, type=Path)
    start.add_argument("--max-iterations", type=int, default=3)
    start.add_argument("--json", action="store_true")

    attach = subparsers.add_parser("attach-subloop")
    attach.add_argument("task", type=Path)
    attach.add_argument("--project-root", required=True, type=Path)
    attach.add_argument("--json", action="store_true")

    transition_parser = subparsers.add_parser("transition")
    transition_parser.add_argument("task", type=Path)
    transition_parser.add_argument(
        "next_phase",
        choices=("reproduce", "diagnose", "verify"),
    )
    transition_parser.add_argument("--project-root", required=True, type=Path)
    transition_parser.add_argument("--json", action="store_true")

    diagnose = subparsers.add_parser("diagnose")
    diagnose.add_argument("task", type=Path)
    diagnose.add_argument("--diagnosis", required=True, type=Path)
    diagnose.add_argument("--project-root", required=True, type=Path)
    diagnose.add_argument("--json", action="store_true")

    complete = subparsers.add_parser("complete")
    complete.add_argument("task", type=Path)
    complete.add_argument("--project-root", required=True, type=Path)
    complete.add_argument("--json", action="store_true")

    terminate = subparsers.add_parser("terminate")
    terminate.add_argument("task", type=Path)
    terminate.add_argument("status", choices=("needs-decision", "blocked"))
    terminate.add_argument("--project-root", required=True, type=Path)
    terminate.add_argument("--json", action="store_true")

    status = subparsers.add_parser("status")
    status.add_argument("task", type=Path, nargs="?")
    status.add_argument("--project-root", required=True, type=Path)
    status.add_argument("--json", action="store_true")

    prepare = subparsers.add_parser("prepare-subloop-result")
    prepare.add_argument("task", type=Path)
    prepare.add_argument("--diagnosis", required=True, type=Path)
    prepare.add_argument("--project-root", required=True, type=Path)
    prepare.add_argument("--json", action="store_true")

    args = parser.parse_args()
    if args.operation == "status" and args.task is None:
        task, errors = resolve_managed_run(DEBUG_RUN, args.project_root)
        result = LoopResult(False, errors, {}) if task is None else load_run(task)
        _print_payload(result, args.json, task)
        return 0 if result.allowed else 1
    if args.operation in {"attach-subloop", "prepare-subloop-result"}:
        task = args.task
    else:
        task, errors = direct_workspace_task(args.task, args.project_root)
        if task is None:
            result = LoopResult(False, errors, {})
            _print_payload(result, args.json)
            return 1

    if args.operation == "start":
        value, errors = _read_json(args.request, "debug request")
        result = (
            LoopResult(False, errors, {})
            if errors
            else start_run(task, value, args.max_iterations)
        )
    elif args.operation == "attach-subloop":
        result = attach_subloop(task, args.project_root)
    elif args.operation == "transition":
        result = transition_run(task, args.next_phase)
    elif args.operation == "diagnose":
        value, errors = _read_json(args.diagnosis, "diagnosis")
        result = (
            LoopResult(False, errors, {})
            if errors
            else submit_diagnosis(task, value)
        )
    elif args.operation == "complete":
        result = complete_run(task, args.project_root)
    elif args.operation == "terminate":
        result = terminate_run(task, args.status)
    elif args.operation == "prepare-subloop-result":
        value, errors = _read_json(args.diagnosis, "diagnosis")
        result = (
            LoopResult(False, errors, {})
            if errors
            else prepare_subloop_result(task, args.project_root, value)
        )
    else:
        result = load_run(task)

    _print_payload(result, args.json, task)
    return 0 if result.allowed else 1


if __name__ == "__main__":
    sys.exit(main())
