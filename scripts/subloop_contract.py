#!/usr/bin/env python3
"""Strict parent-child contracts for hierarchical Agent Loop execution."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping

from loop_engine import canonical_json, content_sha256

SCHEMA_VERSION = 1
EXECUTION_MODES = frozenset({"standalone", "subloop"})
RESULT_STATUSES = frozenset(
    {
        "completed",
        "changes-requested",
        "needs-decision",
        "blocked",
        "budget-exhausted",
    }
)
SUBLOOP_PERMISSIONS = frozenset(
    {
        "read-repository",
        "modify-worktree",
        "run-local-verification",
        "read-external-sources",
    }
)
INVOCATION_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
INVOCATION_FIELDS = frozenset(
    {
        "schema_version",
        "invocation_id",
        "pack",
        "mode",
        "parent",
        "objective",
        "requirements",
        "scope",
        "source_snapshot",
        "permissions",
        "budget",
        "completion_task_ref",
    }
)
PARENT_FIELDS = frozenset({"run_id", "task_ref", "state_sha256"})
PARENT_CONTEXT_FIELDS = frozenset(
    {
        "run_id",
        "task_ref",
        "state_sha256",
        "scope",
        "permissions",
        "remaining_iterations",
        "source_snapshot_sha256",
    }
)
SOURCE_SNAPSHOT_FIELDS = frozenset({"ref", "sha256"})
BUDGET_FIELDS = frozenset({"iteration_limit"})
RESULT_FIELDS = frozenset(
    {
        "schema_version",
        "invocation_id",
        "invocation_sha256",
        "pack",
        "status",
        "summary",
        "finding_refs",
        "changed_paths",
        "evidence_refs",
        "budget_usage",
        "completion_receipt",
        "decision",
        "source_snapshot_after_sha256",
    }
)
BUDGET_USAGE_FIELDS = frozenset({"iterations_used"})
COMPLETION_RECEIPT_FIELDS = frozenset({"task_ref", "scenario_result_sha256"})
DECISION_FIELDS = frozenset({"question", "options"})


@dataclass(frozen=True)
class PackProfile:
    name: str
    supported_modes: frozenset[str]

    def __post_init__(self) -> None:
        if not self.name or not self.name.endswith("-loop"):
            raise ValueError("Pack profile name must be a non-empty *-loop name")
        if not self.supported_modes or not self.supported_modes <= EXECUTION_MODES:
            raise ValueError("Pack profile has unsupported execution modes")

    def supports(self, mode: str) -> bool:
        return mode in self.supported_modes


@dataclass(frozen=True)
class ContractValidation:
    allowed: bool
    errors: tuple[str, ...]
    value: Mapping[str, Any]
    sha256: str | None = None


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


def _object(
    value: Any,
    label: str,
    errors: list[str],
) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        errors.append(f"{label} must be an object")
        return {}
    return value


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


def _relative_path(value: Any, label: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not value.strip() or "\\" in value:
        errors.append(f"{label} must be a relative POSIX path")
        return
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or value.startswith("./"):
        errors.append(f"{label} must be a relative POSIX path")


def _path_is_within(path: str, boundary: str) -> bool:
    if boundary == ".":
        return True
    candidate = PurePosixPath(path)
    parent = PurePosixPath(boundary)
    return candidate == parent or parent in candidate.parents


def _scope_is_subset(child: list[str], parent: list[str]) -> bool:
    return all(
        any(_path_is_within(child_path, parent_path) for parent_path in parent)
        for child_path in child
    )


def _validate_parent_context(
    value: Any,
    errors: list[str],
) -> Mapping[str, Any]:
    context = _object(value, "parent context", errors)
    _exact_fields(context, PARENT_CONTEXT_FIELDS, "parent context", errors)
    for field in ("run_id", "task_ref"):
        _non_empty_string(context.get(field), f"parent context {field}", errors)
    if not _valid_sha256(context.get("state_sha256")):
        errors.append("parent context state_sha256 must be a lowercase SHA-256")
    if not _valid_sha256(context.get("source_snapshot_sha256")):
        errors.append(
            "parent context source_snapshot_sha256 must be a lowercase SHA-256"
        )
    scope = _string_list(context.get("scope"), "parent context scope", errors)
    for index, item in enumerate(scope):
        _relative_path(item, f"parent context scope[{index}]", errors)
    permissions = _string_list(
        context.get("permissions"),
        "parent context permissions",
        errors,
        allow_empty=True,
    )
    unsupported = sorted(set(permissions) - SUBLOOP_PERMISSIONS)
    if unsupported:
        errors.append(
            "parent context has unsupported permissions: " + ", ".join(unsupported)
        )
    remaining = context.get("remaining_iterations")
    if (
        isinstance(remaining, bool)
        or not isinstance(remaining, int)
        or not 0 <= remaining <= 10
    ):
        errors.append(
            "parent context remaining_iterations must be an integer from 0 through 10"
        )
    return context


def validate_invocation(
    value: Any,
    profile: PackProfile,
    parent_context: Any,
) -> ContractValidation:
    if not isinstance(value, dict):
        return ContractValidation(
            False,
            ("Subloop invocation must be an object",),
            {},
        )
    errors: list[str] = []
    _exact_fields(value, INVOCATION_FIELDS, "Subloop invocation", errors)
    context = _validate_parent_context(parent_context, errors)
    if value.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"Subloop invocation schema_version must be {SCHEMA_VERSION}")
    invocation_id = value.get("invocation_id")
    if (
        not isinstance(invocation_id, str)
        or INVOCATION_ID.fullmatch(invocation_id) is None
    ):
        errors.append("Subloop invocation_id is invalid")
    if value.get("pack") != profile.name:
        errors.append("Subloop invocation pack does not match the Pack profile")
    mode = value.get("mode")
    if mode != "subloop":
        errors.append("hierarchical invocation mode must be subloop")
    elif not profile.supports(mode):
        errors.append(f"{profile.name} does not support subloop mode")

    parent = _object(value.get("parent"), "Subloop invocation parent", errors)
    _exact_fields(parent, PARENT_FIELDS, "Subloop invocation parent", errors)
    for field in ("run_id", "task_ref", "state_sha256"):
        if parent.get(field) != context.get(field):
            label = "parent state" if field == "state_sha256" else f"parent {field}"
            errors.append(f"Subloop invocation {label} is stale")

    _non_empty_string(
        value.get("objective"),
        "Subloop invocation objective",
        errors,
    )
    _string_list(
        value.get("requirements"),
        "Subloop invocation requirements",
        errors,
    )
    scope = _string_list(value.get("scope"), "Subloop invocation scope", errors)
    for index, item in enumerate(scope):
        _relative_path(item, f"Subloop invocation scope[{index}]", errors)
    parent_scope = context.get("scope")
    if (
        scope
        and isinstance(parent_scope, list)
        and not _scope_is_subset(scope, parent_scope)
    ):
        errors.append("Subloop invocation scope expands the parent scope")

    snapshot = _object(
        value.get("source_snapshot"),
        "Subloop invocation source_snapshot",
        errors,
    )
    _exact_fields(
        snapshot,
        SOURCE_SNAPSHOT_FIELDS,
        "Subloop invocation source_snapshot",
        errors,
    )
    _relative_path(
        snapshot.get("ref"),
        "Subloop invocation source_snapshot ref",
        errors,
    )
    if not _valid_sha256(snapshot.get("sha256")):
        errors.append(
            "Subloop invocation source_snapshot sha256 must be a lowercase SHA-256"
        )
    elif snapshot.get("sha256") != context.get("source_snapshot_sha256"):
        errors.append("Subloop invocation source snapshot is stale")

    permissions = _string_list(
        value.get("permissions"),
        "Subloop invocation permissions",
        errors,
        allow_empty=True,
    )
    unsupported = sorted(set(permissions) - SUBLOOP_PERMISSIONS)
    if unsupported:
        errors.append(
            "Subloop invocation has unsupported permissions: " + ", ".join(unsupported)
        )
    parent_permissions = context.get("permissions")
    if isinstance(parent_permissions, list) and not set(permissions) <= set(
        parent_permissions
    ):
        errors.append("Subloop invocation permissions expand parent permissions")

    budget = _object(value.get("budget"), "Subloop invocation budget", errors)
    _exact_fields(budget, BUDGET_FIELDS, "Subloop invocation budget", errors)
    limit = budget.get("iteration_limit")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
        errors.append(
            "Subloop invocation iteration_limit must be an integer from 1 through 10"
        )
    remaining = context.get("remaining_iterations")
    if (
        isinstance(limit, int)
        and not isinstance(limit, bool)
        and isinstance(remaining, int)
        and not isinstance(remaining, bool)
        and limit > remaining
    ):
        errors.append("Subloop invocation budget exceeds parent remaining iterations")

    if value.get("completion_task_ref") != context.get("task_ref"):
        errors.append(
            "Subloop invocation completion_task_ref must name the parent task"
        )

    normalized = {field: value[field] for field in INVOCATION_FIELDS if field in value}
    content = canonical_json(normalized)
    return ContractValidation(
        not errors,
        tuple(dict.fromkeys(errors)),
        normalized,
        content_sha256(content) if not errors else None,
    )


def _validate_completion_receipt(
    value: Any,
    invocation: Mapping[str, Any],
    errors: list[str],
) -> None:
    if value is None:
        return
    receipt = _object(value, "Subloop result completion_receipt", errors)
    _exact_fields(
        receipt,
        COMPLETION_RECEIPT_FIELDS,
        "Subloop result completion_receipt",
        errors,
    )
    if receipt.get("task_ref") != invocation.get("completion_task_ref"):
        errors.append("Subloop result completion receipt names the wrong task")
    if not _valid_sha256(receipt.get("scenario_result_sha256")):
        errors.append(
            "Subloop result scenario_result_sha256 must be a lowercase SHA-256"
        )


def _validate_decision(value: Any, errors: list[str]) -> None:
    if value is None:
        return
    decision = _object(value, "Subloop result decision", errors)
    _exact_fields(decision, DECISION_FIELDS, "Subloop result decision", errors)
    _non_empty_string(
        decision.get("question"),
        "Subloop result decision question",
        errors,
    )
    _string_list(
        decision.get("options"),
        "Subloop result decision options",
        errors,
    )


def validate_result(
    value: Any,
    invocation: Mapping[str, Any],
    *,
    current_source_snapshot_sha256: str,
) -> ContractValidation:
    if not isinstance(value, dict):
        return ContractValidation(
            False,
            ("Subloop result must be an object",),
            {},
        )
    errors: list[str] = []
    _exact_fields(value, RESULT_FIELDS, "Subloop result", errors)
    if value.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"Subloop result schema_version must be {SCHEMA_VERSION}")
    if value.get("invocation_id") != invocation.get("invocation_id"):
        errors.append("Subloop result invocation_id does not match")
    expected_invocation_sha = content_sha256(canonical_json(invocation))
    if value.get("invocation_sha256") != expected_invocation_sha:
        errors.append("Subloop result invocation_sha256 does not match")
    if value.get("pack") != invocation.get("pack"):
        errors.append("Subloop result pack does not match")
    status = value.get("status")
    if status not in RESULT_STATUSES:
        errors.append(f"unsupported Subloop result status: {status}")
    _non_empty_string(value.get("summary"), "Subloop result summary", errors)
    finding_refs = _string_list(
        value.get("finding_refs"),
        "Subloop result finding_refs",
        errors,
        allow_empty=True,
    )
    changed_paths = _string_list(
        value.get("changed_paths"),
        "Subloop result changed_paths",
        errors,
        allow_empty=True,
    )
    for index, item in enumerate(changed_paths):
        _relative_path(item, f"Subloop result changed_paths[{index}]", errors)
    evidence_refs = _string_list(
        value.get("evidence_refs"),
        "Subloop result evidence_refs",
        errors,
        allow_empty=True,
    )

    permissions = invocation.get("permissions")
    if changed_paths and (
        not isinstance(permissions, list) or "modify-worktree" not in permissions
    ):
        errors.append("Subloop result has changes without modify-worktree permission")
    scope = invocation.get("scope")
    if (
        changed_paths
        and isinstance(scope, list)
        and not all(
            any(_path_is_within(path, boundary) for boundary in scope)
            for path in changed_paths
        )
    ):
        errors.append("Subloop result changed_paths exceed invocation scope")

    usage = _object(
        value.get("budget_usage"),
        "Subloop result budget_usage",
        errors,
    )
    _exact_fields(
        usage,
        BUDGET_USAGE_FIELDS,
        "Subloop result budget_usage",
        errors,
    )
    iterations_used = usage.get("iterations_used")
    limit = (
        invocation.get("budget", {}).get("iteration_limit")
        if isinstance(invocation.get("budget"), dict)
        else None
    )
    if (
        isinstance(iterations_used, bool)
        or not isinstance(iterations_used, int)
        or iterations_used < 0
    ):
        errors.append("Subloop result iterations_used must be non-negative")
    elif isinstance(limit, int) and iterations_used > limit:
        errors.append("Subloop result exceeds the allocated budget")

    _validate_completion_receipt(
        value.get("completion_receipt"),
        invocation,
        errors,
    )
    _validate_decision(value.get("decision"), errors)
    if not _valid_sha256(value.get("source_snapshot_after_sha256")):
        errors.append(
            "Subloop result source_snapshot_after_sha256 must be a lowercase SHA-256"
        )
    elif value.get("source_snapshot_after_sha256") != (current_source_snapshot_sha256):
        errors.append("Subloop result source snapshot is stale")

    if status == "changes-requested" and not finding_refs:
        errors.append("changes-requested requires at least one finding reference")
    if status == "needs-decision" and value.get("decision") is None:
        errors.append("needs-decision requires a decision")
    if status != "needs-decision" and value.get("decision") is not None:
        errors.append("only needs-decision may include a decision")
    if status == "blocked" and not (finding_refs or evidence_refs):
        errors.append("blocked requires finding or evidence references")
    if (
        status == "budget-exhausted"
        and isinstance(limit, int)
        and iterations_used != limit
    ):
        errors.append("budget-exhausted requires the full allocated budget")

    normalized = {field: value[field] for field in RESULT_FIELDS if field in value}
    content = canonical_json(normalized)
    return ContractValidation(
        not errors,
        tuple(dict.fromkeys(errors)),
        normalized,
        content_sha256(content) if not errors else None,
    )
