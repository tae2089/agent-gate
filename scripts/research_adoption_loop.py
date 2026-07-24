#!/usr/bin/env python3
"""Requirements-gated contracts for the Agent Loop research adoption pack."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

import evolution_loop
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

REQUEST_SCHEMA_VERSION = 2
ASSESSMENT_SCHEMA_VERSION = 1
EVIDENCE_GRADE_SCHEMA_VERSION = 1
ADOPTION_BRIEF_SCHEMA_VERSION = 1
REQUEST_FIELDS = frozenset(
    {
        "schema_version",
        "source",
        "source_ref",
        "request",
        "question",
        "requirements",
        "constraints",
        "success_criteria",
        "evidence",
    }
)
ASSESSMENT_FIELDS = frozenset({"schema_version", "request_sha256", "criteria"})
REQUIREMENT_CRITERIA = frozenset(
    {
        "clarity",
        "completeness",
        "consistency",
        "necessity",
        "traceability",
        "feasibility",
        "verifiability",
        "atomicity",
    }
)
CRITERION_FIELDS = frozenset({"status", "findings"})
EVIDENCE_GRADE_FIELDS = frozenset(
    {
        "schema_version",
        "request_sha256",
        "grade",
        "sources",
        "rationale",
        "limitations",
    }
)
SOURCE_FIELDS = frozenset({"url", "title", "claims"})
EVIDENCE_GRADES = frozenset({"high", "moderate", "low", "very-low"})
ADOPTION_BRIEF_FIELDS = frozenset(
    {
        "schema_version",
        "request_sha256",
        "requirements_assessment_sha256",
        "evidence_grade_sha256",
        "prototype_result_sha256",
        "scenario_result_sha256",
        "verdict",
        "evidence_certainty",
        "repository_fit",
        "prototype_result",
        "findings",
        "prototype_disposition",
        "evolution_candidate",
    }
)
EVIDENCE_CERTAINTY_FIELDS = frozenset({"grade", "rationale"})
DECISION_AXIS_FIELDS = frozenset({"status", "evidence", "findings"})
EVOLUTION_CANDIDATE_FIELDS = frozenset(
    {"kind", "title", "problem", "evidence", "labels"}
)
STATUSES = frozenset({"pass", "fail"})
VERDICTS = frozenset({"adopt", "reject"})
PROTOTYPE_DISPOSITIONS = frozenset({"adopted", "removed", "not-created"})
PHASE_TRANSITIONS = {
    "frame": frozenset({"requirements-gate"}),
    "requirements-gate": frozenset({"research"}),
    "research": frozenset({"evidence-grade"}),
    "evidence-grade": frozenset({"prototype"}),
    "prototype": frozenset({"verification"}),
    "verification": frozenset({"adopted", "rejected"}),
}
TERMINAL_STATUSES = frozenset(
    {
        "adopted",
        "rejected",
        "needs-clarification",
        "blocked",
    }
)
RESEARCH_ADOPTION_DEFINITION = LoopDefinition(
    name="research adoption",
    transitions=PHASE_TRANSITIONS,
    terminal_statuses=TERMINAL_STATUSES,
    iteration_transitions=frozenset(),
    budget_terminal="blocked",
)
ACTIVE_RESEARCH_ADOPTION_FILENAME = ".active-research-adoption"
REQUEST_FILENAME = "research-request.json"
STATE_FILENAME = "research-adoption-state.json"
ASSESSMENT_FILENAME = "requirements-assessment.json"
EVIDENCE_GRADE_FILENAME = "evidence-grade.json"
PROTOTYPE_RESULT_FILENAME = "prototype-result.json"
ADOPTION_BRIEF_FILENAME = "adoption-brief.json"
EVOLUTION_CANDIDATE_FILENAME = "evolution-candidate.json"
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


Validator = Callable[[Any], ArtifactValidation]


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
    unknown = sorted(str(key) for key in value if key not in fields)
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


def _validate_sha256(
    value: Mapping[str, Any],
    field: str,
    label: str,
    errors: list[str],
) -> None:
    if not _valid_sha256(value.get(field)):
        errors.append(f"{label} {field} must be a lowercase SHA-256")


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
    for field, label in (
        ("requirements", "research requirements"),
        ("constraints", "research constraints"),
        ("success_criteria", "research success criteria"),
        ("evidence", "research evidence"),
    ):
        _string_list(value.get(field), label, errors)
    if value.get("source") != "manual":
        errors.append("research request source must be manual")
    normalized = {field: value[field] for field in REQUEST_FIELDS if field in value}
    return ArtifactValidation(
        not errors,
        tuple(dict.fromkeys(errors)),
        normalized,
    )


def _validate_binary_axis(
    value: Any,
    label: str,
    fields: frozenset[str],
    errors: list[str],
    *,
    evidence_required: bool,
) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        errors.append(f"{label} must be an object")
        return {}
    _exact_fields(value, fields, label, errors)
    status = value.get("status")
    if status not in STATUSES:
        errors.append(f"{label}.status must be pass or fail")
    evidence: list[str] | None = None
    if "evidence" in fields:
        evidence = _string_list(
            value.get("evidence"),
            f"{label}.evidence",
            errors,
            allow_empty=not evidence_required,
        )
    findings = _string_list(
        value.get("findings"),
        f"{label}.findings",
        errors,
        allow_empty=True,
    )
    if status == "pass" and findings:
        errors.append(f"{label} cannot pass with findings")
    if status == "fail" and not findings:
        errors.append(f"{label} fail requires a finding")
    normalized: dict[str, Any] = {
        "status": status,
        "findings": findings,
    }
    if evidence is not None:
        normalized["evidence"] = evidence
    return normalized


def validate_requirements_assessment(value: Any) -> ArtifactValidation:
    if not isinstance(value, dict):
        return ArtifactValidation(
            False,
            ("requirements assessment must be an object",),
            {},
        )
    errors: list[str] = []
    _exact_fields(
        value,
        ASSESSMENT_FIELDS,
        "requirements assessment",
        errors,
    )
    if value.get("schema_version") != ASSESSMENT_SCHEMA_VERSION:
        errors.append(
            "requirements assessment schema_version "
            f"must be {ASSESSMENT_SCHEMA_VERSION}"
        )
    _validate_sha256(
        value,
        "request_sha256",
        "requirements assessment",
        errors,
    )
    raw_criteria = value.get("criteria")
    normalized_criteria: dict[str, Mapping[str, Any]] = {}
    if not isinstance(raw_criteria, dict):
        errors.append("requirements assessment criteria must be an object")
    else:
        _exact_fields(
            raw_criteria,
            REQUIREMENT_CRITERIA,
            "requirements assessment criteria",
            errors,
        )
        for name in sorted(REQUIREMENT_CRITERIA):
            normalized_criteria[name] = _validate_binary_axis(
                raw_criteria.get(name),
                f"requirement criterion {name}",
                CRITERION_FIELDS,
                errors,
                evidence_required=False,
            )
    normalized = {field: value[field] for field in ASSESSMENT_FIELDS if field in value}
    if "criteria" in normalized:
        normalized["criteria"] = normalized_criteria
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


def validate_evidence_grade(value: Any) -> ArtifactValidation:
    if not isinstance(value, dict):
        return ArtifactValidation(
            False,
            ("evidence grade must be an object",),
            {},
        )
    errors: list[str] = []
    _exact_fields(
        value,
        EVIDENCE_GRADE_FIELDS,
        "evidence grade",
        errors,
    )
    if value.get("schema_version") != EVIDENCE_GRADE_SCHEMA_VERSION:
        errors.append(
            f"evidence grade schema_version must be {EVIDENCE_GRADE_SCHEMA_VERSION}"
        )
    _validate_sha256(value, "request_sha256", "evidence grade", errors)
    grade = value.get("grade")
    if grade not in EVIDENCE_GRADES:
        errors.append("evidence grade must be high, moderate, low, or very-low")
    raw_sources = value.get("sources")
    sources: list[Mapping[str, Any]] = []
    if not isinstance(raw_sources, list) or not raw_sources:
        errors.append("evidence grade sources must be a non-empty list")
    else:
        sources = [
            _validate_source(item, index, errors)
            for index, item in enumerate(raw_sources)
        ]
    rationale = _string_list(
        value.get("rationale"),
        "evidence grade rationale",
        errors,
    )
    limitations = _string_list(
        value.get("limitations"),
        "evidence grade limitations",
        errors,
        allow_empty=True,
    )
    normalized = {
        field: value[field] for field in EVIDENCE_GRADE_FIELDS if field in value
    }
    if "sources" in normalized:
        normalized["sources"] = sources
    if "rationale" in normalized:
        normalized["rationale"] = rationale
    if "limitations" in normalized:
        normalized["limitations"] = limitations
    return ArtifactValidation(
        not errors,
        tuple(dict.fromkeys(errors)),
        normalized,
    )


def _validate_evidence_certainty(
    value: Any,
    errors: list[str],
) -> Mapping[str, Any]:
    label = "adoption brief evidence_certainty"
    if not isinstance(value, dict):
        errors.append(f"{label} must be an object")
        return {}
    _exact_fields(value, EVIDENCE_CERTAINTY_FIELDS, label, errors)
    grade = value.get("grade")
    if grade not in EVIDENCE_GRADES:
        errors.append(f"{label}.grade must be high, moderate, low, or very-low")
    rationale = _string_list(
        value.get("rationale"),
        f"{label}.rationale",
        errors,
    )
    return {"grade": grade, "rationale": rationale}


def _validate_evolution_candidate_summary(
    value: Any,
    errors: list[str],
) -> Mapping[str, Any] | None:
    if value is None:
        return None
    label = "adoption brief evolution_candidate"
    if not isinstance(value, dict):
        errors.append(f"{label} must be an object or null")
        return None
    _exact_fields(value, EVOLUTION_CANDIDATE_FIELDS, label, errors)
    for field in ("kind", "title", "problem"):
        _non_empty_string(value.get(field), f"{label}.{field}", errors)
    evidence = _string_list(
        value.get("evidence"),
        f"{label}.evidence",
        errors,
    )
    labels = _string_list(
        value.get("labels"),
        f"{label}.labels",
        errors,
        allow_empty=True,
    )
    normalized = {
        field: value[field] for field in EVOLUTION_CANDIDATE_FIELDS if field in value
    }
    if "evidence" in normalized:
        normalized["evidence"] = evidence
    if "labels" in normalized:
        normalized["labels"] = labels
    return normalized


def validate_adoption_brief(value: Any) -> ArtifactValidation:
    if not isinstance(value, dict):
        return ArtifactValidation(
            False,
            ("adoption brief must be an object",),
            {},
        )
    errors: list[str] = []
    _exact_fields(
        value,
        ADOPTION_BRIEF_FIELDS,
        "adoption brief",
        errors,
    )
    if value.get("schema_version") != ADOPTION_BRIEF_SCHEMA_VERSION:
        errors.append(
            f"adoption brief schema_version must be {ADOPTION_BRIEF_SCHEMA_VERSION}"
        )
    for field in (
        "request_sha256",
        "requirements_assessment_sha256",
        "evidence_grade_sha256",
        "prototype_result_sha256",
        "scenario_result_sha256",
    ):
        _validate_sha256(value, field, "adoption brief", errors)
    verdict = value.get("verdict")
    if verdict not in VERDICTS:
        errors.append(f"unsupported adoption brief verdict: {verdict}")
    evidence_certainty = _validate_evidence_certainty(
        value.get("evidence_certainty"),
        errors,
    )
    repository_fit = _validate_binary_axis(
        value.get("repository_fit"),
        "adoption brief repository_fit",
        DECISION_AXIS_FIELDS,
        errors,
        evidence_required=True,
    )
    prototype_result = _validate_binary_axis(
        value.get("prototype_result"),
        "adoption brief prototype_result",
        DECISION_AXIS_FIELDS,
        errors,
        evidence_required=True,
    )
    findings = _string_list(
        value.get("findings"),
        "adoption brief findings",
        errors,
        allow_empty=True,
    )
    disposition = value.get("prototype_disposition")
    if disposition not in PROTOTYPE_DISPOSITIONS:
        errors.append("adoption brief prototype_disposition is unsupported")
    candidate = _validate_evolution_candidate_summary(
        value.get("evolution_candidate"),
        errors,
    )

    if verdict == "adopt":
        failed_axes = [
            name
            for name, axis in (
                ("repository_fit", repository_fit),
                ("prototype_result", prototype_result),
            )
            if axis.get("status") != "pass"
        ]
        if failed_axes:
            errors.append(
                "adopt brief has failed axes: " + ", ".join(sorted(failed_axes))
            )
        if findings:
            errors.append("adopt brief must not have findings")
        if disposition != "adopted":
            errors.append("adopt brief requires prototype_disposition adopted")
        if candidate is None:
            errors.append("adopt brief requires an Evolution candidate")
    elif verdict == "reject":
        if not findings:
            errors.append("reject brief requires an evidence-backed finding")
        if disposition not in {"removed", "not-created"}:
            errors.append("reject brief requires a removed or absent prototype")
        if value.get("evolution_candidate") is not None:
            errors.append("reject brief must not contain an Evolution candidate")

    normalized = {
        field: value[field] for field in ADOPTION_BRIEF_FIELDS if field in value
    }
    for field, normalized_value in (
        ("evidence_certainty", evidence_certainty),
        ("repository_fit", repository_fit),
        ("prototype_result", prototype_result),
        ("findings", findings),
        ("evolution_candidate", candidate),
    ):
        if field in normalized:
            normalized[field] = normalized_value
    return ArtifactValidation(
        not errors,
        tuple(dict.fromkeys(errors)),
        normalized,
    )


def load_run(task_dir: Path) -> LoopResult:
    return load_managed_run(RESEARCH_ADOPTION_RUN, Path(task_dir))


def start_run(task_dir: Path, request: Any) -> LoopResult:
    validation = validate_request(request)
    if not validation.allowed:
        return LoopResult(False, validation.errors, {})
    return start_managed_run(
        RESEARCH_ADOPTION_RUN,
        Path(task_dir),
        validation.value,
        max_iterations=1,
    )


def transition_run(task_dir: Path, next_phase: str) -> LoopResult:
    task = Path(task_dir)
    loaded = load_run(task)
    if not loaded.allowed:
        return loaded
    allowed_edges = {
        ("frame", "requirements-gate"),
        ("research", "evidence-grade"),
        ("prototype", "verification"),
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
        return (f"cannot read existing {label}: {exc}",)
    if existing != content:
        return (f"{label} already exists with different content",)
    return ()


def assess_requirements(
    task_dir: Path,
    assessment: Any,
) -> LoopResult:
    task = Path(task_dir)
    loaded = load_run(task)
    if not loaded.allowed:
        return loaded
    if loaded.state["status"] != "requirements-gate":
        return LoopResult(
            False,
            ("research adoption run must be in requirements-gate phase",),
            loaded.state,
        )
    validation = validate_requirements_assessment(assessment)
    if not validation.allowed:
        return LoopResult(False, validation.errors, loaded.state)
    value = validation.value
    if value["request_sha256"] != loaded.state["request_sha256"]:
        return LoopResult(
            False,
            ("requirements assessment request_sha256 does not match the active run",),
            loaded.state,
        )
    errors = _persist_once_or_match(
        task / ASSESSMENT_FILENAME,
        canonical_json(value),
        "requirements assessment",
    )
    if errors:
        return LoopResult(False, errors, loaded.state)
    failed = sorted(
        name
        for name, criterion in value["criteria"].items()
        if criterion["status"] == "fail"
    )
    if failed:
        return terminate_managed_run(
            RESEARCH_ADOPTION_RUN,
            task,
            "needs-clarification",
        )
    return transition_managed_run(
        RESEARCH_ADOPTION_RUN,
        task,
        "research",
    )


def submit_evidence_grade(
    task_dir: Path,
    grade: Any,
) -> LoopResult:
    task = Path(task_dir)
    loaded = load_run(task)
    if not loaded.allowed:
        return loaded
    if loaded.state["status"] != "evidence-grade":
        return LoopResult(
            False,
            ("research adoption run must be in evidence-grade phase",),
            loaded.state,
        )
    validation = validate_evidence_grade(grade)
    if not validation.allowed:
        return LoopResult(False, validation.errors, loaded.state)
    value = validation.value
    if value["request_sha256"] != loaded.state["request_sha256"]:
        return LoopResult(
            False,
            ("evidence grade request_sha256 does not match the active run",),
            loaded.state,
        )
    errors = _persist_once_or_match(
        task / EVIDENCE_GRADE_FILENAME,
        canonical_json(value),
        "evidence grade",
    )
    if errors:
        return LoopResult(False, errors, loaded.state)
    return transition_managed_run(
        RESEARCH_ADOPTION_RUN,
        task,
        "prototype",
    )


def capture_prototype_result(
    task_dir: Path,
    project_root: Path,
) -> LoopResult:
    task = Path(task_dir)
    loaded = load_run(task)
    if not loaded.allowed:
        return loaded
    if loaded.state["status"] != "verification":
        return LoopResult(
            False,
            ("research adoption run must be in verification phase",),
            loaded.state,
        )
    source = task / "scenario-result.json"
    archive = (
        task
        / "iterations"
        / f"{loaded.state['iteration']:03d}"
        / PROTOTYPE_RESULT_FILENAME
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
    errors = _persist_once_or_match(
        archive,
        content,
        "captured prototype result",
    )
    if errors:
        return LoopResult(False, errors, loaded.state)
    return LoopResult(True, (), loaded.state)


def _read_validated_artifact(
    path: Path,
    label: str,
    validator: Validator,
) -> tuple[Mapping[str, Any], bytes, tuple[str, ...]]:
    if path.is_symlink():
        return {}, b"", (f"{label} must not be a symlink",)
    try:
        content = path.read_bytes()
        raw = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {}, b"", (f"cannot read {label}: {exc}",)
    validation = validator(raw)
    return validation.value, content, validation.errors


def _build_evolution_candidate(
    brief: Mapping[str, Any],
    request: Mapping[str, Any],
) -> Mapping[str, Any]:
    summary = brief["evolution_candidate"]
    return {
        "schema_version": 1,
        "kind": summary["kind"],
        "source": "manual",
        "source_ref": request["source_ref"],
        "title": summary["title"],
        "problem": summary["problem"],
        "evidence": summary["evidence"],
        "labels": summary["labels"],
        "request": request["request"],
    }


def _validate_brief_bindings(
    task: Path,
    project_root: Path,
    state: Mapping[str, Any],
    brief: Mapping[str, Any],
) -> tuple[Mapping[str, Any] | None, tuple[str, ...]]:
    errors: list[str] = []
    if brief["request_sha256"] != state["request_sha256"]:
        errors.append("adoption brief request_sha256 does not match the active run")

    assessment, assessment_content, assessment_errors = _read_validated_artifact(
        task / ASSESSMENT_FILENAME,
        "requirements assessment",
        validate_requirements_assessment,
    )
    errors.extend(assessment_errors)
    if assessment and (
        content_sha256(assessment_content) != brief["requirements_assessment_sha256"]
    ):
        errors.append("requirements_assessment_sha256 is stale")
    if assessment and assessment.get("request_sha256") != state["request_sha256"]:
        errors.append("requirements assessment does not match the active request")
    if assessment and any(
        criterion.get("status") != "pass"
        for criterion in assessment.get("criteria", {}).values()
    ):
        errors.append("requirements Gate is not fully passed")

    grade, grade_content, grade_errors = _read_validated_artifact(
        task / EVIDENCE_GRADE_FILENAME,
        "evidence grade",
        validate_evidence_grade,
    )
    errors.extend(grade_errors)
    if grade and content_sha256(grade_content) != brief["evidence_grade_sha256"]:
        errors.append("evidence_grade_sha256 is stale")
    if grade and grade.get("request_sha256") != state["request_sha256"]:
        errors.append("evidence grade does not match the active request")
    if grade and brief["evidence_certainty"] != {
        "grade": grade.get("grade"),
        "rationale": grade.get("rationale"),
    }:
        errors.append("adoption brief evidence_certainty is stale")

    prototype_path = (
        task / "iterations" / f"{state['iteration']:03d}" / PROTOTYPE_RESULT_FILENAME
    )
    if prototype_path.is_symlink():
        errors.append("captured prototype result must not be a symlink")
    else:
        try:
            prototype_content = prototype_path.read_bytes()
        except OSError as exc:
            errors.append(f"cannot read captured prototype result: {exc}")
        else:
            if content_sha256(prototype_content) != brief["prototype_result_sha256"]:
                errors.append("prototype_result_sha256 is stale")

    receipt = validate_scenario_receipt(
        task,
        project_root,
        brief["scenario_result_sha256"],
        require_completion=True,
    )
    errors.extend(receipt.errors)

    request_value, _, request_errors = _read_validated_artifact(
        task / REQUEST_FILENAME,
        "research request",
        validate_request,
    )
    errors.extend(request_errors)
    candidate: Mapping[str, Any] | None = None
    if brief["verdict"] == "adopt" and request_value:
        candidate = _build_evolution_candidate(brief, request_value)
        validation = evolution_loop.validate_candidate(candidate)
        errors.extend(f"Evolution candidate: {error}" for error in validation.errors)
        if validation.allowed:
            candidate = validation.candidate
    return candidate, tuple(dict.fromkeys(errors))


def submit_adoption_brief(
    task_dir: Path,
    project_root: Path,
    brief: Any,
) -> LoopResult:
    task = Path(task_dir)
    loaded = load_run(task)
    if not loaded.allowed:
        return loaded
    if loaded.state["status"] != "verification":
        return LoopResult(
            False,
            ("research adoption run must be in verification phase",),
            loaded.state,
        )
    validation = validate_adoption_brief(brief)
    if not validation.allowed:
        return LoopResult(False, validation.errors, loaded.state)
    value = validation.value
    _, binding_errors = _validate_brief_bindings(
        task,
        Path(project_root),
        loaded.state,
        value,
    )
    if binding_errors:
        return LoopResult(False, binding_errors, loaded.state)
    errors = _persist_once_or_match(
        task / ADOPTION_BRIEF_FILENAME,
        canonical_json(value),
        "adoption brief",
    )
    if errors:
        return LoopResult(False, errors, loaded.state)
    target = "adopted" if value["verdict"] == "adopt" else "rejected"
    return transition_managed_run(RESEARCH_ADOPTION_RUN, task, target)


def export_evolution_candidate(
    task_dir: Path,
    project_root: Path,
) -> LoopResult:
    task = Path(task_dir)
    loaded = load_run(task)
    if not loaded.allowed:
        return loaded
    if loaded.state["status"] != "adopted":
        return LoopResult(
            False,
            ("only an adopted brief can be handed to Evolution",),
            loaded.state,
        )
    brief, _, brief_errors = _read_validated_artifact(
        task / ADOPTION_BRIEF_FILENAME,
        "adoption brief",
        validate_adoption_brief,
    )
    if brief_errors:
        return LoopResult(False, brief_errors, loaded.state)
    if brief.get("verdict") != "adopt":
        return LoopResult(
            False,
            ("only an adopted brief can be handed to Evolution",),
            loaded.state,
        )
    candidate, binding_errors = _validate_brief_bindings(
        task,
        Path(project_root),
        loaded.state,
        brief,
    )
    if binding_errors or candidate is None:
        return LoopResult(False, binding_errors, loaded.state)
    errors = _persist_once_or_match(
        task / EVOLUTION_CANDIDATE_FILENAME,
        canonical_json(candidate),
        "Evolution candidate",
    )
    if errors:
        return LoopResult(False, errors, loaded.state)
    return LoopResult(True, (), loaded.state)


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


def _artifact_result(
    path: Path,
    label: str,
    action: Callable[[Any], LoopResult],
) -> LoopResult:
    value, errors = _read_json_artifact(path, label)
    if errors:
        return LoopResult(False, errors, {})
    return action(value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)

    start = subparsers.add_parser("start")
    start.add_argument("task", type=Path)
    start.add_argument("--request", required=True, type=Path)
    start.add_argument("--project-root", required=True, type=Path)
    start.add_argument("--json", action="store_true")

    transition_parser = subparsers.add_parser("transition")
    transition_parser.add_argument("task", type=Path)
    transition_parser.add_argument(
        "next_phase",
        choices=("requirements-gate", "evidence-grade", "verification"),
    )
    transition_parser.add_argument("--project-root", required=True, type=Path)
    transition_parser.add_argument("--json", action="store_true")

    assess = subparsers.add_parser("assess")
    assess.add_argument("task", type=Path)
    assess.add_argument("--assessment", required=True, type=Path)
    assess.add_argument("--project-root", required=True, type=Path)
    assess.add_argument("--json", action="store_true")

    grade = subparsers.add_parser("grade")
    grade.add_argument("task", type=Path)
    grade.add_argument("--grade", required=True, type=Path)
    grade.add_argument("--project-root", required=True, type=Path)
    grade.add_argument("--json", action="store_true")

    capture = subparsers.add_parser("capture")
    capture.add_argument("task", type=Path)
    capture.add_argument("--project-root", required=True, type=Path)
    capture.add_argument("--json", action="store_true")

    submit = subparsers.add_parser("submit")
    submit.add_argument("task", type=Path)
    submit.add_argument("--brief", required=True, type=Path)
    submit.add_argument("--project-root", required=True, type=Path)
    submit.add_argument("--json", action="store_true")

    handoff = subparsers.add_parser("handoff")
    handoff.add_argument("task", type=Path)
    handoff.add_argument("--project-root", required=True, type=Path)
    handoff.add_argument("--json", action="store_true")

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
        result = _artifact_result(
            args.request,
            "research request",
            lambda value: start_run(task, value),
        )
    elif args.operation == "transition":
        result = transition_run(task, args.next_phase)
    elif args.operation == "assess":
        result = _artifact_result(
            args.assessment,
            "requirements assessment",
            lambda value: assess_requirements(task, value),
        )
    elif args.operation == "grade":
        result = _artifact_result(
            args.grade,
            "evidence grade",
            lambda value: submit_evidence_grade(task, value),
        )
    elif args.operation == "capture":
        result = capture_prototype_result(task, args.project_root)
    elif args.operation == "submit":
        result = _artifact_result(
            args.brief,
            "adoption brief",
            lambda value: submit_adoption_brief(
                task,
                args.project_root,
                value,
            ),
        )
    elif args.operation == "handoff":
        result = export_evolution_candidate(task, args.project_root)
    elif args.operation == "terminate":
        result = terminate_run(task, args.status)
    else:
        result = load_run(task)

    _print_payload(result, args.json, task)
    return 0 if result.allowed else 1


if __name__ == "__main__":
    sys.exit(main())
