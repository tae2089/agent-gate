#!/usr/bin/env python3
"""Deterministic scenario-contract policy, execution, and completion gate.

Semantic authoring and review stay in skills.  This module accepts only strict
JSON artifacts and owns references, freshness, rollout policy, and normalized
runner evidence.
"""

from __future__ import annotations

import hashlib
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from readiness_gate import AC_PATTERN, INHERITANCE_FILENAME, P_REF_PATTERN, validate_task_dir

SCHEMA_VERSION = 1
CONFIG_RELATIVE = Path(".agent-gate/scenario-gate.json")
CONTRACT_FILENAME = "scenario-contract.json"
REVIEW_FILENAME = "scenario-review.json"
OVERLAY_FILENAME = "scenario-overlay.json"
RESULT_FILENAME = "scenario-result.json"
MODES = frozenset({"advisory", "critical-enforce", "enforce"})
RISKS = frozenset({"standard", "critical"})
LEVELS = frozenset({"in-process", "integration", "e2e"})
STANDARD_RUNNER_WARNING_THRESHOLD = 5
SAFE_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
SCENARIO_ID = re.compile(r"S-[A-Z0-9][A-Z0-9-]*")

CONFIG_FIELDS = frozenset({"schema_version", "mode", "runners"})
RUNNER_FIELDS = frozenset(
    {"command", "format", "timeout_seconds", "max_output_bytes"}
)
CONTRACT_FIELDS = frozenset({"schema_version", "scenarios"})
SCENARIO_FIELDS = frozenset(
    {"id", "title", "covers", "risk", "level", "runner", "given", "when", "then"}
)
COVERS_FIELDS = frozenset({"acceptance", "flow"})
OVERLAY_FIELDS = frozenset(
    {
        "schema_version",
        "parent_task",
        "parent_contract_sha256",
        "inherited_scenarios",
        "local_scenarios",
    }
)
LOCAL_SCENARIO_FIELDS = SCENARIO_FIELDS | {"ownership"}
OWNERSHIP = frozenset({"child", "parent-candidate"})
RESULT_FIELDS = frozenset(
    {
        "schema_version",
        "effective_scenarios_sha256",
        "runner_config_sha256",
        "source_fingerprint",
        "results",
    }
)
RESULT_ITEM_FIELDS = frozenset({"id", "status", "duration_ms", "reason"})
RESULT_STATUSES = frozenset({"passed", "failed", "infrastructure-error"})
SAFE_ENVIRONMENT = frozenset(
    {
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TMPDIR",
        "TEMP",
        "TMP",
        "SYSTEMROOT",
        "COMSPEC",
        "PATHEXT",
        "JAVA_HOME",
        "GOPATH",
        "GOMODCACHE",
        "CARGO_HOME",
        "RUSTUP_HOME",
    }
)
REVIEW_FIELDS = frozenset(
    {
        "schema_version",
        "task_sha256",
        "flow_sha256",
        "subject_sha256",
        "parent_contract_sha256",
        "runner_config_sha256",
        "reviewed_scenarios",
        "verdict",
        "blocking_findings",
    }
)


@dataclass(frozen=True)
class ScenarioGateResult:
    enabled: bool
    allowed: bool
    mode: str
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    required_scenarios: tuple[str, ...]


@dataclass(frozen=True)
class RunnerDefinition:
    command: tuple[str, ...]
    timeout_seconds: int
    max_output_bytes: int


@dataclass(frozen=True)
class ScenarioPolicy:
    mode: str
    runners: dict[str, RunnerDefinition]
    sha256: str


@dataclass(frozen=True)
class ScenarioRunResult:
    result_written: bool
    errors: tuple[str, ...]
    result_path: Path | None


@dataclass(frozen=True)
class _ResolvedScenarioSet:
    scenarios: tuple[dict[str, Any], ...]
    flow_path: Path
    subject_content: bytes | None
    parent_contract_sha256: str


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _canonical_sha256(value: Any) -> str:
    content = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256(content)


def _unknown_fields(value: dict[str, Any], allowed: frozenset[str], label: str, errors: list[str]) -> None:
    unknown = sorted(value.keys() - allowed)
    missing = sorted(allowed - value.keys())
    if unknown:
        errors.append(f"{label} has unknown fields: {', '.join(unknown)}")
    if missing:
        errors.append(f"{label} is missing fields: {', '.join(missing)}")


