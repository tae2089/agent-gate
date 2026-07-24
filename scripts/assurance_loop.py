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
from scenario_gate import (
    source_fingerprint,
    validate_completion,
    validate_scenario_receipt,
)
from subloop_contract import PackProfile, SUBLOOP_PERMISSIONS

REQUEST_SCHEMA_VERSION = 2
REPORT_SCHEMA_VERSION = 2
REQUEST_FIELDS = frozenset(
    {
        "schema_version",
        "source",
        "source_ref",
        "request",
        "target",
        "requirements",
        "scope",
        "permissions",
        "evidence",
    }
)
REPORT_FIELDS = frozenset(
    {
        "schema_version",
        "request_sha256",
        "scenario_result_sha256",
        "assessments",
    }
)
ASSESSMENT_FIELDS = frozenset({"status", "findings"})
FINDING_FIELDS = frozenset(
    {
        "id",
        "category",
        "severity",
        "title",
        "requirement_refs",
        "evidence",
        "action",
    }
)
FINDING_ID = re.compile(r"A-[0-9]{3}")
SEVERITIES = frozenset({"P0", "P1", "P2", "P3"})
ASSESSMENT_STATUSES = frozenset({"pass", "fail"})
ASSESSMENT_CATEGORIES = frozenset(
    {
        "requirements_conformance",
        "missing_or_overimplemented_requirements",
        "failure_boundary_compatibility",
        "code_quality_module_responsibility",
        "abstraction_complexity",
        "test_quality_regression_prevention",
    }
)
PHASE_TRANSITIONS = {
    "inspect": frozenset({"assess"}),
    "assess": frozenset({"address", "completed", "changes-requested"}),
    "address": frozenset({"verify"}),
    "verify": frozenset({"assess"}),
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
ASSURANCE_DEFINITION = LoopDefinition(
    name="assurance",
    transitions=PHASE_TRANSITIONS,
    terminal_statuses=TERMINAL_STATUSES,
    iteration_transitions=frozenset({("verify", "assess")}),
    budget_terminal="budget-exhausted",
)
ACTIVE_RUN_FILENAME = ".active-run"
REQUEST_FILENAME = "assurance-request.json"
STATE_FILENAME = "assurance-state.json"
ASSURANCE_RUN = ManagedLoopDefinition(
    loop=ASSURANCE_DEFINITION,
    input_filename=REQUEST_FILENAME,
    state_filename=STATE_FILENAME,
    active_pointer_filename=ACTIVE_RUN_FILENAME,
    input_hash_field="request_sha256",
    initial_status="inspect",
    interrupt_terminals=frozenset({"needs-decision", "blocked"}),
)
PACK_PROFILE = PackProfile(
    name="assurance-loop",
    supported_modes=frozenset({"standalone", "subloop"}),
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
        return ArtifactValidation(
            False,
            ("assurance request must be an object",),
            {},
        )
    errors: list[str] = []
    _exact_fields(value, REQUEST_FIELDS, "assurance request", errors)
    if value.get("schema_version") != REQUEST_SCHEMA_VERSION:
        errors.append(
            f"assurance request schema_version must be {REQUEST_SCHEMA_VERSION}"
        )
    _non_empty_string(value.get("source"), "assurance request source", errors)
    _non_empty_string(
        value.get("source_ref"),
        "assurance request source_ref",
        errors,
    )
    _non_empty_string(value.get("request"), "user request", errors)
    _non_empty_string(value.get("target"), "assurance target", errors)
    _string_list(
        value.get("requirements"),
        "assurance requirements",
        errors,
    )
    _string_list(value.get("scope"), "assurance scope", errors)
    permissions = _string_list(
        value.get("permissions"),
        "assurance permissions",
        errors,
    )
    unsupported = sorted(set(permissions) - SUBLOOP_PERMISSIONS)
    if unsupported:
        errors.append(
            "assurance request has unsupported permissions: " + ", ".join(unsupported)
        )
    _string_list(value.get("evidence"), "assurance evidence", errors)
    if value.get("source") != "manual":
        errors.append("assurance request source must be manual")
    normalized = {field: value[field] for field in REQUEST_FIELDS if field in value}
    return ArtifactValidation(
        not errors,
        tuple(dict.fromkeys(errors)),
        normalized,
    )


def _validate_finding(
    value: Any,
    category: str,
    index: int,
    errors: list[str],
) -> Mapping[str, Any]:
    label = f"assurance {category} finding[{index}]"
    if not isinstance(value, dict):
        errors.append(f"{label} must be an object")
        return {}
    _exact_fields(value, FINDING_FIELDS, label, errors)
    finding_id = value.get("id")
    if not isinstance(finding_id, str) or FINDING_ID.fullmatch(finding_id) is None:
        errors.append(f"{label}.id must match A-NNN")
    if value.get("category") != category:
        errors.append(f"{label}.category must match {category}")
    if value.get("severity") not in SEVERITIES:
        errors.append(f"{label}.severity must be P0, P1, P2, or P3")
    _non_empty_string(value.get("title"), f"{label}.title", errors)
    _string_list(
        value.get("requirement_refs"),
        f"{label}.requirement_refs",
        errors,
    )
    _string_list(value.get("evidence"), f"{label}.evidence", errors)
    _non_empty_string(value.get("action"), f"{label}.action", errors)
    return {field: value[field] for field in FINDING_FIELDS if field in value}


def _validate_assessments(
    value: Any,
    errors: list[str],
) -> tuple[Mapping[str, Any], list[Mapping[str, Any]]]:
    if not isinstance(value, dict):
        errors.append("assurance assessments must be an object")
        return {}, []
    _exact_fields(
        value,
        ASSESSMENT_CATEGORIES,
        "assurance assessments",
        errors,
    )
    normalized: dict[str, Any] = {}
    all_findings: list[Mapping[str, Any]] = []
    finding_ids: list[str] = []
    for category in sorted(ASSESSMENT_CATEGORIES & value.keys()):
        raw_axis = value[category]
        label = f"assurance assessment {category}"
        if not isinstance(raw_axis, dict):
            errors.append(f"{label} must be an object")
            continue
        _exact_fields(raw_axis, ASSESSMENT_FIELDS, label, errors)
        status = raw_axis.get("status")
        if status not in ASSESSMENT_STATUSES:
            errors.append(f"{label}.status must be pass or fail")
        raw_findings = raw_axis.get("findings")
        findings: list[Mapping[str, Any]] = []
        if not isinstance(raw_findings, list):
            errors.append(f"{label}.findings must be a list")
        else:
            findings = [
                _validate_finding(item, category, index, errors)
                for index, item in enumerate(raw_findings)
            ]
        if status == "pass" and findings:
            errors.append(f"{label} pass must not contain findings")
        if status == "fail" and not findings:
            errors.append(f"{label} fail requires findings")
        normalized[category] = {
            "status": status,
            "findings": findings,
        }
        all_findings.extend(findings)
        finding_ids.extend(finding["id"] for finding in findings if "id" in finding)
    if len(finding_ids) != len(set(finding_ids)):
        errors.append("assurance finding ids must be unique")
    return normalized, all_findings


def validate_report(value: Any) -> ArtifactValidation:
    if not isinstance(value, dict):
        return ArtifactValidation(
            False,
            ("assurance report must be an object",),
            {},
        )
    errors: list[str] = []
    _exact_fields(value, REPORT_FIELDS, "assurance report", errors)
    if value.get("schema_version") != REPORT_SCHEMA_VERSION:
        errors.append(
            f"assurance report schema_version must be {REPORT_SCHEMA_VERSION}"
        )
    if not _valid_sha256(value.get("request_sha256")):
        errors.append("assurance report request_sha256 must be a lowercase SHA-256")
    if not _valid_sha256(value.get("scenario_result_sha256")):
        errors.append(
            "assurance report scenario_result_sha256 must be a lowercase SHA-256"
        )
    normalized_assessments, _ = _validate_assessments(
        value.get("assessments"),
        errors,
    )
    normalized = {field: value[field] for field in REPORT_FIELDS if field in value}
    if "assessments" in normalized:
        normalized["assessments"] = normalized_assessments
    return ArtifactValidation(
        not errors,
        tuple(dict.fromkeys(errors)),
        normalized,
    )


def load_run(task_dir: Path) -> LoopResult:
    return load_managed_run(ASSURANCE_RUN, Path(task_dir))


def start_run(
    task_dir: Path,
    request: Any,
    max_iterations: int = 3,
) -> LoopResult:
    validation = validate_request(request)
    if not validation.allowed:
        return LoopResult(False, validation.errors, {})
    return start_managed_run(
        ASSURANCE_RUN,
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
        ("inspect", "assess"),
        ("address", "verify"),
    }
    edge = (loaded.state["status"], next_phase)
    if edge not in allowed_edges:
        return LoopResult(
            False,
            (
                f"assurance transition {edge[0]} -> {edge[1]} "
                "requires a guarded command",
            ),
            loaded.state,
        )
    return transition_managed_run(ASSURANCE_RUN, task, next_phase)


def submit_assessment(
    task_dir: Path,
    project_root: Path,
    report: Any,
) -> LoopResult:
    task = Path(task_dir)
    loaded = load_run(task)
    if not loaded.allowed:
        return loaded
    if loaded.state["status"] != "assess":
        return LoopResult(
            False,
            ("assurance run must be in assess phase",),
            loaded.state,
        )
    validation = validate_report(report)
    if not validation.allowed:
        return LoopResult(False, validation.errors, loaded.state)
    value = validation.value
    if value["request_sha256"] != loaded.state["request_sha256"]:
        return LoopResult(
            False,
            ("assurance report request_sha256 does not match the active run",),
            loaded.state,
        )
    failed_categories = [
        category
        for category, assessment in value["assessments"].items()
        if assessment["status"] == "fail"
    ]
    clean = not failed_categories
    receipt = validate_scenario_receipt(
        task,
        Path(project_root),
        value["scenario_result_sha256"],
        require_completion=clean,
    )
    if not receipt.allowed:
        return LoopResult(False, receipt.errors, loaded.state)
    path = task / "iterations" / f"{loaded.state['iteration']:03d}" / "assurance.json"
    try:
        atomic_write(path, canonical_json(value))
    except OSError as exc:
        return LoopResult(
            False,
            (f"cannot persist assurance report: {exc}",),
            loaded.state,
        )
    if clean:
        target = "completed"
    else:
        request_value, request_errors = _read_json_artifact(
            task / REQUEST_FILENAME,
            "assurance request",
        )
        if request_errors:
            return LoopResult(False, request_errors, loaded.state)
        request_validation = validate_request(request_value)
        if not request_validation.allowed:
            return LoopResult(
                False,
                request_validation.errors,
                loaded.state,
            )
        target = (
            "address"
            if "modify-worktree" in request_validation.value["permissions"]
            else "changes-requested"
        )
    return transition_managed_run(ASSURANCE_RUN, task, target)


def verify_run(task_dir: Path, project_root: Path) -> LoopResult:
    task = Path(task_dir)
    loaded = load_run(task)
    if not loaded.allowed:
        return loaded
    if loaded.state["status"] != "verify":
        return LoopResult(
            False,
            ("assurance run must be in verify phase",),
            loaded.state,
        )
    completion = validate_completion(task, Path(project_root))
    if not completion.allowed:
        return LoopResult(False, completion.errors, loaded.state)
    return transition_managed_run(ASSURANCE_RUN, task, "assess")


def terminate_run(task_dir: Path, status: str) -> LoopResult:
    return terminate_managed_run(ASSURANCE_RUN, Path(task_dir), status)


def build_subloop_result(
    invocation: Mapping[str, Any],
    raw_assessments: Any,
    *,
    source_snapshot_after_sha256: str,
) -> Mapping[str, Any]:
    if (
        not isinstance(invocation, dict)
        or invocation.get("pack") != PACK_PROFILE.name
        or invocation.get("mode") != "subloop"
    ):
        raise ValueError("Assurance Subloop requires a bound assurance invocation")
    errors: list[str] = []
    normalized, findings = _validate_assessments(raw_assessments, errors)
    if errors:
        raise ValueError("; ".join(dict.fromkeys(errors)))
    failed = [
        category
        for category, assessment in normalized.items()
        if assessment["status"] == "fail"
    ]
    finding_refs = [f"assurance-report.json#{finding['id']}" for finding in findings]
    status = "changes-requested" if failed else "completed"
    summary = (
        "Assurance found changes required in: " + ", ".join(failed)
        if failed
        else "All assurance assessment categories passed."
    )
    return {
        "schema_version": 1,
        "invocation_id": invocation["invocation_id"],
        "invocation_sha256": content_sha256(canonical_json(invocation)),
        "pack": PACK_PROFILE.name,
        "status": status,
        "summary": summary,
        "finding_refs": finding_refs,
        "changed_paths": [],
        "evidence_refs": ["assurance-report.json"],
        "budget_usage": {"iterations_used": 1},
        "completion_receipt": None,
        "decision": None,
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
    child_dir: Path,
    project_root: Path,
    raw_assessments: Any,
) -> LoopResult:
    child = Path(child_dir)
    try:
        root = Path(project_root).resolve(strict=True)
        resolved = child.resolve(strict=True)
        relative = resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        return LoopResult(
            False,
            (f"cannot resolve Assurance Subloop: {exc}",),
            {},
        )
    if (
        len(relative.parts) != 4
        or relative.parts[0] != "_workspace"
        or relative.parts[2] != "subloops"
        or child.is_symlink()
        or child.parent.is_symlink()
    ):
        return LoopResult(
            False,
            ("Assurance Subloop must be nested under one direct root task",),
            {},
        )
    invocation, invocation_errors = _read_json_artifact(
        resolved / "invocation.json",
        "Assurance Subloop invocation",
    )
    if invocation_errors:
        return LoopResult(False, invocation_errors, {})
    fingerprint, fingerprint_errors = source_fingerprint(root)
    if fingerprint is None:
        return LoopResult(False, fingerprint_errors, {})
    errors: list[str] = []
    normalized, _ = _validate_assessments(raw_assessments, errors)
    if errors:
        return LoopResult(False, tuple(dict.fromkeys(errors)), {})
    try:
        result = build_subloop_result(
            invocation,
            normalized,
            source_snapshot_after_sha256=fingerprint,
        )
    except ValueError as exc:
        return LoopResult(False, (str(exc),), {})
    report = {
        "schema_version": 1,
        "assessments": normalized,
    }
    for path, content, label in (
        (
            resolved / "assurance-report.json",
            canonical_json(report),
            "Assurance Subloop report",
        ),
        (
            resolved / "result-input.json",
            canonical_json(result),
            "Assurance Subloop result",
        ),
    ):
        persist_errors = _persist_once_or_match(path, content, label)
        if persist_errors:
            return LoopResult(False, persist_errors, {})
    return LoopResult(True, (), result)


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
    transition_parser.add_argument("next_phase", choices=("assess", "verify"))
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
        choices=("needs-decision", "blocked"),
    )
    terminate.add_argument("--project-root", required=True, type=Path)
    terminate.add_argument("--json", action="store_true")

    status = subparsers.add_parser("status")
    status.add_argument("task", type=Path, nargs="?")
    status.add_argument("--project-root", required=True, type=Path)
    status.add_argument("--json", action="store_true")

    subloop = subparsers.add_parser("prepare-subloop-result")
    subloop.add_argument("task", type=Path)
    subloop.add_argument("--assessment", required=True, type=Path)
    subloop.add_argument("--project-root", required=True, type=Path)
    subloop.add_argument("--json", action="store_true")

    args = parser.parse_args()
    if args.operation == "prepare-subloop-result":
        value, errors = _read_json_artifact(
            args.assessment,
            "Assurance Subloop assessment",
        )
        result = (
            LoopResult(False, errors, {})
            if errors
            else prepare_subloop_result(
                args.task,
                args.project_root,
                value,
            )
        )
        _print_payload(result, args.json, args.task)
        return 0 if result.allowed else 1
    if args.operation == "status" and args.task is None:
        task, errors = resolve_managed_run(ASSURANCE_RUN, args.project_root)
        result = LoopResult(False, errors, {}) if task is None else load_run(task)
        _print_payload(result, args.json, task)
        return 0 if result.allowed else 1

    task, error = _direct_task(args.task, args.project_root)
    if error is not None:
        _print_payload(error, args.json)
        return 1
    assert task is not None

    if args.operation == "start":
        value, errors = _read_json_artifact(
            args.request,
            "assurance request",
        )
        result = (
            LoopResult(False, errors, {})
            if errors
            else start_run(task, value, args.max_iterations)
        )
    elif args.operation == "transition":
        result = transition_run(task, args.next_phase)
    elif args.operation == "submit":
        value, errors = _read_json_artifact(
            args.report,
            "assurance report",
        )
        result = (
            LoopResult(False, errors, {})
            if errors
            else submit_assessment(task, args.project_root, value)
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
