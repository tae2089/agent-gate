#!/usr/bin/env python3
"""Deterministic contracts for the Agent Loop research adoption pack."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

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
from scenario_gate import validate_scenario_receipt

REQUEST_SCHEMA_VERSION = 1
DECISION_SCHEMA_VERSION = 1
REQUEST_FIELDS = frozenset(
    {
        "schema_version",
        "source",
        "source_ref",
        "request",
        "question",
        "constraints",
        "evidence",
    }
)
DECISION_FIELDS = frozenset(
    {
        "schema_version",
        "request_sha256",
        "prototype_result_sha256",
        "scenario_result_sha256",
        "verdict",
        "sources",
        "checks",
        "findings",
        "prototype_disposition",
    }
)
SOURCE_FIELDS = frozenset({"url", "title", "claims"})
CHECK_FIELDS = frozenset({"passed", "evidence"})
CHECK_NAMES = frozenset(
    {
        "evidence_quality",
        "repository_fit",
        "prototype_verified",
        "cost_acceptable",
    }
)
VERDICTS = frozenset({"adopt", "reject", "iterate"})
PROTOTYPE_DISPOSITIONS = frozenset({"adopted", "removed", "not-created", "retained"})
PHASE_TRANSITIONS = {
    "frame": frozenset({"research"}),
    "research": frozenset({"prototype"}),
    "prototype": frozenset({"evaluate"}),
    "evaluate": frozenset({"research", "adopted", "rejected"}),
}
TERMINAL_STATUSES = frozenset(
    {
        "adopted",
        "rejected",
        "needs-clarification",
        "blocked",
        "budget-exhausted",
    }
)
RESEARCH_ADOPTION_DEFINITION = LoopDefinition(
    name="research adoption",
    transitions=PHASE_TRANSITIONS,
    terminal_statuses=TERMINAL_STATUSES,
    iteration_transitions=frozenset({("evaluate", "research")}),
    budget_terminal="budget-exhausted",
)
ACTIVE_RESEARCH_ADOPTION_FILENAME = ".active-research-adoption"
REQUEST_FILENAME = "research-request.json"
STATE_FILENAME = "research-adoption-state.json"
PROTOTYPE_RESULT_FILENAME = "prototype-result.json"
RESEARCH_ADOPTION_RUN = ManagedLoopDefinition(
    loop=RESEARCH_ADOPTION_DEFINITION,
    input_filename=REQUEST_FILENAME,
    state_filename=STATE_FILENAME,
    active_pointer_filename=ACTIVE_RESEARCH_ADOPTION_FILENAME,
    input_hash_field="request_sha256",
    initial_status="frame",
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
        return ArtifactValidation(
            False,
            ("research request must be an object",),
            {},
        )
    errors: list[str] = []
    _exact_fields(value, REQUEST_FIELDS, "research request", errors)
    if value.get("schema_version") != REQUEST_SCHEMA_VERSION:
        errors.append(
            f"research request schema_version must be {REQUEST_SCHEMA_VERSION}"
        )
    _non_empty_string(value.get("source"), "research request source", errors)
    _non_empty_string(
        value.get("source_ref"),
        "research request source_ref",
        errors,
    )
    _non_empty_string(value.get("request"), "user request", errors)
    _non_empty_string(value.get("question"), "adoption question", errors)
    _string_list(
        value.get("constraints"),
        "research constraints",
        errors,
    )
    _string_list(value.get("evidence"), "research evidence", errors)
    if value.get("source") != "manual":
        errors.append("research request source must be manual")
    normalized = {field: value[field] for field in REQUEST_FIELDS if field in value}
    return ArtifactValidation(
        not errors,
        tuple(dict.fromkeys(errors)),
        normalized,
    )


def _validate_source(
    value: Any,
    index: int,
    errors: list[str],
) -> Mapping[str, Any]:
    label = f"research source[{index}]"
    if not isinstance(value, dict):
        errors.append(f"{label} must be an object")
        return {}
    _exact_fields(value, SOURCE_FIELDS, label, errors)
    url = value.get("url")
    if not isinstance(url, str) or url != url.strip():
        errors.append(f"{label}.url must be an absolute HTTP(S) URL")
    else:
        parsed = urlparse(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            errors.append(
                f"{label}.url must be an absolute credential-free "
                "HTTP(S) URL without a fragment"
            )
    _non_empty_string(value.get("title"), f"{label}.title", errors)
    _string_list(value.get("claims"), f"{label}.claims", errors)
    return {field: value[field] for field in SOURCE_FIELDS if field in value}


def _validate_checks(
    value: Any,
    errors: list[str],
) -> tuple[Mapping[str, Any], frozenset[str]]:
    if not isinstance(value, dict):
        errors.append("research decision checks must be an object")
        return {}, frozenset()
    _exact_fields(value, CHECK_NAMES, "research decision checks", errors)
    normalized: dict[str, Any] = {}
    failed: set[str] = set()
    for name in sorted(CHECK_NAMES):
        raw = value.get(name)
        label = f"research decision check {name}"
        if not isinstance(raw, dict):
            errors.append(f"{label} must be an object")
            continue
        _exact_fields(raw, CHECK_FIELDS, label, errors)
        passed = raw.get("passed")
        if not isinstance(passed, bool):
            errors.append(f"{label}.passed must be boolean")
        elif not passed:
            failed.add(name)
        evidence = _string_list(
            raw.get("evidence"),
            f"{label}.evidence",
            errors,
        )
        normalized[name] = {"passed": passed, "evidence": evidence}
    return normalized, frozenset(failed)


def validate_decision(value: Any) -> ArtifactValidation:
    if not isinstance(value, dict):
        return ArtifactValidation(
            False,
            ("research decision must be an object",),
            {},
        )
    errors: list[str] = []
    _exact_fields(value, DECISION_FIELDS, "research decision", errors)
    if value.get("schema_version") != DECISION_SCHEMA_VERSION:
        errors.append(
            f"research decision schema_version must be {DECISION_SCHEMA_VERSION}"
        )
    if not _valid_sha256(value.get("request_sha256")):
        errors.append("research decision request_sha256 must be a lowercase SHA-256")
    if not _valid_sha256(value.get("prototype_result_sha256")):
        errors.append(
            "research decision prototype_result_sha256 must be a lowercase SHA-256"
        )
    if not _valid_sha256(value.get("scenario_result_sha256")):
        errors.append(
            "research decision scenario_result_sha256 must be a lowercase SHA-256"
        )
    verdict = value.get("verdict")
    if verdict not in VERDICTS:
        errors.append(f"unsupported research decision verdict: {verdict}")

    raw_sources = value.get("sources")
    sources: list[Mapping[str, Any]] = []
    if not isinstance(raw_sources, list) or not raw_sources:
        errors.append("research decision sources must be a non-empty list")
    else:
        sources = [
            _validate_source(item, index, errors)
            for index, item in enumerate(raw_sources)
        ]
    decision_checks, failed_checks = _validate_checks(
        value.get("checks"),
        errors,
    )
    findings = _string_list(
        value.get("findings"),
        "research decision findings",
        errors,
        allow_empty=True,
    )
    disposition = value.get("prototype_disposition")
    if disposition not in PROTOTYPE_DISPOSITIONS:
        errors.append("research decision prototype_disposition is unsupported")

    if verdict == "adopt":
        if failed_checks:
            errors.append(
                "adopt decision has failed checks: " + ", ".join(sorted(failed_checks))
            )
        if findings:
            errors.append("adopt decision must not have findings")
        if disposition != "adopted":
            errors.append("adopt decision requires prototype_disposition adopted")
    elif verdict == "reject":
        if not failed_checks:
            errors.append("reject decision requires at least one failed check")
        if not findings:
            errors.append("reject decision requires an evidence-backed finding")
        if disposition not in {"removed", "not-created"}:
            errors.append("reject decision requires a removed or absent prototype")
    elif verdict == "iterate":
        if not failed_checks:
            errors.append("iterate decision requires at least one failed check")
        if not findings:
            errors.append("iterate decision requires an actionable finding")
        if disposition == "adopted":
            errors.append("iterate decision cannot adopt the prototype")

    normalized = {field: value[field] for field in DECISION_FIELDS if field in value}
    if "sources" in normalized:
        normalized["sources"] = sources
    if "checks" in normalized:
        normalized["checks"] = decision_checks
    if "findings" in normalized:
        normalized["findings"] = findings
    return ArtifactValidation(
        not errors,
        tuple(dict.fromkeys(errors)),
        normalized,
    )


def load_run(task_dir: Path) -> LoopResult:
    return load_managed_run(RESEARCH_ADOPTION_RUN, Path(task_dir))


def start_run(
    task_dir: Path,
    request: Any,
    max_iterations: int = 3,
) -> LoopResult:
    validation = validate_request(request)
    if not validation.allowed:
        return LoopResult(False, validation.errors, {})
    return start_managed_run(
        RESEARCH_ADOPTION_RUN,
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
        ("frame", "research"),
        ("research", "prototype"),
        ("prototype", "evaluate"),
    }
    edge = (loaded.state["status"], next_phase)
    if edge not in allowed_edges:
        return LoopResult(
            False,
            (
                f"research adoption transition {edge[0]} -> {edge[1]} "
                "requires a guarded command",
            ),
            loaded.state,
        )
    return transition_managed_run(RESEARCH_ADOPTION_RUN, task, next_phase)


def capture_prototype_result(
    task_dir: Path,
    project_root: Path,
) -> LoopResult:
    task = Path(task_dir)
    loaded = load_run(task)
    if not loaded.allowed:
        return loaded
    if loaded.state["status"] != "evaluate":
        return LoopResult(
            False,
            ("research adoption run must be in evaluate phase",),
            loaded.state,
        )
    source = task / "scenario-result.json"
    archive = (
        task
        / "iterations"
        / f"{loaded.state['iteration']:03d}"
        / PROTOTYPE_RESULT_FILENAME
    )
    if archive.exists() or archive.is_symlink():
        return LoopResult(
            False,
            ("captured prototype result already exists for this iteration",),
            loaded.state,
        )
    try:
        content = source.read_bytes()
    except OSError as exc:
        return LoopResult(
            False,
            (f"cannot capture prototype result: {exc}",),
            loaded.state,
        )
    receipt = validate_scenario_receipt(
        task,
        Path(project_root),
        content_sha256(content),
        require_completion=False,
    )
    if not receipt.allowed:
        return LoopResult(False, receipt.errors, loaded.state)
    try:
        atomic_write(archive, content)
    except OSError as exc:
        return LoopResult(
            False,
            (f"cannot capture prototype result: {exc}",),
            loaded.state,
        )
    return LoopResult(True, (), loaded.state)


def submit_decision(
    task_dir: Path,
    project_root: Path,
    decision: Any,
) -> LoopResult:
    task = Path(task_dir)
    loaded = load_run(task)
    if not loaded.allowed:
        return loaded
    if loaded.state["status"] != "evaluate":
        return LoopResult(
            False,
            ("research adoption run must be in evaluate phase",),
            loaded.state,
        )
    validation = validate_decision(decision)
    if not validation.allowed:
        return LoopResult(False, validation.errors, loaded.state)
    value = validation.value
    if value["request_sha256"] != loaded.state["request_sha256"]:
        return LoopResult(
            False,
            ("research decision request_sha256 does not match the active run",),
            loaded.state,
        )
    prototype_path = (
        task
        / "iterations"
        / f"{loaded.state['iteration']:03d}"
        / PROTOTYPE_RESULT_FILENAME
    )
    if prototype_path.is_symlink():
        return LoopResult(
            False,
            ("captured prototype result must not be a symlink",),
            loaded.state,
        )
    try:
        prototype_content = prototype_path.read_bytes()
    except OSError as exc:
        return LoopResult(
            False,
            (f"cannot read captured prototype result: {exc}",),
            loaded.state,
        )
    if content_sha256(prototype_content) != value["prototype_result_sha256"]:
        return LoopResult(
            False,
            ("prototype_result_sha256 is stale",),
            loaded.state,
        )
    terminal = value["verdict"] in {"adopt", "reject"}
    receipt = validate_scenario_receipt(
        task,
        Path(project_root),
        value["scenario_result_sha256"],
        require_completion=terminal,
    )
    if not receipt.allowed:
        return LoopResult(False, receipt.errors, loaded.state)
    path = task / "iterations" / f"{loaded.state['iteration']:03d}" / "decision.json"
    try:
        atomic_write(path, canonical_json(value))
    except OSError as exc:
        return LoopResult(
            False,
            (f"cannot persist research decision: {exc}",),
            loaded.state,
        )
    target = {
        "adopt": "adopted",
        "reject": "rejected",
        "iterate": "research",
    }[value["verdict"]]
    return transition_managed_run(RESEARCH_ADOPTION_RUN, task, target)


def terminate_run(task_dir: Path, status: str) -> LoopResult:
    return terminate_managed_run(
        RESEARCH_ADOPTION_RUN,
        Path(task_dir),
        status,
    )


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
    transition_parser.add_argument(
        "next_phase",
        choices=("research", "prototype", "evaluate"),
    )
    transition_parser.add_argument("--project-root", required=True, type=Path)
    transition_parser.add_argument("--json", action="store_true")

    submit = subparsers.add_parser("submit")
    submit.add_argument("task", type=Path)
    submit.add_argument("--decision", required=True, type=Path)
    submit.add_argument("--project-root", required=True, type=Path)
    submit.add_argument("--json", action="store_true")

    capture = subparsers.add_parser("capture")
    capture.add_argument("task", type=Path)
    capture.add_argument("--project-root", required=True, type=Path)
    capture.add_argument("--json", action="store_true")

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
        task, errors = resolve_managed_run(
            RESEARCH_ADOPTION_RUN,
            args.project_root,
        )
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
            "research request",
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
            args.decision,
            "research decision",
        )
        result = (
            LoopResult(False, errors, {})
            if errors
            else submit_decision(task, args.project_root, value)
        )
    elif args.operation == "capture":
        result = capture_prototype_result(task, args.project_root)
    elif args.operation == "terminate":
        result = terminate_run(task, args.status)
    else:
        result = load_run(task)

    _print_payload(result, args.json, task)
    return 0 if result.allowed else 1


if __name__ == "__main__":
    sys.exit(main())