def _object(value: Any, label: str, errors: list[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        errors.append(f"{label} must be an object")
        return {}
    return value


def _load_json(path: Path, label: str, errors: list[str]) -> tuple[dict[str, Any], bytes] | None:
    if path.is_symlink():
        errors.append(f"{label} must not be a symlink")
        return None
    try:
        content = path.read_bytes()
        value = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        errors.append(f"cannot read {label}: {exc}")
        return None
    if not isinstance(value, dict):
        errors.append(f"{label} must contain a JSON object")
        return None
    return value, content


def _bounded_int(value: Any, default: int, minimum: int, maximum: int, label: str, errors: list[str]) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        errors.append(f"{label} must be an integer in [{minimum}, {maximum}]")
        return default
    return value


def load_policy(project_root: Path | str) -> tuple[ScenarioPolicy | None, tuple[str, ...]]:
    root = Path(project_root)
    path = root / CONFIG_RELATIVE
    if not path.exists() and not path.is_symlink():
        return None, ()
    errors: list[str] = []
    loaded = _load_json(path, str(CONFIG_RELATIVE), errors)
    if loaded is None:
        return None, tuple(errors)
    value, content = loaded
    _unknown_fields(value, CONFIG_FIELDS, "scenario configuration", errors)
    if value.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"scenario configuration schema_version must be {SCHEMA_VERSION}")
    mode = value.get("mode")
    if mode not in MODES:
        errors.append(f"scenario configuration mode must be one of {sorted(MODES)}")
        mode = "advisory"
    raw_runners = _object(value.get("runners"), "scenario configuration runners", errors)
    if not raw_runners:
        errors.append("scenario configuration runners must not be empty")
    runners: dict[str, RunnerDefinition] = {}
    for name, raw in raw_runners.items():
        if not isinstance(name, str) or SAFE_ID.fullmatch(name) is None:
            errors.append(f"runner name is unsafe: {name!r}")
            continue
        runner = _object(raw, f"runner {name}", errors)
        unknown = sorted(runner.keys() - RUNNER_FIELDS)
        missing = sorted({"command", "format"} - runner.keys())
        if unknown:
            errors.append(f"runner {name} has unknown fields: {', '.join(unknown)}")
        if missing:
            errors.append(f"runner {name} is missing fields: {', '.join(missing)}")
        command = runner.get("command")
        if (
            not isinstance(command, list)
            or not command
            or any(
                not isinstance(arg, str) or not arg or "\0" in arg for arg in command
            )
        ):
            errors.append(
                f"runner {name}.command must be a non-empty string array without NUL bytes"
            )
            command_value: tuple[str, ...] = ()
        else:
            command_value = tuple(command)
        if runner.get("format") != "exit-code":
            errors.append(f"runner {name}.format must be one of ['exit-code']")
        runners[name] = RunnerDefinition(
            command=command_value,
            timeout_seconds=_bounded_int(
                runner.get("timeout_seconds"), 300, 1, 3600, f"runner {name}.timeout_seconds", errors
            ),
            max_output_bytes=_bounded_int(
                runner.get("max_output_bytes"),
                1_048_576,
                1024,
                10_485_760,
                f"runner {name}.max_output_bytes",
                errors,
            ),
        )
    if errors:
        return None, tuple(errors)
    return ScenarioPolicy(mode=mode, runners=runners, sha256=_sha256(content)), ()


def _string_list(value: Any, label: str, errors: list[str], pattern: re.Pattern[str] | None = None) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        errors.append(f"{label} must be a non-empty list")
        return ()
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or item != item.strip():
            errors.append(f"{label} contains an invalid string")
            continue
        if pattern is not None and pattern.fullmatch(item) is None:
            errors.append(f"{label} contains an invalid reference: {item}")
            continue
        result.append(item)
    if len(result) != len(set(result)):
        errors.append(f"{label} contains duplicate values")
    return tuple(dict.fromkeys(result))


def _validate_scenario(
    raw: Any,
    index: int,
    runners: dict[str, RunnerDefinition],
    errors: list[str],
    *,
    local: bool = False,
) -> dict[str, Any]:
    label = f"scenario[{index}]"
    scenario = _object(raw, label, errors)
    _unknown_fields(scenario, LOCAL_SCENARIO_FIELDS if local else SCENARIO_FIELDS, label, errors)
    scenario_id = scenario.get("id")
    if not isinstance(scenario_id, str) or SCENARIO_ID.fullmatch(scenario_id) is None:
        errors.append(f"{label}.id must match {SCENARIO_ID.pattern}")
    title = scenario.get("title")
    if not isinstance(title, str) or not title.strip():
        errors.append(f"{label}.title must be a non-empty string")
    covers = _object(scenario.get("covers"), f"{label}.covers", errors)
    _unknown_fields(covers, COVERS_FIELDS, f"{label}.covers", errors)
    _string_list(covers.get("acceptance"), f"{label}.covers.acceptance", errors, AC_PATTERN)
    _string_list(covers.get("flow"), f"{label}.covers.flow", errors, P_REF_PATTERN)
    if scenario.get("risk") not in RISKS:
        errors.append(f"{label}.risk must be one of {sorted(RISKS)}")
    if scenario.get("level") not in LEVELS:
        errors.append(f"{label}.level must be one of {sorted(LEVELS)}")
    runner = scenario.get("runner")
    if not isinstance(runner, str) or runner not in runners:
        errors.append(f"{label}.runner does not name a configured runner")
    for key in ("given", "when", "then"):
        _string_list(scenario.get(key), f"{label}.{key}", errors)
    if local and scenario.get("ownership") not in OWNERSHIP:
        errors.append(f"{label}.ownership must be one of {sorted(OWNERSHIP)}")
    return scenario


def _parent_contract(
    task_dir: Path, policy: ScenarioPolicy, errors: list[str]
) -> tuple[tuple[dict[str, Any], ...], bytes | None]:
    loaded = _load_json(task_dir / CONTRACT_FILENAME, CONTRACT_FILENAME, errors)
    if loaded is None:
        return (), None
    value, content = loaded
    _unknown_fields(value, CONTRACT_FIELDS, "scenario contract", errors)
    if value.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"scenario contract schema_version must be {SCHEMA_VERSION}")
    raw_scenarios = value.get("scenarios")
    if not isinstance(raw_scenarios, list) or not raw_scenarios:
        errors.append("scenario contract scenarios must be a non-empty list")
        return (), content
    scenarios = tuple(
        _validate_scenario(raw, index, policy.runners, errors)
        for index, raw in enumerate(raw_scenarios)
    )
    ids = [scenario.get("id") for scenario in scenarios if isinstance(scenario.get("id"), str)]
    duplicates = sorted({item for item in ids if ids.count(item) > 1})
    if duplicates:
        errors.append(f"duplicate scenario id: {', '.join(duplicates)}")

    try:
        task_text = (task_dir / "task.md").read_text(encoding="utf-8")
        flow_text = (task_dir / "implementation.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        errors.append(f"cannot read parent flow artifacts: {exc}")
        return scenarios, content
    task_refs = set(AC_PATTERN.findall(task_text))
    flow_refs = set(P_REF_PATTERN.findall(flow_text))
    covered_acceptance: set[str] = set()
    for scenario in scenarios:
        covers = scenario.get("covers")
        if not isinstance(covers, dict):
            continue
        for ref in covers.get("acceptance", []):
            if isinstance(ref, str):
                covered_acceptance.add(ref)
                if ref not in task_refs:
                    errors.append(f"acceptance reference {ref} is missing from task.md")
        for ref in covers.get("flow", []):
            if isinstance(ref, str) and ref not in flow_refs:
                errors.append(f"flow reference {ref} is missing from implementation.md")
    missing = sorted(task_refs - covered_acceptance)
    if missing:
        errors.append(f"missing acceptance coverage: {', '.join(missing)}")
    return scenarios, content


def _validate_review(
    task_dir: Path,
    flow_path: Path,
    subject_content: bytes | None,
    scenarios: tuple[dict[str, Any], ...],
    parent_contract_sha256: str,
    runner_config_sha256: str,
    errors: list[str],
) -> None:
    loaded = _load_json(task_dir / REVIEW_FILENAME, REVIEW_FILENAME, errors)
    if loaded is None:
        return
    review, _ = loaded
    _unknown_fields(review, REVIEW_FIELDS, "scenario review", errors)
    if review.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"scenario review schema_version must be {SCHEMA_VERSION}")

    for field, path in (
        ("task_sha256", task_dir / "task.md"),
        ("flow_sha256", flow_path),
    ):
        try:
            actual = _sha256(path.read_bytes())
        except OSError as exc:
            errors.append(f"cannot read {path.name} for scenario review: {exc}")
            continue
        if review.get(field) != actual:
            errors.append(f"scenario review {field} is stale")

    if subject_content is None or review.get("subject_sha256") != _sha256(subject_content):
        errors.append("scenario review subject_sha256 is stale")
    if review.get("parent_contract_sha256") != parent_contract_sha256:
        if parent_contract_sha256:
            errors.append("scenario review parent_contract_sha256 is stale")
        else:
            errors.append("scenario review parent_contract_sha256 must be empty for a parent task")
    if review.get("runner_config_sha256") != runner_config_sha256:
        errors.append("scenario review runner_config_sha256 is stale")

    reviewed = _string_list(
        review.get("reviewed_scenarios"),
        "scenario review reviewed_scenarios",
        errors,
        SCENARIO_ID,
    )
    expected = tuple(
        scenario["id"] for scenario in scenarios if isinstance(scenario.get("id"), str)
    )
    if reviewed != expected:
        errors.append("scenario review reviewed_scenarios must exactly match effective scenarios")
    if review.get("verdict") != "pass":
        errors.append("scenario review verdict must be 'pass'")
    findings = review.get("blocking_findings")
    if not isinstance(findings, list):
        errors.append("scenario review blocking_findings must be a list")
    elif findings:
        errors.append("scenario review blocking_findings must be empty")


