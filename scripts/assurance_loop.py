#!/usr/bin/env python3
"""Deterministic contracts for the Agent Loop assurance pack."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from loop_engine import (
    LoopDefinition,
    LoopResult,
    atomic_write,
    canonical_json,
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
from scenario_gate import validate_completion, validate_scenario_receipt

REQUEST_SCHEMA_VERSION = 1
REPORT_SCHEMA_VERSION = 1
REQUEST_FIELDS = frozenset(
    {
        "schema_version",
        "source",
        "source_ref",
        "request",
        "target",
        "scope",
        "evidence",
    }
)
REPORT_FIELDS = frozenset(
    {
        "schema_version",
        "request_sha256",
        "scenario_result_sha256",
        "verdict",
        "findings",
    }
)
FINDING_FIELDS = frozenset({"id", "severity", "title", "evidence", "action"})
FINDING_ID = re.compile(r"R-[0-9]{3}")
SEVERITIES = frozenset({"P0", "P1", "P2", "P3"})
VERDICTS = frozenset({"actionable", "clean"})
PHASE_TRANSITIONS = {
    "inspect": frozenset({"review"}),
    "review": frozenset({"address", "review-clean"}),
    "address": frozenset({"verify"}),
    "verify": frozenset({"review"}),
}
TERMINAL_STATUSES = frozenset(
    {
        "review-clean",
        "needs-clarification",
        "blocked",
        "budget-exhausted",
    }
)
REVIEW_DEFINITION = LoopDefinition(
    name="review",
    transitions=PHASE_TRANSITIONS,
    terminal_statuses=TERMINAL_STATUSES,
    iteration_transitions=frozenset({("verify", "review")}),
    budget_terminal="budget-exhausted",
)
ACTIVE_REVIEW_FILENAME = ".active-review"
REQUEST_FILENAME = "review-request.json"
STATE_FILENAME = "review-state.json"
REVIEW_RUN = ManagedLoopDefinition(
    loop=REVIEW_DEFINITION,
    input_filename=REQUEST_FILENAME,
    state_filename=STATE_FILENAME,
    active_pointer_filename=ACTIVE_REVIEW_FILENAME,
    input_hash_field="request_sha256",
    initial_status="inspect",
    interrupt_terminals=frozenset({"needs-clarification", "blocked"}),
)


@dataclass(frozen=True)
class ArtifactValidation:
    allowed: bool
    errors: tuple[str, ...]
    value: Mapping[str, Any]


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
    return list(value)


def _exact_fields(
    value: Mapping[str, Any],
    fields: frozenset[str],
    label: str,
    errors: list[str],
) -> None:
    unknown = sorted(value.keys() - fields)
    missing = sorted(fields - value.keys())
    if unknown:
        errors.append(f"{label} has unknown fields: {', '.join(unknown)}")
    if missing:
        errors.append(f"{label} is missing fields: {', '.join(missing)}")


def _valid_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def validate_request(value: Any) -> ArtifactValidation:
    if not isinstance(value, dict):
        return ArtifactValidation(False, ("review request must be an object",), {})
    errors: list[str] = []
    _exact_fields(value, REQUEST_FIELDS, "review request", errors)
    if value.get("schema_version") != REQUEST_SCHEMA_VERSION:
        errors.append(f"review request schema_version must be {REQUEST_SCHEMA_VERSION}")
    _non_empty_string(value.get("source"), "review request source", errors)
    _non_empty_string(
        value.get("source_ref"),
        "review request source_ref",
        errors,
    )
    _non_empty_string(value.get("request"), "user request", errors)
    _non_empty_string(value.get("target"), "review target", errors)
    _string_list(value.get("scope"), "review scope", errors)
    _string_list(value.get("evidence"), "review evidence", errors)
    if value.get("source") != "manual":
        errors.append("review request source must be manual")
    normalized = {field: value[field] for field in REQUEST_FIELDS if field in value}
    return ArtifactValidation(
        not errors,
        tuple(dict.fromkeys(errors)),
        normalized,
    )


def _validate_finding(
    value: Any,
    index: int,
    errors: list[str],
) -> Mapping[str, Any]:
    label = f"review finding[{index}]"
    if not isinstance(value, dict):
        errors.append(f"{label} must be an object")
        return {}
    _exact_fields(value, FINDING_FIELDS, label, errors)
    finding_id = value.get("id")
    if not isinstance(finding_id, str) or FINDING_ID.fullmatch(finding_id) is None:
        errors.append(f"{label}.id must match R-NNN")
    if value.get("severity") not in SEVERITIES:
        errors.append(f"{label}.severity must be P0, P1, P2, or P3")
    _non_empty_string(value.get("title"), f"{label}.title", errors)
    _string_list(value.get("evidence"), f"{label}.evidence", errors)
    _non_empty_string(value.get("action"), f"{label}.action", errors)
    return {field: value[field] for field in FINDING_FIELDS if field in value}


def validate_report(value: Any) -> ArtifactValidation:
    if not isinstance(value, dict):
        return ArtifactValidation(False, ("review report must be an object",), {})
    errors: list[str] = []
    _exact_fields(value, REPORT_FIELDS, "review report", errors)
    if value.get("schema_version") != REPORT_SCHEMA_VERSION:
        errors.append(f"review report schema_version must be {REPORT_SCHEMA_VERSION}")
    if not _valid_sha256(value.get("request_sha256")):
        errors.append("review report request_sha256 must be a lowercase SHA-256")
    if not _valid_sha256(value.get("scenario_result_sha256")):
        errors.append(
            "review report scenario_result_sha256 must be a lowercase SHA-256"
        )
    verdict = value.get("verdict")
    if verdict not in VERDICTS:
        errors.append(f"unsupported review verdict: {verdict}")
    raw_findings = value.get("findings")
    findings: list[Mapping[str, Any]] = []
    if not isinstance(raw_findings, list):
        errors.append("review report findings must be a list")
    else:
        findings = [
            _validate_finding(item, index, errors)
            for index, item in enumerate(raw_findings)
        ]
    ids = [item.get("id") for item in findings if item.get("id") is not None]
    if len(ids) != len(set(ids)):
        errors.append("review report finding ids must be unique")
    if verdict == "actionable" and not findings:
        errors.append("actionable review requires at least one finding")
    if verdict == "clean" and findings:
        errors.append("clean review must not contain findings")
    normalized = {field: value[field] for field in REPORT_FIELDS if field in value}
    if "findings" in normalized:
        normalized["findings"] = findings
    return ArtifactValidation(
        not errors,
        tuple(dict.fromkeys(errors)),
        normalized,
    )


def load_run(task_dir: Path) -> LoopResult:
    return load_managed_run(REVIEW_RUN, Path(task_dir))


def start_run(
    task_dir: Path,
    request: Any,
    max_iterations: int = 3,
) -> LoopResult:
    validation = validate_request(request)
    if not validation.allowed:
        return LoopResult(False, validation.errors, {})
    return start_managed_run(
        REVIEW_RUN,
        Path(task_dir),
        validation.value,
        max_iterations,
    )


def transition_run(task_dir: Path, next_phase: str) -> LoopResult:
    task = Path(task_dir)
    loaded = load_run(task)
    if not loaded.allowed:
        return loaded
    allowed_edges = {
        ("inspect", "review"),
        ("address", "verify"),
    }
    edge = (loaded.state["status"], next_phase)
    if edge not in allowed_edges:
        return LoopResult(
            False,
            (f"review transition {edge[0]} -> {edge[1]} requires a guarded command",),
            loaded.state,
        )
    return transition_managed_run(REVIEW_RUN, task, next_phase)


def submit_review(
    task_dir: Path,
    project_root: Path,
    report: Any,
) -> LoopResult:
    task = Path(task_dir)
    loaded = load_run(task)
    if not loaded.allowed:
        return loaded
    if loaded.state["status"] != "review":
        return LoopResult(
            False,
            ("review run must be in review phase",),
            loaded.state,
        )
    validation = validate_report(report)
    if not validation.allowed:
        return LoopResult(False, validation.errors, loaded.state)
    value = validation.value
    if value["request_sha256"] != loaded.state["request_sha256"]:
        return LoopResult(
            False,
            ("review report request_sha256 does not match the active run",),
            loaded.state,
        )
    clean = value["verdict"] == "clean"
    receipt = validate_scenario_receipt(
        task,
        Path(project_root),
        value["scenario_result_sha256"],
        require_completion=clean,
    )
    if not receipt.allowed:
        return LoopResult(False, receipt.errors, loaded.state)
    path = task / "iterations" / f"{loaded.state['iteration']:03d}" / "review.json"
    try:
        atomic_write(path, canonical_json(value))
    except OSError as exc:
        return LoopResult(
            False,
            (f"cannot persist review report: {exc}",),
            loaded.state,
        )
    target = "review-clean" if clean else "address"
    return transition_managed_run(REVIEW_RUN, task, target)


def verify_run(task_dir: Path, project_root: Path) -> LoopResult:
    task = Path(task_dir)
    loaded = load_run(task)
    if not loaded.allowed:
        return loaded
    if loaded.state["status"] != "verify":
        return LoopResult(
            False,
            ("review run must be in verify phase",),
            loaded.state,
        )
    completion = validate_completion(task, Path(project_root))
    if not completion.allowed:
        return LoopResult(False, completion.errors, loaded.state)
    return transition_managed_run(REVIEW_RUN, task, "review")


def terminate_run(task_dir: Path, status: str) -> LoopResult:
    return terminate_managed_run(REVIEW_RUN, Path(task_dir), status)


def _read_json_artifact(
    path: Path,
    label: str,
) -> tuple[Any, tuple[str, ...]]:
    if path.is_symlink():
        return None, (f"{label} must not be a symlink",)
    try:
        return json.loads(path.read_text(encoding="utf-8")), ()
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, (f"cannot read {label}: {exc}",)


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


def _direct_task(
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
    start.add_argument("--request", required=True, type=Path)
    start.add_argument("--project-root", required=True, type=Path)
    start.add_argument("--max-iterations", type=int, default=3)
    start.add_argument("--json", action="store_true")

    transition_parser = subparsers.add_parser("transition")
    transition_parser.add_argument("task", type=Path)
    transition_parser.add_argument("next_phase", choices=("review", "verify"))
    transition_parser.add_argument("--project-root", required=True, type=Path)
    transition_parser.add_argument("--json", action="store_true")

    submit = subparsers.add_parser("submit")
    submit.add_argument("task", type=Path)
    submit.add_argument("--report", required=True, type=Path)
    submit.add_argument("--project-root", required=True, type=Path)
    submit.add_argument("--json", action="store_true")

    verify = subparsers.add_parser("verify")
    verify.add_argument("task", type=Path)
    verify.add_argument("--project-root", required=True, type=Path)
    verify.add_argument("--json", action="store_true")

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
        task, errors = resolve_managed_run(REVIEW_RUN, args.project_root)
        result = LoopResult(False, errors, {}) if task is None else load_run(task)
        _print_payload(result, args.json, task)
        return 0 if result.allowed else 1

    task, error = _direct_task(args.task, args.project_root)
    if error is not None:
        _print_payload(error, args.json)
        return 1
    assert task is not None

    if args.operation == "start":
        value, errors = _read_json_artifact(args.request, "review request")
        result = (
            LoopResult(False, errors, {})
            if errors
            else start_run(task, value, args.max_iterations)
        )
    elif args.operation == "transition":
        result = transition_run(task, args.next_phase)
    elif args.operation == "submit":
        value, errors = _read_json_artifact(args.report, "review report")
        result = (
            LoopResult(False, errors, {})
            if errors
            else submit_review(task, args.project_root, value)
        )
    elif args.operation == "verify":
        result = verify_run(task, args.project_root)
    elif args.operation == "terminate":
        result = terminate_run(task, args.status)
    else:
        result = load_run(task)

    _print_payload(result, args.json, task)
    return 0 if result.allowed else 1


if __name__ == "__main__":
    sys.exit(main())
