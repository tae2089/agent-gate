#!/usr/bin/env python3
"""Validate content-bound task clarity and implementation readiness.

The semantic judge proposes dimension scores and exact evidence in
``assessment.json``. This module owns the deterministic policy: artifact
structure, score ranges and floors, weighted aggregates, AC traceability,
content hashes, and evidence membership.

Exit codes: 0 ready/template emitted, 1 not ready, 2 usage/read error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from artifact_lint import AC_ID_PATTERN, lint_file

SCHEMA_VERSION = 1
TASK_AMBIGUITY_MAX = 0.20
IMPLEMENTATION_READINESS_MIN = 0.80
AC_PATTERN = re.compile(AC_ID_PATTERN)
INHERITANCE_FILENAME = "inherited-readiness.json"
INHERITANCE_MODE = "inherit-full"
INHERITANCE_FIELDS = frozenset({
    "schema_version",
    "mode",
    "parent_task",
    "child_task_sha256",
    "flow_refs",
    "acceptance_refs",
})
P_REF_PATTERN = re.compile(r"\bP[1-9]\d*\b")

# name: (weight, minimum score)
TASK_DIMENSIONS = {
    "outcome_clarity": (0.35, 0.75),
    "constraint_clarity": (0.25, 0.65),
    "acceptance_clarity": (0.25, 0.70),
    "grounding_clarity": (0.15, 0.60),
}

# The three dimensions below sum to 0.65; AC coverage contributes the rest.
AC_COVERAGE_WEIGHT = 0.35
IMPLEMENTATION_DIMENSIONS = {
    "decision_closure": 0.30,
    "change_specificity": 0.20,
    "risk_response": 0.15,
}


@dataclass(frozen=True)
class ValidationResult:
    ready: bool
    errors: tuple[str, ...]
    task_ambiguity: float | None
    implementation_readiness: float | None
    ac_coverage: float | None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["errors"] = list(self.errors)
        return value


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _read_artifact(path: Path, label: str, errors: list[str]) -> tuple[bytes, str] | None:
    try:
        content = path.read_bytes()
        return content, content.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        errors.append(f"cannot read {label}: {exc}")
        return None


def _load_assessment(path: Path, errors: list[str]) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        errors.append(f"cannot read assessment.json: {exc}")
        return None
    if not isinstance(value, dict):
        errors.append("assessment.json must contain a JSON object")
        return None
    return value


def _section(value: Any, label: str, errors: list[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        errors.append(f"{label} must be an object")
        return {}
    return value


def _validate_hash(
    section: dict[str, Any], label: str, content: bytes, errors: list[str]
) -> None:
    actual = _sha256(content)
    claimed = section.get("sha256")
    if claimed != actual:
        errors.append(f"{label}.sha256 is missing or stale")


def _score(value: Any, label: str, errors: list[str]) -> float | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0.0 <= value <= 1.0
    ):
        errors.append(f"{label} must be a finite number in [0, 1]")
        return None
    return float(value)


def _validate_dimensions(
    section: dict[str, Any],
    expected: tuple[str, ...],
    source: str,
    label: str,
    errors: list[str],
) -> dict[str, float]:
    dimensions = _section(section.get("dimensions"), f"{label}.dimensions", errors)
    scores: dict[str, float] = {}
    for name in expected:
        entry = _section(dimensions.get(name), f"{label}.{name}", errors)
        score = _score(entry.get("score"), f"{label}.{name}.score", errors)
        if score is not None:
            scores[name] = score

        evidence = entry.get("evidence")
        if not isinstance(evidence, str) or not evidence.strip() or evidence not in source:
            errors.append(f"{label}.{name}.evidence must be a non-empty exact source excerpt")
    return scores


def _require_empty_list(section: dict[str, Any], key: str, label: str, errors: list[str]) -> None:
    value = section.get(key)
    if not isinstance(value, list):
        errors.append(f"{label}.{key} must be a list")
    elif value:
        errors.append(f"{label}.{key} must be empty before source editing")


def _structural_lint(task_dir: Path, errors: list[str]) -> None:
    for filename, artifact_type in (
        ("task.md", "task"),
        ("implementation.md", "implementation"),
    ):
        result = lint_file(task_dir / filename, artifact_type)
        if result is None:
            continue
        if not result["passed"]:
            failed = [key for key, passed in result["checks"].items() if not passed]
            errors.append(f"{filename} fails structural lint: {', '.join(failed)}")


def _ac_coverage(task_text: str, implementation_text: str, errors: list[str]) -> float:
    task_ids = tuple(dict.fromkeys(AC_PATTERN.findall(task_text)))
    implementation_ids = set(AC_PATTERN.findall(implementation_text))
    if not task_ids:
        errors.append("task.md defines no AC-number identifiers")
        return 0.0
    missing = [ac_id for ac_id in task_ids if ac_id not in implementation_ids]
    if missing:
        errors.append(f"implementation.md is missing acceptance references: {', '.join(missing)}")
    return (len(task_ids) - len(missing)) / len(task_ids)


def _validate_full_task_dir(task_dir: Path | str) -> ValidationResult:
    """Validate one self-contained Full task without inheritance dispatch."""
    task_dir = Path(task_dir)
    errors: list[str] = []
    task_artifact = _read_artifact(task_dir / "task.md", "task.md", errors)
    implementation_artifact = _read_artifact(
        task_dir / "implementation.md", "implementation.md", errors
    )
    assessment = _load_assessment(task_dir / "assessment.json", errors)

    if task_artifact is None or implementation_artifact is None or assessment is None:
        return ValidationResult(False, tuple(errors), None, None, None)

    task_bytes, task_text = task_artifact
    implementation_bytes, implementation_text = implementation_artifact
    _structural_lint(task_dir, errors)

    if assessment.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")

    task = _section(assessment.get("task"), "task", errors)
    implementation = _section(assessment.get("implementation"), "implementation", errors)
    _validate_hash(task, "task", task_bytes, errors)
    _validate_hash(implementation, "implementation", implementation_bytes, errors)

    task_scores = _validate_dimensions(
        task, tuple(TASK_DIMENSIONS), task_text, "task", errors
    )
    for name, (_, floor) in TASK_DIMENSIONS.items():
        score = task_scores.get(name)
        if score is not None and score < floor:
            errors.append(f"task.{name} floor is {floor:.2f}, got {score:.3f}")

    task_ambiguity = None
    if len(task_scores) == len(TASK_DIMENSIONS):
        clarity = sum(
            task_scores[name] * weight
            for name, (weight, _) in TASK_DIMENSIONS.items()
        )
        task_ambiguity = round(1.0 - clarity, 6)
        if task_ambiguity > TASK_AMBIGUITY_MAX:
            errors.append(
                f"task ambiguity must be <= {TASK_AMBIGUITY_MAX:.2f}, got {task_ambiguity:.3f}"
            )
    _require_empty_list(task, "blocking_unknowns", "task", errors)

    coverage = round(_ac_coverage(task_text, implementation_text, errors), 6)
    implementation_scores = _validate_dimensions(
        implementation,
        tuple(IMPLEMENTATION_DIMENSIONS),
        implementation_text,
        "implementation",
        errors,
    )
    _require_empty_list(
        implementation, "unresolved_decisions", "implementation", errors
    )

    implementation_readiness = None
    if len(implementation_scores) == len(IMPLEMENTATION_DIMENSIONS):
        implementation_readiness = AC_COVERAGE_WEIGHT * coverage + sum(
            implementation_scores[name] * weight
            for name, weight in IMPLEMENTATION_DIMENSIONS.items()
        )
        implementation_readiness = round(implementation_readiness, 6)
        if implementation_readiness < IMPLEMENTATION_READINESS_MIN:
            errors.append(
                "implementation readiness must be "
                f">= {IMPLEMENTATION_READINESS_MIN:.2f}, got {implementation_readiness:.3f}"
            )

    return ValidationResult(
        ready=not errors,
        errors=tuple(errors),
        task_ambiguity=task_ambiguity,
        implementation_readiness=implementation_readiness,
        ac_coverage=coverage,
    )


def _manifest_object(path: Path, errors: list[str]) -> dict[str, Any] | None:
    if path.is_symlink():
        errors.append(f"{INHERITANCE_FILENAME} must not be a symlink")
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        errors.append(f"cannot read {INHERITANCE_FILENAME}: {exc}")
        return None
    if not isinstance(value, dict):
        errors.append(f"{INHERITANCE_FILENAME} must contain a JSON object")
        return None
    return value


def _workspace_task(task_dir: Path, label: str, errors: list[str]) -> Path | None:
    """Resolve a direct ``_workspace/<slug>`` directory without symlinks."""
    try:
        if task_dir.is_symlink() or task_dir.parent.is_symlink():
            errors.append(f"{label} must not be a symlink")
            return None
        resolved = task_dir.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        errors.append(f"cannot resolve {label}: {exc}")
        return None
    if (
        not resolved.is_dir()
        or resolved.parent.name != "_workspace"
        or not resolved.name
        or resolved.name.startswith(".")
    ):
        errors.append(f"{label} must be a direct _workspace/<task> directory")
        return None
    return resolved


def _parent_task(child: Path, value: Any, errors: list[str]) -> Path | None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or value.startswith(".")
        or Path(value).parts != (value,)
    ):
        errors.append("parent_task must be one safe sibling task name")
        return None
    candidate = child.parent / value
    if candidate == child:
        errors.append("parent_task must not reference the child itself")
        return None
    resolved = _workspace_task(candidate, "parent task", errors)
    if resolved is None:
        return None
    inherited = resolved / INHERITANCE_FILENAME
    if inherited.is_symlink() or inherited.exists():
        errors.append("parent task must be a direct Full task, not another inherited task")
        return None
    return resolved


def _reference_list(
    manifest: dict[str, Any], key: str, pattern: re.Pattern[str], errors: list[str]
) -> tuple[str, ...]:
    value = manifest.get(key)
    if not isinstance(value, list) or not value:
        errors.append(f"{key} must be a non-empty list")
        return ()
    refs: list[str] = []
    for item in value:
        if not isinstance(item, str) or pattern.fullmatch(item) is None:
            errors.append(f"{key} contains an invalid reference: {item!r}")
            continue
        refs.append(item)
    if len(refs) != len(set(refs)):
        errors.append(f"{key} contains duplicate references")
    return tuple(dict.fromkeys(refs))


def _validate_inherited_task_dir(task_dir: Path) -> ValidationResult:
    errors: list[str] = []
    child = _workspace_task(task_dir, "child task", errors)
    if child is None:
        return ValidationResult(False, tuple(errors), None, None, None)

    manifest = _manifest_object(child / INHERITANCE_FILENAME, errors)
    child_artifact = _read_artifact(child / "task.md", "child task.md", errors)
    if manifest is None or child_artifact is None:
        return ValidationResult(False, tuple(errors), None, None, None)
    child_bytes, child_text = child_artifact

    missing_fields = sorted(INHERITANCE_FIELDS - manifest.keys())
    unknown_fields = sorted(manifest.keys() - INHERITANCE_FIELDS)
    if missing_fields:
        errors.append(f"inheritance manifest is missing fields: {', '.join(missing_fields)}")
    if unknown_fields:
        errors.append(f"inheritance manifest has unknown fields: {', '.join(unknown_fields)}")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if manifest.get("mode") != INHERITANCE_MODE:
        errors.append(f"mode must be {INHERITANCE_MODE!r}")
    if manifest.get("child_task_sha256") != _sha256(child_bytes):
        errors.append("child_task_sha256 is missing or stale")
    for filename in ("implementation.md", "assessment.json"):
        path = child / filename
        if path.is_symlink() or path.exists():
            errors.append(f"inherited child must not define {filename}")

    child_lint = lint_file(child / "task.md", "task")
    if child_lint is None:
        errors.append("cannot lint child task.md")
    elif not child_lint["passed"]:
        failed = [key for key, passed in child_lint["checks"].items() if not passed]
        errors.append(f"child task.md fails structural lint: {', '.join(failed)}")

    flow_refs = _reference_list(manifest, "flow_refs", P_REF_PATTERN, errors)
    acceptance_refs = _reference_list(manifest, "acceptance_refs", AC_PATTERN, errors)
    parent = _parent_task(child, manifest.get("parent_task"), errors)
    if parent is None:
        return ValidationResult(False, tuple(errors), None, None, None)

    parent_result = _validate_full_task_dir(parent)
    if not parent_result.ready:
        errors.extend(f"parent readiness: {error}" for error in parent_result.errors)
        return ValidationResult(
            False,
            tuple(errors),
            parent_result.task_ambiguity,
            parent_result.implementation_readiness,
            parent_result.ac_coverage,
        )

    try:
        parent_task_text = (parent / "task.md").read_text(encoding="utf-8")
        parent_implementation_text = (parent / "implementation.md").read_text(
            encoding="utf-8"
        )
    except (OSError, UnicodeError) as exc:
        errors.append(f"cannot read parent flow artifacts: {exc}")
    else:
        parent_flow_refs = set(P_REF_PATTERN.findall(parent_implementation_text))
        parent_acceptance_refs = set(AC_PATTERN.findall(parent_task_text))
        child_acceptance_refs = set(AC_PATTERN.findall(child_text))
        for ref in flow_refs:
            if ref not in parent_flow_refs:
                errors.append(f"flow reference {ref} is missing from parent implementation.md")
        for ref in acceptance_refs:
            if ref not in parent_acceptance_refs:
                errors.append(f"acceptance reference {ref} is missing from parent task.md")
            if ref not in child_acceptance_refs:
                errors.append(f"acceptance reference {ref} is missing from child task.md")
        unbound_child_refs = sorted(child_acceptance_refs - set(acceptance_refs))
        if unbound_child_refs:
            errors.append(
                "child task.md has acceptance references not bound by the manifest: "
                + ", ".join(unbound_child_refs)
            )

    return ValidationResult(
        ready=not errors,
        errors=tuple(errors),
        task_ambiguity=parent_result.task_ambiguity,
        implementation_readiness=parent_result.implementation_readiness,
        ac_coverage=parent_result.ac_coverage,
    )


def validate_task_dir(task_dir: Path | str) -> ValidationResult:
    """Validate a self-contained Full task or a child inheriting one."""
    path = Path(task_dir)
    manifest = path / INHERITANCE_FILENAME
    if manifest.is_symlink() or manifest.exists():
        return _validate_inherited_task_dir(path)
    return _validate_full_task_dir(path)


def assessment_template(task_dir: Path | str) -> dict[str, Any]:
    """Create an unscored assessment skeleton bound to current artifact bytes."""
    task_dir = Path(task_dir)
    task_bytes = (task_dir / "task.md").read_bytes()
    implementation_bytes = (task_dir / "implementation.md").read_bytes()

    def dimensions(names: tuple[str, ...]) -> dict[str, dict[str, Any]]:
        return {name: {"score": 0.0, "evidence": ""} for name in names}

    return {
        "schema_version": SCHEMA_VERSION,
        "task": {
            "sha256": _sha256(task_bytes),
            "dimensions": dimensions(tuple(TASK_DIMENSIONS)),
            "blocking_unknowns": [],
        },
        "implementation": {
            "sha256": _sha256(implementation_bytes),
            "dimensions": dimensions(tuple(IMPLEMENTATION_DIMENSIONS)),
            "unresolved_decisions": [],
        },
    }


def inheritance_template(
    child_dir: Path | str, parent_dir: Path | str
) -> dict[str, Any]:
    """Create a child manifest bound to a ready direct Full parent."""
    errors: list[str] = []
    child = _workspace_task(Path(child_dir), "child task", errors)
    if child is None:
        raise ValueError("; ".join(errors))
    parent_input = Path(parent_dir)
    if parent_input.is_symlink():
        raise ValueError("parent task must not be a symlink")
    try:
        parent_resolved = parent_input.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"cannot resolve parent task: {exc}") from exc
    parent = _parent_task(child, parent_resolved.name, errors)
    if parent is None or parent != parent_resolved:
        raise ValueError(
            "; ".join(errors or ["parent task must be a sibling of child task"])
        )
    parent_result = _validate_full_task_dir(parent)
    if not parent_result.ready:
        raise ValueError("parent task is not ready: " + "; ".join(parent_result.errors[:3]))
    child_artifact = _read_artifact(child / "task.md", "child task.md", errors)
    if child_artifact is None:
        raise ValueError("; ".join(errors))
    child_bytes, _ = child_artifact
    child_lint = lint_file(child / "task.md", "task")
    if child_lint is None or not child_lint["passed"]:
        raise ValueError("child task.md must pass structural lint")
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": INHERITANCE_MODE,
        "parent_task": parent.name,
        "child_task_sha256": _sha256(child_bytes),
        "flow_refs": [],
        "acceptance_refs": [],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable validation"
    )
    templates = parser.add_mutually_exclusive_group()
    templates.add_argument(
        "--template",
        action="store_true",
        help="emit a content-bound assessment skeleton",
    )
    templates.add_argument(
        "--inherit-from",
        metavar="PARENT_TASK",
        help="emit a child manifest inheriting a ready direct Full parent",
    )
    parser.add_argument("task_dir")
    args = parser.parse_args()
    task_dir = Path(args.task_dir)

    if args.template:
        try:
            template = assessment_template(task_dir)
        except OSError as exc:
            print(f"cannot create template: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(template, ensure_ascii=False, indent=2))
        return 0
    if args.inherit_from:
        try:
            template = inheritance_template(task_dir, Path(args.inherit_from))
        except (OSError, ValueError) as exc:
            print(f"cannot create inheritance template: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(template, ensure_ascii=False, indent=2))
        return 0

    result = validate_task_dir(task_dir)
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False))
    else:
        status = "READY" if result.ready else "NOT READY"
        print(
            f"{status} task_ambiguity={result.task_ambiguity} "
            f"implementation_readiness={result.implementation_readiness} "
            f"ac_coverage={result.ac_coverage}"
        )
        for error in result.errors:
            print(f"  - {error}")
    return 0 if result.ready else 1


if __name__ == "__main__":
    sys.exit(main())