def _scenario_scope_errors(
    scenario: dict[str, Any],
    acceptance_scope: set[str],
    flow_scope: set[str],
    label: str,
    errors: list[str],
) -> None:
    covers = scenario.get("covers")
    if not isinstance(covers, dict):
        return
    acceptance_refs = {
        ref for ref in covers.get("acceptance", []) if isinstance(ref, str)
    }
    flow_refs = {ref for ref in covers.get("flow", []) if isinstance(ref, str)}
    outside_acceptance = sorted(acceptance_refs - acceptance_scope)
    outside_flow = sorted(flow_refs - flow_scope)
    if outside_acceptance:
        errors.append(
            f"{label} is outside child acceptance scope: {', '.join(outside_acceptance)}"
        )
    if outside_flow:
        errors.append(f"{label} is outside child flow scope: {', '.join(outside_flow)}")


def _child_scenarios(
    child: Path,
    policy: ScenarioPolicy,
    errors: list[str],
    *,
    validate_subject_review: bool = True,
) -> _ResolvedScenarioSet | None:
    inheritance_loaded = _load_json(
        child / INHERITANCE_FILENAME, INHERITANCE_FILENAME, errors
    )
    if inheritance_loaded is None:
        return None
    inheritance, _ = inheritance_loaded
    parent_name = inheritance.get("parent_task")
    if (
        not isinstance(parent_name, str)
        or Path(parent_name).parts != (parent_name,)
        or parent_name.startswith(".")
    ):
        errors.append("child scenario parent_task is unsafe")
        return None
    parent = child.parent / parent_name
    try:
        if parent.is_symlink() or not parent.resolve(strict=True).is_dir():
            raise OSError("not a direct directory")
    except (OSError, RuntimeError) as exc:
        errors.append(f"cannot resolve child scenario parent: {exc}")
        return None

    parent_scenarios, parent_content = _parent_contract(parent, policy, errors)
    _validate_review(
        parent,
        parent / "implementation.md",
        parent_content,
        parent_scenarios,
        "",
        policy.sha256,
        errors,
    )
    overlay_loaded = _load_json(child / OVERLAY_FILENAME, OVERLAY_FILENAME, errors)
    if overlay_loaded is None:
        return None
    overlay, overlay_content = overlay_loaded
    _unknown_fields(overlay, OVERLAY_FIELDS, "scenario overlay", errors)
    if overlay.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"scenario overlay schema_version must be {SCHEMA_VERSION}")
    if overlay.get("parent_task") != parent_name:
        errors.append("scenario overlay parent_task must match inherited readiness")
    parent_sha256 = _sha256(parent_content) if parent_content is not None else ""
    if overlay.get("parent_contract_sha256") != parent_sha256:
        errors.append("scenario overlay parent_contract_sha256 is stale")

    inherited_ids = _string_list(
        overlay.get("inherited_scenarios"),
        "scenario overlay inherited_scenarios",
        errors,
        SCENARIO_ID,
    )
    parent_by_id = {
        scenario["id"]: scenario
        for scenario in parent_scenarios
        if isinstance(scenario.get("id"), str)
    }
    acceptance_scope = set(inheritance.get("acceptance_refs", []))
    flow_scope = set(inheritance.get("flow_refs", []))
    effective: list[dict[str, Any]] = []
    for scenario_id in inherited_ids:
        scenario = parent_by_id.get(scenario_id)
        if scenario is None:
            errors.append(f"inherited scenario {scenario_id} is missing from parent contract")
            continue
        _scenario_scope_errors(
            scenario, acceptance_scope, flow_scope, f"inherited scenario {scenario_id}", errors
        )
        effective.append(scenario)

    raw_local = overlay.get("local_scenarios")
    if not isinstance(raw_local, list):
        errors.append("scenario overlay local_scenarios must be a list")
        raw_local = []
    local_scenarios = tuple(
        _validate_scenario(raw, index, policy.runners, errors, local=True)
        for index, raw in enumerate(raw_local)
    )
    for scenario in local_scenarios:
        scenario_id = scenario.get("id")
        _scenario_scope_errors(
            scenario,
            acceptance_scope,
            flow_scope,
            f"local scenario {scenario_id}",
            errors,
        )
        effective.append(scenario)
    effective_ids = [
        scenario["id"] for scenario in effective if isinstance(scenario.get("id"), str)
    ]
    duplicate_ids = sorted({item for item in effective_ids if effective_ids.count(item) > 1})
    if duplicate_ids:
        errors.append(f"duplicate effective scenario id: {', '.join(duplicate_ids)}")

    if validate_subject_review:
        _validate_review(
            child,
            parent / "implementation.md",
            overlay_content,
            tuple(effective),
            parent_sha256,
            policy.sha256,
            errors,
        )
    return _ResolvedScenarioSet(
        scenarios=tuple(effective),
        flow_path=parent / "implementation.md",
        subject_content=overlay_content,
        parent_contract_sha256=parent_sha256,
    )


