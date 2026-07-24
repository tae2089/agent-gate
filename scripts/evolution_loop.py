#!/usr/bin/env python3
"""Deterministic contracts for the Agent Loop evolution pack."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

from loop_engine import (
    LoopDefinition,
    LoopResult as RunResult,
    atomic_write as _atomic_write,
    canonical_json as _canonical_json,
    content_sha256,
    direct_workspace_task as _direct_task,
    resolve_active_run,
    transition as transition_state,
)
from scenario_gate import validate_completion

CANDIDATE_SCHEMA_VERSION = 1
STATE_SCHEMA_VERSION = 1
CANDIDATE_REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "source",
        "source_ref",
        "title",
        "problem",
        "evidence",
        "labels",
        "request",
    }
)
CANDIDATE_ALLOWED_FIELDS = CANDIDATE_REQUIRED_FIELDS
WORK_KINDS = frozenset(
    {"feature", "bug", "contract-violation", "technical-debt"}
)
STATE_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "iteration",
        "max_iterations",
        "candidate_sha256",
        "pr_url",
    }
)
PHASE_TRANSITIONS = {
    "interview": frozenset({"seed"}),
    "seed": frozenset({"execute"}),
    "execute": frozenset({"interview", "evaluate"}),
    "evaluate": frozenset({"execute", "interview", "pr-ready"}),
}
TERMINAL_STATUSES = frozenset(
    {
        "no-action",
        "needs-clarification",
        "blocked",
        "invalid-candidate",
        "budget-exhausted",
        "pr-ready",
        "pr-opened",
    }
)
EVOLUTION_DEFINITION = LoopDefinition(
    name="evolution",
    transitions=PHASE_TRANSITIONS,
    terminal_statuses=TERMINAL_STATUSES,
    iteration_transitions=frozenset(
        {
            ("execute", "interview"),
            ("evaluate", "interview"),
        }
    ),
    budget_terminal="budget-exhausted",
)
EVALUATION_SCHEMA_VERSION = 1
EVALUATION_FIELDS = frozenset(
    {
        "schema_version",
        "verdict",
        "candidate_sha256",
        "scenario_result_sha256",
        "checks",
        "findings",
    }
)
EVALUATION_CHECK_NAMES = frozenset(
    {
        "planned_scope_only",
        "no_speculative_abstraction",
        "compatibility_has_consumer",
        "simpler_alternative_considered",
    }
)
EVALUATION_VERDICTS = frozenset(
    {"pr-ready", "iterate", "needs-clarification", "blocked"}
)
EVALUATION_CHECK_FIELDS = frozenset({"passed", "evidence"})
ACTIVE_EVOLUTION_FILENAME = ".active-evolution"


@dataclass(frozen=True)
class CandidateValidation:
    allowed: bool
    errors: tuple[str, ...]
    candidate: Mapping[str, Any]


def _non_empty_string(
    value: Any, label: str, errors: list[str]
) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{label} must be a non-empty string")
        return None
    return value


def _string_list(value: Any, label: str, errors: list[str]) -> list[str]:
    if not isinstance(value, list):
        errors.append(f"{label} must be a list")
        return []
    if any(not isinstance(item, str) or not item.strip() for item in value):
        errors.append(f"{label} contains an invalid string")
        return []
    return list(value)


def validate_candidate(value: Any) -> CandidateValidation:
    errors: list[str] = []
    if not isinstance(value, dict):
        return CandidateValidation(False, ("candidate must be an object",), {})

    unknown = sorted(value.keys() - CANDIDATE_ALLOWED_FIELDS)
    missing = sorted(CANDIDATE_REQUIRED_FIELDS - value.keys())
    if unknown:
        errors.append(f"candidate has unknown fields: {', '.join(unknown)}")
    if missing:
        errors.append(f"candidate is missing fields: {', '.join(missing)}")

    schema_version = value.get("schema_version")
    if schema_version != CANDIDATE_SCHEMA_VERSION:
        errors.append(
            f"candidate schema_version must be {CANDIDATE_SCHEMA_VERSION}"
        )
    kind = _non_empty_string(value.get("kind"), "candidate kind", errors)
    source = _non_empty_string(value.get("source"), "candidate source", errors)
    _non_empty_string(value.get("source_ref"), "candidate source_ref", errors)
    _non_empty_string(value.get("title"), "candidate title", errors)
    _non_empty_string(value.get("problem"), "candidate problem", errors)
    evidence = _string_list(value.get("evidence"), "candidate evidence", errors)
    _string_list(value.get("labels"), "candidate labels", errors)
    _non_empty_string(value.get("request"), "user request", errors)

    if kind is not None and kind not in WORK_KINDS:
        errors.append(f"unsupported candidate kind: {kind}")
    if source is not None and source != "manual":
        errors.append("candidate source must be manual")
    if not evidence:
        errors.append("candidate evidence must be non-empty")

    normalized = {
        field: value[field]
        for field in CANDIDATE_ALLOWED_FIELDS
        if field in value
    }
    return CandidateValidation(not errors, tuple(dict.fromkeys(errors)), normalized)


def _write_state(task_dir: Path, state: Mapping[str, Any]) -> None:
    _atomic_write(task_dir / "evolution-state.json", _canonical_json(state))


def _load_state(task_dir: Path) -> RunResult:
    path = task_dir / "evolution-state.json"
    if path.is_symlink():
        return RunResult(False, ("evolution state must not be a symlink",), {})
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return RunResult(False, (f"cannot read evolution state: {exc}",), {})
    if not isinstance(value, dict):
        return RunResult(False, ("evolution state must be an object",), {})
    unknown = sorted(value.keys() - STATE_FIELDS)
    missing = sorted(STATE_FIELDS - value.keys())
    errors = []
    if unknown:
        errors.append(f"evolution state has unknown fields: {', '.join(unknown)}")
    if missing:
        errors.append(f"evolution state is missing fields: {', '.join(missing)}")
    if value.get("schema_version") != STATE_SCHEMA_VERSION:
        errors.append(f"evolution state schema_version must be {STATE_SCHEMA_VERSION}")
    status = value.get("status")
    if status not in set(PHASE_TRANSITIONS) | TERMINAL_STATUSES:
        errors.append(f"unsupported evolution status: {status}")
    iteration = value.get("iteration")
    max_iterations = value.get("max_iterations")
    if (
        isinstance(iteration, bool)
        or not isinstance(iteration, int)
        or iteration < 1
    ):
        errors.append("evolution state iteration must be a positive integer")
    if (
        isinstance(max_iterations, bool)
        or not isinstance(max_iterations, int)
        or not 1 <= max_iterations <= 10
    ):
        errors.append("evolution state max_iterations must be from 1 through 10")
    if (
        isinstance(iteration, int)
        and not isinstance(iteration, bool)
        and isinstance(max_iterations, int)
        and not isinstance(max_iterations, bool)
        and iteration > max_iterations
    ):
        errors.append("evolution state iteration exceeds max_iterations")
    candidate_hash = value.get("candidate_sha256")
    if (
        not isinstance(candidate_hash, str)
        or len(candidate_hash) != 64
        or any(character not in "0123456789abcdef" for character in candidate_hash)
    ):
        errors.append("evolution state candidate_sha256 must be a lowercase SHA-256")
    pr_url = value.get("pr_url")
    if status == "pr-opened":
        if not isinstance(pr_url, str) or not pr_url:
            errors.append("pr-opened evolution state requires pr_url")
    elif pr_url is not None:
        errors.append("only pr-opened evolution state may contain pr_url")
    if errors:
        return RunResult(False, tuple(errors), value)
    return RunResult(True, (), value)


def _active_evolution_task(
    project_root: Path,
) -> tuple[Optional[Path], tuple[str, ...]]:
    return resolve_active_run(
        Path(project_root),
        ACTIVE_EVOLUTION_FILENAME,
        "evolution",
    )


def start_run(
    task_dir: Path,
    candidate: Any,
    max_iterations: int = 3,
) -> RunResult:
    validation = validate_candidate(candidate)
    if not validation.allowed:
        return RunResult(False, validation.errors, {})
    normalized_candidate = validation.candidate
    if (
        isinstance(max_iterations, bool)
        or not isinstance(max_iterations, int)
        or not 1 <= max_iterations <= 10
    ):
        return RunResult(
            False, ("max_iterations must be an integer from 1 through 10",), {}
        )
    task = Path(task_dir)
    if not task.is_dir() or task.is_symlink():
        return RunResult(False, ("task directory must be a real directory",), {})
    if task.parent.name != "_workspace":
        return RunResult(False, ("task must be a direct _workspace task",), {})
    state_path = task / "evolution-state.json"
    if state_path.exists() or state_path.is_symlink():
        return RunResult(False, ("evolution state already exists",), {})
    root = task.parent.parent.resolve(strict=True)
    pointer = task.parent / ACTIVE_EVOLUTION_FILENAME
    if pointer.exists() or pointer.is_symlink():
        active_task, active_errors = _active_evolution_task(root)
        if active_task is None:
            return RunResult(False, active_errors, {})
        active = _load_state(active_task)
        if not active.allowed:
            return RunResult(False, active.errors, active.state)
        if active.state["status"] in set(PHASE_TRANSITIONS) | {"pr-ready"}:
            return RunResult(False, ("another evolution run is active",), active.state)

    candidate_content = _canonical_json(normalized_candidate)
    state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "status": "seed",
        "iteration": 1,
        "max_iterations": max_iterations,
        "candidate_sha256": content_sha256(candidate_content),
        "pr_url": None,
    }
    try:
        _atomic_write(task / "candidate.json", candidate_content)
        _write_state(task, state)
        relative = task.resolve(strict=True).relative_to(root)
        _atomic_write(pointer, (relative.as_posix() + "\n").encode("utf-8"))
    except OSError as exc:
        return RunResult(False, (f"cannot start evolution run: {exc}",), {})
    return RunResult(True, (), state)


def transition_run(task_dir: Path, next_phase: str) -> RunResult:
    loaded = _load_state(Path(task_dir))
    if not loaded.allowed:
        return loaded
    decision = transition_state(EVOLUTION_DEFINITION, loaded.state, next_phase)
    if not decision.allowed:
        return decision
    state = dict(decision.state)
    try:
        _write_state(Path(task_dir), state)
    except OSError as exc:
        return RunResult(False, (f"cannot persist evolution state: {exc}",), loaded.state)
    return RunResult(True, (), state)


def terminate_run(task_dir: Path, status: str) -> RunResult:
    loaded = _load_state(Path(task_dir))
    if not loaded.allowed:
        return loaded
    state = dict(loaded.state)
    if state["status"] not in PHASE_TRANSITIONS:
        return RunResult(False, ("evolution run is already terminal",), state)
    if status not in TERMINAL_STATUSES - {"pr-ready", "pr-opened"}:
        return RunResult(False, (f"unsupported terminal status: {status}",), state)
    state["status"] = status
    try:
        _write_state(Path(task_dir), state)
    except OSError as exc:
        return RunResult(False, (f"cannot persist evolution state: {exc}",), loaded.state)
    return RunResult(True, (), state)


def _validate_evaluation(
    value: Any,
    state: Mapping[str, Any],
    scenario_result_sha256: str,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return {}, ("evaluation must be an object",)
    unknown = sorted(value.keys() - EVALUATION_FIELDS)
    missing = sorted(EVALUATION_FIELDS - value.keys())
    if unknown:
        errors.append(f"evaluation has unknown fields: {', '.join(unknown)}")
    if missing:
        errors.append(f"evaluation is missing fields: {', '.join(missing)}")
    if value.get("schema_version") != EVALUATION_SCHEMA_VERSION:
        errors.append(
            f"evaluation schema_version must be {EVALUATION_SCHEMA_VERSION}"
        )
    verdict = value.get("verdict")
    if verdict not in EVALUATION_VERDICTS:
        errors.append(f"unsupported evaluation verdict: {verdict}")
    if value.get("candidate_sha256") != state.get("candidate_sha256"):
        errors.append("evaluation candidate_sha256 does not match the active run")
    if value.get("scenario_result_sha256") != scenario_result_sha256:
        errors.append("evaluation scenario_result_sha256 is stale")

    checks = value.get("checks")
    failed_checks: list[str] = []
    if not isinstance(checks, dict):
        errors.append("evaluation checks must be an object")
    else:
        unknown_checks = sorted(checks.keys() - EVALUATION_CHECK_NAMES)
        missing_checks = sorted(EVALUATION_CHECK_NAMES - checks.keys())
        if unknown_checks:
            errors.append(
                f"evaluation has unknown checks: {', '.join(unknown_checks)}"
            )
        if missing_checks:
            errors.append(
                f"evaluation is missing checks: {', '.join(missing_checks)}"
            )
        for name in sorted(EVALUATION_CHECK_NAMES & checks.keys()):
            check = checks[name]
            if not isinstance(check, dict):
                errors.append(f"evaluation check {name} must be an object")
                continue
            if set(check) != EVALUATION_CHECK_FIELDS:
                errors.append(
                    f"evaluation check {name} must contain passed and evidence"
                )
                continue
            if not isinstance(check["passed"], bool):
                errors.append(f"evaluation check {name} passed must be boolean")
            elif not check["passed"]:
                failed_checks.append(name)
            evidence = check["evidence"]
            if (
                not isinstance(evidence, list)
                or not evidence
                or any(
                    not isinstance(item, str) or not item.strip()
                    for item in evidence
                )
            ):
                errors.append(
                    f"evaluation check {name} evidence must be a non-empty string list"
                )

    findings = value.get("findings")
    if not isinstance(findings, list) or any(
        not isinstance(item, str) or not item.strip() for item in findings
    ):
        errors.append("evaluation findings must be a string list")
        findings = []

    if verdict == "pr-ready":
        if failed_checks:
            errors.append(
                "pr-ready evaluation has failed checks: "
                + ", ".join(sorted(failed_checks))
            )
        if findings:
            errors.append("pr-ready evaluation must not have remaining findings")
    elif verdict == "iterate":
        if not failed_checks:
            errors.append("iterate evaluation requires at least one failed check")
        if not findings:
            errors.append("iterate evaluation requires an actionable finding")
    elif verdict in {"needs-clarification", "blocked"} and not findings:
        errors.append(f"{verdict} evaluation requires a finding")

    normalized = {field: value[field] for field in EVALUATION_FIELDS if field in value}
    return normalized, tuple(dict.fromkeys(errors))


def evaluate_run(
    task_dir: Path, project_root: Path, evaluation: Any
) -> RunResult:
    task = Path(task_dir)
    loaded = _load_state(task)
    if not loaded.allowed:
        return loaded
    if loaded.state["status"] != "evaluate":
        return RunResult(
            False,
            ("evolution run must be in evaluate phase",),
            loaded.state,
        )

    completion = validate_completion(task, Path(project_root))
    if not completion.allowed:
        return RunResult(
            False,
            ("scenario completion is not current and complete",)
            + completion.errors,
            loaded.state,
        )
    scenario_result_path = task / "scenario-result.json"
    if scenario_result_path.is_symlink():
        return RunResult(
            False, ("scenario result must not be a symlink",), loaded.state
        )
    try:
        scenario_result_content = scenario_result_path.read_bytes()
    except OSError as exc:
        return RunResult(
            False, (f"cannot read scenario result: {exc}",), loaded.state
        )
    result_hash = content_sha256(scenario_result_content)
    normalized, errors = _validate_evaluation(
        evaluation, loaded.state, result_hash
    )
    if errors:
        return RunResult(False, errors, loaded.state)

    iteration = loaded.state["iteration"]
    evaluation_path = (
        task / "iterations" / f"{iteration:03d}" / "evaluation.json"
    )
    try:
        _atomic_write(evaluation_path, _canonical_json(normalized))
    except OSError as exc:
        return RunResult(
            False, (f"cannot persist evaluation: {exc}",), loaded.state
        )

    verdict = normalized["verdict"]
    if verdict == "pr-ready":
        return transition_run(task, "pr-ready")
    if verdict == "iterate":
        return transition_run(task, "interview")
    return terminate_run(task, verdict)


def _recorded_pr_url(value: Any) -> Optional[str]:
    if not isinstance(value, str) or value != value.strip():
        return None
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path
        or parsed.path == "/"
        or parsed.query
        or parsed.fragment
    ):
        return None
    return value


def record_pr(
    task_dir: Path, project_root: Path, pr_url: Any
) -> RunResult:
    task = Path(task_dir)
    loaded = _load_state(task)
    if not loaded.allowed:
        return loaded

    recorded_url = _recorded_pr_url(pr_url)
    if recorded_url is None:
        return RunResult(
            False, ("pull request receipt must be an absolute HTTPS URL",), loaded.state
        )
    if loaded.state["status"] == "pr-opened":
        if loaded.state["pr_url"] == recorded_url:
            return RunResult(True, (), loaded.state)
        return RunResult(
            False,
            ("a different pull request receipt is already recorded",),
            loaded.state,
        )
    if loaded.state["status"] != "pr-ready":
        return RunResult(
            False, ("evolution run is not pr-ready",), loaded.state
        )

    completion = validate_completion(task, Path(project_root))
    if not completion.allowed:
        return RunResult(
            False,
            ("scenario completion is not current and complete",)
            + completion.errors,
            loaded.state,
        )
    state = dict(loaded.state)
    state["status"] = "pr-opened"
    state["pr_url"] = recorded_url
    try:
        _write_state(task, state)
    except OSError as exc:
        return RunResult(
            False,
            (f"cannot persist pull request receipt: {exc}",),
            loaded.state,
        )
    return RunResult(True, (), state)


def _read_json_artifact(path: Path, label: str) -> tuple[Any, tuple[str, ...]]:
    if path.is_symlink():
        return None, (f"{label} must not be a symlink",)
    try:
        return json.loads(path.read_text(encoding="utf-8")), ()
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, (f"cannot read {label}: {exc}",)


def _run_payload(result: RunResult) -> dict[str, Any]:
    return {
        "allowed": result.allowed,
        "errors": list(result.errors),
        "state": dict(result.state),
    }


def _print_payload(payload: Mapping[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    print("PASS" if payload.get("allowed", True) else "BLOCK")
    for error in payload.get("errors", []):
        print(f"  error: {error}")
    state = payload.get("state")
    if isinstance(state, dict) and state:
        print(f"  status: {state.get('status')}")


def _task_or_result(
    raw_task: Path, project_root: Path
) -> tuple[Optional[Path], Optional[RunResult]]:
    task, errors = _direct_task(raw_task, project_root)
    if task is None:
        return None, RunResult(False, errors, {})
    return task, None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)

    start = subparsers.add_parser("start")
    start.add_argument("task", type=Path)
    start.add_argument("--candidate", required=True, type=Path)
    start.add_argument("--project-root", required=True, type=Path)
    start.add_argument("--max-iterations", type=int, default=3)
    start.add_argument("--json", action="store_true")

    transition = subparsers.add_parser("transition")
    transition.add_argument("task", type=Path)
    transition.add_argument(
        "next_phase", choices=("interview", "seed", "execute", "evaluate", "pr-ready")
    )
    transition.add_argument("--project-root", required=True, type=Path)
    transition.add_argument("--json", action="store_true")

    evaluate = subparsers.add_parser("evaluate")
    evaluate.add_argument("task", type=Path)
    evaluate.add_argument("--evaluation", required=True, type=Path)
    evaluate.add_argument("--project-root", required=True, type=Path)
    evaluate.add_argument("--json", action="store_true")

    terminate = subparsers.add_parser("terminate")
    terminate.add_argument("task", type=Path)
    terminate.add_argument("status", choices=sorted(TERMINAL_STATUSES - {"pr-ready", "pr-opened"}))
    terminate.add_argument("--project-root", required=True, type=Path)
    terminate.add_argument("--json", action="store_true")

    status = subparsers.add_parser("status")
    status.add_argument("task", type=Path, nargs="?")
    status.add_argument("--project-root", required=True, type=Path)
    status.add_argument("--json", action="store_true")

    record_pr_parser = subparsers.add_parser("record-pr")
    record_pr_parser.add_argument("task", type=Path)
    record_pr_parser.add_argument("--project-root", required=True, type=Path)
    record_pr_parser.add_argument("--url", required=True)
    record_pr_parser.add_argument("--json", action="store_true")

    args = parser.parse_args()
    if args.operation == "status" and args.task is None:
        task, errors = _active_evolution_task(args.project_root)
        if task is None:
            result = RunResult(False, errors, {})
            _print_payload(_run_payload(result), args.json)
            return 1
        result = _load_state(task)
        _print_payload(_run_payload(result), args.json)
        return 0 if result.allowed else 1

    task, error_result = _task_or_result(args.task, args.project_root)
    if error_result is not None:
        _print_payload(_run_payload(error_result), args.json)
        return 1
    assert task is not None

    if args.operation == "start":
        candidate_value, errors = _read_json_artifact(args.candidate, "candidate")
        if errors:
            result = RunResult(False, errors, {})
        else:
            result = start_run(
                task,
                candidate_value,
                args.max_iterations,
            )
    elif args.operation == "transition":
        result = transition_run(task, args.next_phase)
    elif args.operation == "evaluate":
        evaluation_value, errors = _read_json_artifact(
            args.evaluation, "evaluation"
        )
        result = (
            RunResult(False, errors, {})
            if errors
            else evaluate_run(task, args.project_root, evaluation_value)
        )
    elif args.operation == "terminate":
        result = terminate_run(task, args.status)
    elif args.operation == "record-pr":
        result = record_pr(task, args.project_root, args.url)
    else:
        result = _load_state(task)

    _print_payload(_run_payload(result), args.json)
    return 0 if result.allowed else 1


if __name__ == "__main__":
    sys.exit(main())