def _runner_group_findings(
    scenarios: tuple[dict[str, Any], ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    errors: list[str] = []
    warnings: list[str] = []
    for scenario in scenarios:
        runner = scenario.get("runner")
        if isinstance(runner, str):
            grouped.setdefault(runner, []).append(scenario)
    for runner, assigned in grouped.items():
        if len(assigned) > 1 and any(
            scenario.get("risk") == "critical" for scenario in assigned
        ):
            scenario_ids = sorted(
                scenario["id"]
                for scenario in assigned
                if isinstance(scenario.get("id"), str)
            )
            errors.append(
                f"critical runner {runner} must be exclusive; assigned scenarios: "
                + ", ".join(scenario_ids)
            )
        elif len(assigned) > STANDARD_RUNNER_WARNING_THRESHOLD:
            warnings.append(
                f"standard runner {runner} covers {len(assigned)} scenarios; "
                "review whether it should be split by observable flow"
            )
    return tuple(errors), tuple(warnings)


def _resolve_scenario_set(
    task: Path,
    policy: ScenarioPolicy,
    errors: list[str],
    *,
    validate_review_artifact: bool = True,
) -> _ResolvedScenarioSet:
    inherited = task / INHERITANCE_FILENAME
    if inherited.is_symlink() or inherited.exists():
        resolved = _child_scenarios(
            task,
            policy,
            errors,
            validate_subject_review=validate_review_artifact,
        )
        if resolved is not None:
            errors.extend(_runner_group_findings(resolved.scenarios)[0])
            return resolved
        return _ResolvedScenarioSet((), task / "implementation.md", None, "")
    scenarios, contract_content = _parent_contract(task, policy, errors)
    if validate_review_artifact:
        _validate_review(
            task,
            task / "implementation.md",
            contract_content,
            scenarios,
            "",
            policy.sha256,
            errors,
        )
    resolved = _ResolvedScenarioSet(
        scenarios=scenarios,
        flow_path=task / "implementation.md",
        subject_content=contract_content,
        parent_contract_sha256="",
    )
    errors.extend(_runner_group_findings(resolved.scenarios)[0])
    return resolved


def _project_task_dir(
    project_root: Path, task_dir: Path, errors: list[str]
) -> Path | None:
    try:
        root = project_root.resolve(strict=True)
        if task_dir.is_symlink() or task_dir.parent.is_symlink():
            errors.append("scenario task must not be a symlink")
            return None
        task = task_dir.resolve(strict=True)
        relative = task.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        errors.append(f"scenario task must be inside the project _workspace: {exc}")
        return None
    if (
        len(relative.parts) != 2
        or relative.parts[0] != "_workspace"
        or not relative.parts[1]
        or relative.parts[1].startswith(".")
        or not task.is_dir()
    ):
        errors.append("scenario task must be a direct _workspace/<task> directory")
        return None
    return task


def validate_readiness(task_dir: Path | str, project_root: Path | str) -> ScenarioGateResult:
    root = Path(project_root)
    policy, policy_errors = load_policy(root)
    if policy is None and not policy_errors:
        return ScenarioGateResult(False, True, "disabled", (), (), ())
    if policy is None:
        return ScenarioGateResult(True, False, "invalid", policy_errors, (), ())

    errors: list[str] = []
    task = _project_task_dir(root, Path(task_dir), errors)
    if task is None:
        if policy.mode == "advisory":
            return ScenarioGateResult(True, True, policy.mode, (), tuple(errors), ())
        return ScenarioGateResult(True, False, policy.mode, tuple(errors), (), ())
    readiness = validate_task_dir(task)
    if not readiness.ready:
        errors.extend(f"task readiness: {error}" for error in readiness.errors)
    scenarios = _resolve_scenario_set(task, policy, errors).scenarios
    warnings = list(_runner_group_findings(scenarios)[1])
    all_ids = tuple(
        scenario["id"] for scenario in scenarios if isinstance(scenario.get("id"), str)
    )
    required = (
        tuple(
            scenario["id"]
            for scenario in scenarios
            if scenario.get("risk") == "critical" and isinstance(scenario.get("id"), str)
        )
        if policy.mode == "critical-enforce"
        else all_ids
    )
    if policy.mode == "advisory":
        return ScenarioGateResult(
            True, True, policy.mode, (), tuple(errors + warnings), all_ids
        )
    return ScenarioGateResult(
        True,
        not errors,
        policy.mode,
        tuple(errors),
        tuple(warnings),
        required,
    )


def _infrastructure_results(
    scenario_ids: tuple[str, ...], reason: str, duration_ms: int
) -> list[dict[str, Any]]:
    return [
        {
            "id": scenario_id,
            "status": "infrastructure-error",
            "duration_ms": duration_ms,
            "reason": reason,
        }
        for scenario_id in scenario_ids
    ]


def _kill_runner(process: subprocess.Popen[Any]) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            process.kill()
    else:
        process.kill()
    process.wait()


def _stored_results(
    raw: Any,
    effective_ids: tuple[str, ...],
    errors: list[str],
) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, list):
        errors.append("scenario result results must be a list")
        return {}
    by_id: dict[str, dict[str, Any]] = {}
    for index, value in enumerate(raw):
        item = _object(value, f"scenario result[{index}]", errors)
        unknown = sorted(item.keys() - RESULT_ITEM_FIELDS)
        missing = sorted({"id", "status", "duration_ms"} - item.keys())
        if unknown:
            errors.append(f"scenario result[{index}] has unknown fields: {', '.join(unknown)}")
        if missing:
            errors.append(f"scenario result[{index}] is missing fields: {', '.join(missing)}")
        scenario_id = item.get("id")
        if not isinstance(scenario_id, str) or scenario_id not in effective_ids:
            errors.append(f"scenario result[{index}].id is not an effective scenario")
            continue
        if scenario_id in by_id:
            errors.append(f"scenario result has duplicate scenario id: {scenario_id}")
            continue
        status = item.get("status")
        if status not in RESULT_STATUSES:
            errors.append(f"scenario result {scenario_id}.status is invalid")
        duration = item.get("duration_ms")
        if isinstance(duration, bool) or not isinstance(duration, int) or duration < 0:
            errors.append(f"scenario result {scenario_id}.duration_ms must be non-negative")
        reason = item.get("reason")
        if reason is not None and not isinstance(reason, str):
            errors.append(f"scenario result {scenario_id}.reason must be a string")
        normalized = {
            "id": scenario_id,
            "status": status,
            "duration_ms": duration,
        }
        if isinstance(reason, str) and reason:
            normalized["reason"] = reason
        by_id[scenario_id] = normalized
    return by_id


def _execute_runner(
    root: Path,
    definition: RunnerDefinition,
    scenario_ids: tuple[str, ...],
) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
    environment = {key: value for key, value in os.environ.items() if key in SAFE_ENVIRONMENT}
    started = time.monotonic()
    timed_out = False
    output_exceeded = False
    launch_error = None
    return_code = 1
    with tempfile.TemporaryFile() as output:
        try:
            process = subprocess.Popen(
                definition.command,
                cwd=root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=output,
                shell=False,
                start_new_session=os.name == "posix",
            )
            deadline = started + definition.timeout_seconds
            while True:
                polled = process.poll()
                if polled is not None:
                    return_code = polled
                    break
                if os.fstat(output.fileno()).st_size > definition.max_output_bytes:
                    output_exceeded = True
                    _kill_runner(process)
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    _kill_runner(process)
                    break
                time.sleep(min(0.05, remaining))
        except OSError as exc:
            launch_error = exc
        duration_ms = max(0, round((time.monotonic() - started) * 1000))
        output.seek(0, os.SEEK_END)
        output_size = output.tell()

    if launch_error is not None:
        reason = f"runner launch failed: {launch_error}"
        return _infrastructure_results(scenario_ids, reason, duration_ms), (reason,)
    if timed_out:
        reason = f"runner timed out after {definition.timeout_seconds}s"
        return _infrastructure_results(scenario_ids, reason, duration_ms), (reason,)
    if output_exceeded or output_size > definition.max_output_bytes:
        reason = f"runner output exceeded {definition.max_output_bytes} bytes"
        return _infrastructure_results(scenario_ids, reason, duration_ms), (reason,)
    status = "passed" if return_code == 0 else "failed"
    results = [
        {"id": scenario_id, "status": status, "duration_ms": duration_ms}
        for scenario_id in scenario_ids
    ]
    if return_code != 0:
        for result in results:
            result["reason"] = f"runner exited with code {return_code}"
    return results, ()


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    if path.is_symlink():
        raise OSError(f"refusing to replace symlink: {path.name}")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
            temporary = Path(stream.name)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _git_output(root: Path, arguments: tuple[str, ...]) -> tuple[bytes | None, str | None]:
    max_bytes = 52_428_800
    with tempfile.TemporaryFile() as output:
        try:
            process = subprocess.Popen(
                ("git", "-C", str(root), *arguments),
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=output,
                shell=False,
            )
            try:
                return_code = process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                return None, f"git {' '.join(arguments)} timed out"
        except OSError as exc:
            return None, f"cannot execute git: {exc}"
        output.seek(0, os.SEEK_END)
        size = output.tell()
        if size > max_bytes:
            return None, f"git {' '.join(arguments)} output exceeded {max_bytes} bytes"
        output.seek(0)
        content = output.read()
    if return_code != 0:
        return None, f"git {' '.join(arguments)} failed with code {return_code}"
    return content, None


def _source_fingerprint(root: Path) -> tuple[str | None, tuple[str, ...]]:
    errors: list[str] = []
    head, error = _git_output(root, ("rev-parse", "HEAD"))
    if error:
        errors.append(error)
    tracked_diff, error = _git_output(
        root, ("diff", "--binary", "--no-ext-diff", "HEAD", "--")
    )
    if error:
        errors.append(error)
    untracked, error = _git_output(
        root, ("ls-files", "--others", "--exclude-standard", "-z")
    )
    if error:
        errors.append(error)
    if errors or head is None or tracked_diff is None or untracked is None:
        return None, tuple(errors)

    digest = hashlib.sha256()
    digest.update(b"HEAD\0")
    digest.update(head)
    digest.update(b"\0DIFF\0")
    digest.update(tracked_diff)
    digest.update(b"\0UNTRACKED\0")
    for raw_relative in sorted(item for item in untracked.split(b"\0") if item):
        relative_text = os.fsdecode(raw_relative)
        relative = Path(relative_text)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or relative.parts[:1] in ((".git",), ("_workspace",))
        ):
            continue
        path = root / relative
        try:
            stat = path.lstat()
        except OSError as exc:
            errors.append(f"cannot stat untracked path {relative.as_posix()}: {exc}")
            continue
        digest.update(raw_relative)
        digest.update(b"\0")
        digest.update(
            f"{stat.st_mode}:{stat.st_size}:{stat.st_mtime_ns}".encode("ascii")
        )
        digest.update(b"\0")
    if errors:
        return None, tuple(errors)
    return digest.hexdigest(), ()


def run_scenarios(
    task_dir: Path | str,
    project_root: Path | str,
    requested_ids: tuple[str, ...] | None = None,
) -> ScenarioRunResult:
    root = Path(project_root).resolve(strict=True)
    policy, policy_errors = load_policy(root)
    if policy is None:
        errors = policy_errors or ("scenario gate is disabled",)
        return ScenarioRunResult(False, tuple(errors), None)
    errors: list[str] = []
    task = _project_task_dir(root, Path(task_dir), errors)
    if task is None:
        return ScenarioRunResult(False, tuple(errors), None)
    readiness = validate_task_dir(task)
    if not readiness.ready:
        errors.extend(f"task readiness: {error}" for error in readiness.errors)
    scenarios = _resolve_scenario_set(task, policy, errors).scenarios
    by_id = {
        scenario["id"]: scenario
        for scenario in scenarios
        if isinstance(scenario.get("id"), str)
    }
    if requested_ids is None:
        selected_ids = tuple(by_id)
    else:
        selected_ids = tuple(dict.fromkeys(requested_ids))
        unknown = sorted(set(selected_ids) - by_id.keys())
        if unknown:
            errors.append(f"requested scenario ids are unknown: {', '.join(unknown)}")
    if errors:
        return ScenarioRunResult(False, tuple(errors), None)

    grouped: dict[str, list[str]] = {}
    for scenario_id in selected_ids:
        runner = by_id[scenario_id]["runner"]
        grouped.setdefault(runner, []).append(scenario_id)
    normalized_results: dict[str, dict[str, Any]] = {}
    execution_errors: list[str] = []
    for runner_name, ids in grouped.items():
        results, runner_errors = _execute_runner(
            root, policy.runners[runner_name], tuple(ids)
        )
        execution_errors.extend(f"runner {runner_name}: {error}" for error in runner_errors)
        normalized_results.update({item["id"]: item for item in results})

    source_fingerprint, fingerprint_errors = _source_fingerprint(root)
    if source_fingerprint is None:
        return ScenarioRunResult(False, tuple(execution_errors) + fingerprint_errors, None)

    result_value = {
        "schema_version": SCHEMA_VERSION,
        "effective_scenarios_sha256": _canonical_sha256(list(scenarios)),
        "runner_config_sha256": policy.sha256,
        "source_fingerprint": source_fingerprint,
        "results": [normalized_results[scenario_id] for scenario_id in selected_ids],
    }
    result_path = task / RESULT_FILENAME
    try:
        _atomic_write_json(result_path, result_value)
    except OSError as exc:
        return ScenarioRunResult(
            False,
            tuple(execution_errors) + (f"cannot write {RESULT_FILENAME}: {exc}",),
            None,
        )
    return ScenarioRunResult(True, tuple(execution_errors), result_path)


def validate_completion(
    task_dir: Path | str, project_root: Path | str
) -> ScenarioGateResult:
    root = Path(project_root)
    policy, policy_errors = load_policy(root)
    if policy is None and not policy_errors:
        return ScenarioGateResult(False, True, "disabled", (), (), ())
    if policy is None:
        return ScenarioGateResult(True, False, "invalid", policy_errors, (), ())

    errors: list[str] = []
    warnings: list[str] = []
    task = _project_task_dir(root, Path(task_dir), errors)
    if task is None:
        if policy.mode == "advisory":
            return ScenarioGateResult(True, True, policy.mode, (), tuple(errors), ())
        return ScenarioGateResult(True, False, policy.mode, tuple(errors), (), ())
    readiness = validate_task_dir(task)
    if not readiness.ready:
        errors.extend(f"task readiness: {error}" for error in readiness.errors)
    scenarios = _resolve_scenario_set(task, policy, errors).scenarios
    warnings.extend(_runner_group_findings(scenarios)[1])
    effective_ids = tuple(
        scenario["id"] for scenario in scenarios if isinstance(scenario.get("id"), str)
    )
    critical_ids = tuple(
        scenario["id"]
        for scenario in scenarios
        if scenario.get("risk") == "critical" and isinstance(scenario.get("id"), str)
    )
    required_ids = critical_ids if policy.mode == "critical-enforce" else effective_ids

    loaded = _load_json(task / RESULT_FILENAME, RESULT_FILENAME, errors)
    by_id: dict[str, dict[str, Any]] = {}
    if loaded is not None:
        result, _ = loaded
        _unknown_fields(result, RESULT_FIELDS, "scenario result", errors)
        if result.get("schema_version") != SCHEMA_VERSION:
            errors.append(f"scenario result schema_version must be {SCHEMA_VERSION}")
        if result.get("effective_scenarios_sha256") != _canonical_sha256(list(scenarios)):
            errors.append("scenario result effective_scenarios_sha256 is stale")
        if result.get("runner_config_sha256") != policy.sha256:
            errors.append("scenario result runner_config_sha256 is stale")
        fingerprint, fingerprint_errors = _source_fingerprint(root.resolve(strict=True))
        errors.extend(fingerprint_errors)
        if fingerprint is None or result.get("source_fingerprint") != fingerprint:
            errors.append("scenario result source_fingerprint is stale")
        by_id = _stored_results(result.get("results"), effective_ids, errors)

    for scenario_id in required_ids:
        item = by_id.get(scenario_id)
        if item is None:
            errors.append(f"required scenario result is missing: {scenario_id}")
        elif item.get("status") != "passed":
            errors.append(
                f"required scenario {scenario_id} did not pass: {item.get('status')}"
            )
    if policy.mode == "critical-enforce":
        for scenario_id in set(effective_ids) - set(required_ids):
            item = by_id.get(scenario_id)
            if item is None:
                warnings.append(f"standard scenario result is missing: {scenario_id}")
            elif item.get("status") != "passed":
                warnings.append(
                    f"standard scenario {scenario_id} did not pass: {item.get('status')}"
                )
    if policy.mode == "enforce":
        candidates = [
            scenario["id"]
            for scenario in scenarios
            if scenario.get("ownership") == "parent-candidate"
            and isinstance(scenario.get("id"), str)
        ]
        if candidates:
            errors.append(
                "unresolved parent-candidate scenarios: " + ", ".join(candidates)
            )

    if policy.mode == "advisory":
        return ScenarioGateResult(
            True,
            True,
            policy.mode,
            (),
            tuple(errors + warnings),
            effective_ids,
        )
    return ScenarioGateResult(
        True,
        not errors,
        policy.mode,
        tuple(errors),
        tuple(warnings),
        required_ids,
    )


def review_template(task_dir: Path | str, project_root: Path | str) -> dict[str, Any]:
    root = Path(project_root)
    policy, policy_errors = load_policy(root)
    if policy is None:
        raise ValueError("; ".join(policy_errors) or "scenario gate is disabled")
    errors: list[str] = []
    task = _project_task_dir(root, Path(task_dir), errors)
    if task is None:
        raise ValueError("; ".join(errors))
    readiness = validate_task_dir(task)
    if not readiness.ready:
        errors.extend(f"task readiness: {error}" for error in readiness.errors)
    resolved = _resolve_scenario_set(
        task, policy, errors, validate_review_artifact=False
    )
    if errors:
        raise ValueError("; ".join(errors))
    try:
        task_sha256 = _sha256((task / "task.md").read_bytes())
        flow_sha256 = _sha256(resolved.flow_path.read_bytes())
        if resolved.subject_content is None:
            raise OSError("scenario subject is unavailable")
        subject_sha256 = _sha256(resolved.subject_content)
    except OSError as exc:
        errors.append(f"cannot create scenario review template: {exc}")
    if errors:
        raise ValueError("; ".join(errors))
    return {
        "schema_version": SCHEMA_VERSION,
        "task_sha256": task_sha256,
        "flow_sha256": flow_sha256,
        "subject_sha256": subject_sha256,
        "parent_contract_sha256": resolved.parent_contract_sha256,
        "runner_config_sha256": policy.sha256,
        "reviewed_scenarios": [
            scenario["id"]
            for scenario in resolved.scenarios
            if isinstance(scenario.get("id"), str)
        ],
        "verdict": "revise",
        "blocking_findings": ["independent scenario review has not run"],
    }


def _gate_json(result: ScenarioGateResult) -> dict[str, Any]:
    return {
        "enabled": result.enabled,
        "allowed": result.allowed,
        "mode": result.mode,
        "errors": list(result.errors),
        "warnings": list(result.warnings),
        "required_scenarios": list(result.required_scenarios),
    }


def _print_gate(result: ScenarioGateResult, as_json: bool) -> None:
    if as_json:
        print(json.dumps(_gate_json(result), ensure_ascii=False, sort_keys=True))
        return
    label = "PASS" if result.allowed else "BLOCK"
    print(f"{label} mode={result.mode}")
    for error in result.errors:
        print(f"  error: {error}")
    for warning in result.warnings:
        print(f"  warning: {warning}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    for name in ("readiness", "run", "completion", "review-template"):
        command = subparsers.add_parser(name)
        command.add_argument("task_dir", type=Path)
        command.add_argument("--project-root", required=True, type=Path)
        command.add_argument("--json", action="store_true")
        if name == "run":
            command.add_argument("--scenario", action="append", default=[])
    args = parser.parse_args()

    if args.operation == "review-template":
        try:
            template = review_template(args.task_dir, args.project_root)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(template, ensure_ascii=False, indent=2))
        return 0
    if args.operation == "readiness":
        result = validate_readiness(args.task_dir, args.project_root)
        _print_gate(result, args.json)
        return 0 if result.allowed else 1
    if args.operation == "completion":
        result = validate_completion(args.task_dir, args.project_root)
        _print_gate(result, args.json)
        return 0 if result.allowed else 1

    requested = tuple(args.scenario) if args.scenario else None
    run = run_scenarios(args.task_dir, args.project_root, requested)
    if not run.result_written:
        value = {"result_written": False, "errors": list(run.errors)}
        if args.json:
            print(json.dumps(value, ensure_ascii=False, sort_keys=True))
        else:
            print("RUNNER_ERROR")
            for error in run.errors:
                print(f"  error: {error}")
        return 1
    completion = validate_completion(args.task_dir, args.project_root)
    if args.json:
        print(
            json.dumps(
                {
                    "result_written": True,
                    "errors": list(run.errors),
                    "completion": _gate_json(completion),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        print("RUN_COMPLETE")
        _print_gate(completion, False)
    return 0 if completion.allowed else 1


if __name__ == "__main__":
    sys.exit(main())
