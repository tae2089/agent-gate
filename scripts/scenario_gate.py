#!/usr/bin/env python3
"""Deterministic observable-scenario evidence and completion gate.

Semantic authoring and evidence judgment stay in skills. This module owns
strict artifact validation, trusted command execution, freshness, transparent
evidence coverage, and the Full-parent completion boundary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from readiness_gate import AC_PATTERN, INHERITANCE_FILENAME, P_REF_PATTERN, validate_task_dir

SCHEMA_VERSION = 1
CONFIG_RELATIVE = Path(".agent-gate/scenario-gate.json")
CONTRACT_FILENAME = "scenario-contract.json"
EVIDENCE_FILENAME = "scenario-evidence.json"
RESULT_FILENAME = "scenario-result.json"
OBSOLETE_CHILD_FILENAMES = (
    "scenario-overlay.json",
    CONTRACT_FILENAME,
    EVIDENCE_FILENAME,
    RESULT_FILENAME,
)

SAFE_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
SCENARIO_ID = re.compile(r"S-[A-Z0-9][A-Z0-9-]*")
OBSERVATION_ID = re.compile(r"O-[A-Z0-9][A-Z0-9-]*")

CONFIG_FIELDS = frozenset({"schema_version", "runners"})
RUNNER_FIELDS = frozenset({"command", "timeout_seconds", "max_output_bytes"})
CONTRACT_FIELDS = frozenset({"schema_version", "scenarios"})
SCENARIO_FIELDS = frozenset(
    {"id", "title", "covers", "runner", "given", "when", "then"}
)
COVERS_FIELDS = frozenset({"acceptance", "flow"})
OBSERVATION_FIELDS = frozenset({"id", "expectation"})
EVIDENCE_FIELDS = frozenset(
    {
        "schema_version",
        "task_sha256",
        "flow_sha256",
        "contract_sha256",
        "source_fingerprint",
        "observations",
        "verdict",
        "blocking_findings",
    }
)
EVIDENCE_ITEM_FIELDS = frozenset({"id", "implementation", "verification"})
LOCATION_FIELDS = frozenset({"path", "line"})
RESULT_FIELDS = frozenset(
    {
        "schema_version",
        "contract_sha256",
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


@dataclass(frozen=True)
class ScenarioCoverage:
    required: int
    implementation_mapped: int
    verification_mapped: int
    execution_passed: int
    verified: int
    percentage: float


@dataclass(frozen=True)
class ScenarioGateResult:
    enabled: bool
    allowed: bool
    errors: tuple[str, ...]
    required_scenarios: tuple[str, ...]
    coverage: ScenarioCoverage | None = None


@dataclass(frozen=True)
class RunnerDefinition:
    command: tuple[str, ...]
    timeout_seconds: int
    max_output_bytes: int


@dataclass(frozen=True)
class ScenarioPolicy:
    runners: dict[str, RunnerDefinition]
    sha256: str


@dataclass(frozen=True)
class ScenarioRunResult:
    result_written: bool
    errors: tuple[str, ...]
    result_path: Path | None


@dataclass(frozen=True)
class _ResolvedScenarioSet:
    requested_task: Path
    owner_task: Path
    flow_path: Path
    contract_content: bytes
    scenarios: tuple[dict[str, Any], ...]


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _fields(
    value: dict[str, Any],
    required: frozenset[str],
    allowed: frozenset[str],
    label: str,
    errors: list[str],
) -> bool:
    unknown = sorted(value.keys() - allowed)
    missing = sorted(required - value.keys())
    if unknown:
        errors.append(f"{label} has unknown fields: {', '.join(unknown)}")
    if missing:
        errors.append(f"{label} is missing fields: {', '.join(missing)}")
    return not unknown and not missing


def _object(value: Any, label: str, errors: list[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        errors.append(f"{label} must be an object")
        return {}
    return value


def _load_json(
    path: Path, label: str, errors: list[str]
) -> tuple[dict[str, Any], bytes] | None:
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


def _bounded_int(
    value: Any,
    default: int,
    minimum: int,
    maximum: int,
    label: str,
    errors: list[str],
) -> int:
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
    _fields(value, CONFIG_FIELDS, CONFIG_FIELDS, "scenario configuration", errors)
    if value.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"scenario configuration schema_version must be {SCHEMA_VERSION}")
    raw_runners = _object(value.get("runners"), "scenario configuration runners", errors)
    if not raw_runners:
        errors.append("scenario configuration runners must not be empty")
    runners: dict[str, RunnerDefinition] = {}
    for name, raw in raw_runners.items():
        if not isinstance(name, str) or SAFE_ID.fullmatch(name) is None:
            errors.append(f"runner name is unsafe: {name!r}")
            continue
        runner = _object(raw, f"runner {name}", errors)
        _fields(
            runner,
            frozenset({"command"}),
            RUNNER_FIELDS,
            f"runner {name}",
            errors,
        )
        command = runner.get("command")
        if (
            not isinstance(command, list)
            or not command
            or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in command)
        ):
            errors.append(
                f"runner {name}.command must be a non-empty string array without NUL bytes"
            )
            command_value: tuple[str, ...] = ()
        else:
            command_value = tuple(command)
        runners[name] = RunnerDefinition(
            command=command_value,
            timeout_seconds=_bounded_int(
                runner.get("timeout_seconds"),
                300,
                1,
                3600,
                f"runner {name}.timeout_seconds",
                errors,
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
    return ScenarioPolicy(runners=runners, sha256=_sha256(content)), ()


def _string_list(
    value: Any,
    label: str,
    errors: list[str],
    pattern: re.Pattern[str] | None = None,
) -> tuple[str, ...]:
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
) -> dict[str, Any]:
    label = f"scenario[{index}]"
    scenario = _object(raw, label, errors)
    _fields(scenario, SCENARIO_FIELDS, SCENARIO_FIELDS, label, errors)
    scenario_id = scenario.get("id")
    if not isinstance(scenario_id, str) or SCENARIO_ID.fullmatch(scenario_id) is None:
        errors.append(f"{label}.id must match {SCENARIO_ID.pattern}")
    title = scenario.get("title")
    if not isinstance(title, str) or not title.strip():
        errors.append(f"{label}.title must be a non-empty string")
    covers = _object(scenario.get("covers"), f"{label}.covers", errors)
    _fields(covers, COVERS_FIELDS, COVERS_FIELDS, f"{label}.covers", errors)
    _string_list(covers.get("acceptance"), f"{label}.covers.acceptance", errors, AC_PATTERN)
    _string_list(covers.get("flow"), f"{label}.covers.flow", errors, P_REF_PATTERN)
    runner_name = scenario.get("runner")
    if not isinstance(runner_name, str) or runner_name not in runners:
        errors.append(f"{label}.runner does not name a configured runner")
    _string_list(scenario.get("given"), f"{label}.given", errors)
    _string_list(scenario.get("when"), f"{label}.when", errors)

    raw_observations = scenario.get("then")
    if not isinstance(raw_observations, list) or not raw_observations:
        errors.append(f"{label}.then must be a non-empty list")
        return scenario
    observation_ids: list[str] = []
    for observation_index, raw_observation in enumerate(raw_observations):
        observation_label = f"{label}.then[{observation_index}]"
        observation = _object(raw_observation, observation_label, errors)
        _fields(
            observation,
            OBSERVATION_FIELDS,
            OBSERVATION_FIELDS,
            observation_label,
            errors,
        )
        observation_id = observation.get("id")
        if (
            not isinstance(observation_id, str)
            or OBSERVATION_ID.fullmatch(observation_id) is None
        ):
            errors.append(f"{observation_label}.id must match {OBSERVATION_ID.pattern}")
        else:
            observation_ids.append(observation_id)
        expectation = observation.get("expectation")
        if not isinstance(expectation, str) or not expectation.strip():
            errors.append(f"{observation_label}.expectation must be a non-empty string")
    if len(observation_ids) != len(set(observation_ids)):
        errors.append(f"{label}.then contains duplicate observation ids")
    return scenario


def _parent_contract(
    task_dir: Path, policy: ScenarioPolicy, errors: list[str]
) -> tuple[tuple[dict[str, Any], ...], bytes]:
    loaded = _load_json(task_dir / CONTRACT_FILENAME, CONTRACT_FILENAME, errors)
    if loaded is None:
        return (), b""
    value, content = loaded
    _fields(value, CONTRACT_FIELDS, CONTRACT_FIELDS, "scenario contract", errors)
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

    scenario_ids = [
        scenario.get("id")
        for scenario in scenarios
        if isinstance(scenario.get("id"), str)
    ]
    duplicate_scenarios = sorted(
        {item for item in scenario_ids if scenario_ids.count(item) > 1}
    )
    if duplicate_scenarios:
        errors.append("duplicate scenario id: " + ", ".join(duplicate_scenarios))

    observation_ids = [
        observation.get("id")
        for scenario in scenarios
        for observation in scenario.get("then", [])
        if isinstance(observation, dict) and isinstance(observation.get("id"), str)
    ]
    duplicate_observations = sorted(
        {item for item in observation_ids if observation_ids.count(item) > 1}
    )
    if duplicate_observations:
        errors.append("duplicate observation id: " + ", ".join(duplicate_observations))

    runner_to_scenarios: dict[str, list[str]] = {}
    for scenario in scenarios:
        runner_name = scenario.get("runner")
        scenario_id = scenario.get("id")
        if isinstance(runner_name, str) and isinstance(scenario_id, str):
            runner_to_scenarios.setdefault(runner_name, []).append(scenario_id)
    for runner_name, assigned in runner_to_scenarios.items():
        if len(assigned) > 1:
            errors.append(
                f"runner {runner_name} must be exclusive; assigned scenarios: "
                + ", ".join(sorted(assigned))
            )

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
        for reference in covers.get("acceptance", []):
            if isinstance(reference, str):
                covered_acceptance.add(reference)
                if reference not in task_refs:
                    errors.append(f"acceptance reference {reference} is missing from task.md")
        for reference in covers.get("flow", []):
            if isinstance(reference, str) and reference not in flow_refs:
                errors.append(f"flow reference {reference} is missing from implementation.md")
    missing = sorted(task_refs - covered_acceptance)
    if missing:
        errors.append(f"missing acceptance coverage: {', '.join(missing)}")
    return scenarios, content


def _project_task_dir(project_root: Path, task_dir: Path, errors: list[str]) -> Path | None:
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


def _scenario_owner(task: Path, errors: list[str]) -> Path | None:
    inheritance_path = task / INHERITANCE_FILENAME
    if not inheritance_path.exists() and not inheritance_path.is_symlink():
        return task
    loaded = _load_json(inheritance_path, INHERITANCE_FILENAME, errors)
    if loaded is None:
        return None
    inheritance, _ = loaded
    parent_name = inheritance.get("parent_task")
    if (
        not isinstance(parent_name, str)
        or not parent_name
        or parent_name.startswith(".")
        or Path(parent_name).parts != (parent_name,)
    ):
        errors.append("child scenario parent_task is unsafe")
        return None
    parent = task.parent / parent_name
    try:
        if parent == task or parent.is_symlink() or not parent.resolve(strict=True).is_dir():
            raise OSError("not a safe direct Full parent")
        parent = parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        errors.append(f"cannot resolve child scenario parent: {exc}")
        return None
    if (parent / INHERITANCE_FILENAME).exists() or (parent / INHERITANCE_FILENAME).is_symlink():
        errors.append("child scenario parent must be a direct Full task")
        return None
    for filename in OBSOLETE_CHILD_FILENAMES:
        path = task / filename
        if path.exists() or path.is_symlink():
            errors.append(
                f"inherited child must not own {filename}; use parent task {parent.name}"
            )
    return parent


def _resolve_scenario_set(
    project_root: Path,
    task_dir: Path,
    policy: ScenarioPolicy,
    errors: list[str],
) -> _ResolvedScenarioSet | None:
    task = _project_task_dir(project_root, task_dir, errors)
    if task is None:
        return None
    readiness = validate_task_dir(task)
    if not readiness.ready:
        errors.extend(f"task readiness: {error}" for error in readiness.errors)
    owner = _scenario_owner(task, errors)
    if owner is None:
        return None
    if owner != task:
        owner_readiness = validate_task_dir(owner)
        if not owner_readiness.ready:
            errors.extend(f"parent task readiness: {error}" for error in owner_readiness.errors)
    scenarios, contract_content = _parent_contract(owner, policy, errors)
    return _ResolvedScenarioSet(
        requested_task=task,
        owner_task=owner,
        flow_path=owner / "implementation.md",
        contract_content=contract_content,
        scenarios=scenarios,
    )


def validate_readiness(
    task_dir: Path | str, project_root: Path | str
) -> ScenarioGateResult:
    root = Path(project_root)
    policy, policy_errors = load_policy(root)
    if policy is None and not policy_errors:
        return ScenarioGateResult(False, True, (), ())
    if policy is None:
        return ScenarioGateResult(True, False, policy_errors, ())
    errors: list[str] = []
    resolved = _resolve_scenario_set(root, Path(task_dir), policy, errors)
    required = ()
    if resolved is not None:
        required = tuple(
            scenario["id"]
            for scenario in resolved.scenarios
            if isinstance(scenario.get("id"), str)
        )
    return ScenarioGateResult(True, not errors, tuple(errors), required)


def _ordered_observations(
    scenarios: tuple[dict[str, Any], ...]
) -> tuple[tuple[str, str], ...]:
    return tuple(
        (observation["id"], scenario["id"])
        for scenario in scenarios
        if isinstance(scenario.get("id"), str)
        for observation in scenario.get("then", [])
        if isinstance(observation, dict) and isinstance(observation.get("id"), str)
    )


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
        root,
        (
            "diff",
            "--binary",
            "--no-ext-diff",
            "HEAD",
            "--",
            ".",
            ":(exclude)_workspace/**",
            ":(exclude).agent-gate/scenario-gate.json",
        ),
    )
    if error:
        errors.append(error)
    untracked, error = _git_output(root, ("ls-files", "--others", "--exclude-standard", "-z"))
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
        relative = Path(os.fsdecode(raw_relative))
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or relative.parts[:1] in ((".git",), ("_workspace",))
            or relative == CONFIG_RELATIVE
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
        digest.update(f"{stat.st_mode}:{stat.st_size}:{stat.st_mtime_ns}".encode("ascii"))
        digest.update(b"\0")
    if errors:
        return None, tuple(errors)
    return digest.hexdigest(), ()


def evidence_template(task_dir: Path | str, project_root: Path | str) -> dict[str, Any]:
    root = Path(project_root).resolve(strict=True)
    policy, policy_errors = load_policy(root)
    if policy is None:
        raise ValueError("; ".join(policy_errors) or "scenario gate is disabled")
    errors: list[str] = []
    resolved = _resolve_scenario_set(root, Path(task_dir), policy, errors)
    if resolved is None:
        raise ValueError("; ".join(errors))
    if resolved.owner_task != resolved.requested_task:
        raise ValueError(
            f"write scenario evidence on Full parent task {resolved.owner_task.name}"
        )
    fingerprint, fingerprint_errors = _source_fingerprint(root)
    errors.extend(fingerprint_errors)
    try:
        task_sha256 = _sha256((resolved.owner_task / "task.md").read_bytes())
        flow_sha256 = _sha256(resolved.flow_path.read_bytes())
    except OSError as exc:
        errors.append(f"cannot create scenario evidence template: {exc}")
    if errors or fingerprint is None:
        raise ValueError("; ".join(errors))
    return {
        "schema_version": SCHEMA_VERSION,
        "task_sha256": task_sha256,
        "flow_sha256": flow_sha256,
        "contract_sha256": _sha256(resolved.contract_content),
        "source_fingerprint": fingerprint,
        "observations": [
            {"id": observation_id, "implementation": [], "verification": []}
            for observation_id, _ in _ordered_observations(resolved.scenarios)
        ],
        "verdict": "revise",
        "blocking_findings": ["independent scenario evidence review has not run"],
    }


def _validate_locations(
    raw: Any,
    label: str,
    root: Path,
    errors: list[str],
) -> bool:
    if not isinstance(raw, list) or not raw:
        errors.append(f"{label} must be a non-empty list")
        return False
    valid = True
    for index, raw_location in enumerate(raw):
        location_label = f"{label}[{index}]"
        location = _object(raw_location, location_label, errors)
        if not _fields(location, LOCATION_FIELDS, LOCATION_FIELDS, location_label, errors):
            valid = False
        raw_path = location.get("path")
        if (
            not isinstance(raw_path, str)
            or not raw_path
            or raw_path != raw_path.strip()
            or Path(raw_path).is_absolute()
            or ".." in Path(raw_path).parts
            or Path(raw_path).parts[:1] in ((".git",), ("_workspace",))
        ):
            errors.append(f"{location_label}.path must be a safe project-relative source path")
            valid = False
            continue
        path = root / raw_path
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(root)
            if path.is_symlink() or not resolved.is_file():
                raise OSError("not a regular non-symlink file")
            lines = resolved.read_text(encoding="utf-8").splitlines()
        except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
            errors.append(f"{location_label}.path is invalid: {exc}")
            valid = False
            continue
        line = location.get("line")
        if isinstance(line, bool) or not isinstance(line, int) or not 1 <= line <= len(lines):
            errors.append(f"{location_label}.line must identify an existing source line")
            valid = False
    return valid


def _validate_evidence(
    resolved: _ResolvedScenarioSet,
    root: Path,
    fingerprint: str | None,
    errors: list[str],
) -> tuple[set[str], set[str], bool]:
    local_errors: list[str] = []
    loaded = _load_json(
        resolved.owner_task / EVIDENCE_FILENAME, EVIDENCE_FILENAME, local_errors
    )
    if loaded is None:
        errors.extend(local_errors)
        return set(), set(), False
    evidence, _ = loaded
    schema_valid = _fields(
        evidence, EVIDENCE_FIELDS, EVIDENCE_FIELDS, "scenario evidence", local_errors
    )
    if evidence.get("schema_version") != SCHEMA_VERSION:
        local_errors.append(f"scenario evidence schema_version must be {SCHEMA_VERSION}")
        schema_valid = False
    for field, path in (
        ("task_sha256", resolved.owner_task / "task.md"),
        ("flow_sha256", resolved.flow_path),
    ):
        try:
            actual = _sha256(path.read_bytes())
        except OSError as exc:
            local_errors.append(f"cannot read {path.name} for scenario evidence: {exc}")
            schema_valid = False
            continue
        if evidence.get(field) != actual:
            local_errors.append(f"scenario evidence {field} is stale")
            schema_valid = False
    if evidence.get("contract_sha256") != _sha256(resolved.contract_content):
        local_errors.append("scenario evidence contract_sha256 is stale")
        schema_valid = False
    if fingerprint is None or evidence.get("source_fingerprint") != fingerprint:
        local_errors.append("scenario evidence source_fingerprint is stale")
        schema_valid = False

    expected_ids = tuple(item[0] for item in _ordered_observations(resolved.scenarios))
    raw_items = evidence.get("observations")
    if not isinstance(raw_items, list):
        local_errors.append("scenario evidence observations must be a list")
        raw_items = []
        schema_valid = False
    implementation_mapped: set[str] = set()
    verification_mapped: set[str] = set()
    observed_ids: list[str] = []
    for index, raw_item in enumerate(raw_items):
        label = f"scenario evidence observations[{index}]"
        item = _object(raw_item, label, local_errors)
        if not _fields(item, EVIDENCE_ITEM_FIELDS, EVIDENCE_ITEM_FIELDS, label, local_errors):
            schema_valid = False
        observation_id = item.get("id")
        if not isinstance(observation_id, str) or OBSERVATION_ID.fullmatch(observation_id) is None:
            local_errors.append(f"{label}.id is invalid")
            schema_valid = False
            continue
        observed_ids.append(observation_id)
        if _validate_locations(item.get("implementation"), f"{label}.implementation", root, local_errors):
            implementation_mapped.add(observation_id)
        if _validate_locations(item.get("verification"), f"{label}.verification", root, local_errors):
            verification_mapped.add(observation_id)
    if tuple(observed_ids) != expected_ids:
        local_errors.append("scenario evidence observation ids must exactly match the contract")
        schema_valid = False
    if len(observed_ids) != len(set(observed_ids)):
        local_errors.append("scenario evidence contains duplicate observation ids")
        schema_valid = False

    semantic_approved = schema_valid
    if evidence.get("verdict") != "pass":
        local_errors.append("scenario evidence verdict must be 'pass'")
        semantic_approved = False
    findings = evidence.get("blocking_findings")
    if not isinstance(findings, list):
        local_errors.append("scenario evidence blocking_findings must be a list")
        semantic_approved = False
    elif findings:
        local_errors.append("scenario evidence blocking_findings must be empty")
        semantic_approved = False
    errors.extend(local_errors)
    return implementation_mapped, verification_mapped, semantic_approved


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


def _execute_runner(
    root: Path, definition: RunnerDefinition, scenario_id: str
) -> tuple[dict[str, Any], tuple[str, ...]]:
    environment = {key: value for key, value in os.environ.items() if key in SAFE_ENVIRONMENT}
    started = time.monotonic()
    timed_out = False
    output_exceeded = False
    launch_error: OSError | None = None
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

    item: dict[str, Any] = {"id": scenario_id, "duration_ms": duration_ms}
    if launch_error is not None:
        reason = f"runner launch failed: {launch_error}"
        item.update(status="infrastructure-error", reason=reason)
        return item, (reason,)
    if timed_out:
        reason = f"runner timed out after {definition.timeout_seconds}s"
        item.update(status="infrastructure-error", reason=reason)
        return item, (reason,)
    if output_exceeded or output_size > definition.max_output_bytes:
        reason = f"runner output exceeded {definition.max_output_bytes} bytes"
        item.update(status="infrastructure-error", reason=reason)
        return item, (reason,)
    if return_code == 0:
        item["status"] = "passed"
    else:
        item.update(status="failed", reason=f"runner exited with code {return_code}")
    return item, ()


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


def run_scenarios(
    task_dir: Path | str, project_root: Path | str
) -> ScenarioRunResult:
    root = Path(project_root).resolve(strict=True)
    policy, policy_errors = load_policy(root)
    if policy is None:
        return ScenarioRunResult(False, policy_errors or ("scenario gate is disabled",), None)
    errors: list[str] = []
    resolved = _resolve_scenario_set(root, Path(task_dir), policy, errors)
    if resolved is None or errors:
        return ScenarioRunResult(False, tuple(errors), None)

    results: list[dict[str, Any]] = []
    execution_errors: list[str] = []
    for scenario in resolved.scenarios:
        scenario_id = scenario["id"]
        runner_name = scenario["runner"]
        item, runner_errors = _execute_runner(root, policy.runners[runner_name], scenario_id)
        results.append(item)
        execution_errors.extend(f"runner {runner_name}: {error}" for error in runner_errors)

    fingerprint, fingerprint_errors = _source_fingerprint(root)
    if fingerprint is None:
        return ScenarioRunResult(False, tuple(execution_errors) + fingerprint_errors, None)
    result_value = {
        "schema_version": SCHEMA_VERSION,
        "contract_sha256": _sha256(resolved.contract_content),
        "runner_config_sha256": policy.sha256,
        "source_fingerprint": fingerprint,
        "results": results,
    }
    result_path = resolved.owner_task / RESULT_FILENAME
    try:
        _atomic_write_json(result_path, result_value)
    except OSError as exc:
        return ScenarioRunResult(
            False,
            tuple(execution_errors) + (f"cannot write {RESULT_FILENAME}: {exc}",),
            None,
        )
    return ScenarioRunResult(True, tuple(execution_errors), result_path)


def _stored_results(
    raw: Any, expected_ids: tuple[str, ...], errors: list[str]
) -> tuple[dict[str, dict[str, Any]], bool]:
    if not isinstance(raw, list):
        errors.append("scenario result results must be a list")
        return {}, False
    by_id: dict[str, dict[str, Any]] = {}
    valid = True
    for index, raw_item in enumerate(raw):
        label = f"scenario result[{index}]"
        item = _object(raw_item, label, errors)
        if not _fields(
            item,
            frozenset({"id", "status", "duration_ms"}),
            RESULT_ITEM_FIELDS,
            label,
            errors,
        ):
            valid = False
        scenario_id = item.get("id")
        if not isinstance(scenario_id, str) or scenario_id not in expected_ids:
            errors.append(f"{label}.id is not an effective scenario")
            valid = False
            continue
        if scenario_id in by_id:
            errors.append(f"scenario result has duplicate scenario id: {scenario_id}")
            valid = False
            continue
        status = item.get("status")
        if status not in RESULT_STATUSES:
            errors.append(f"scenario result {scenario_id}.status is invalid")
            valid = False
        duration = item.get("duration_ms")
        if isinstance(duration, bool) or not isinstance(duration, int) or duration < 0:
            errors.append(f"scenario result {scenario_id}.duration_ms must be non-negative")
            valid = False
        reason = item.get("reason")
        if reason is not None and not isinstance(reason, str):
            errors.append(f"scenario result {scenario_id}.reason must be a string")
            valid = False
        by_id[scenario_id] = item
    if tuple(by_id) != expected_ids:
        errors.append("scenario result ids must exactly match the contract")
        valid = False
    return by_id, valid


def _validate_result(
    resolved: _ResolvedScenarioSet,
    policy: ScenarioPolicy,
    fingerprint: str | None,
    errors: list[str],
) -> tuple[set[str], bool]:
    loaded = _load_json(resolved.owner_task / RESULT_FILENAME, RESULT_FILENAME, errors)
    if loaded is None:
        return set(), False
    result, _ = loaded
    current = _fields(result, RESULT_FIELDS, RESULT_FIELDS, "scenario result", errors)
    if result.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"scenario result schema_version must be {SCHEMA_VERSION}")
        current = False
    if result.get("contract_sha256") != _sha256(resolved.contract_content):
        errors.append("scenario result contract_sha256 is stale")
        current = False
    if result.get("runner_config_sha256") != policy.sha256:
        errors.append("scenario result runner_config_sha256 is stale")
        current = False
    if fingerprint is None or result.get("source_fingerprint") != fingerprint:
        errors.append("scenario result source_fingerprint is stale")
        current = False
    expected_ids = tuple(
        scenario["id"]
        for scenario in resolved.scenarios
        if isinstance(scenario.get("id"), str)
    )
    by_id, items_valid = _stored_results(result.get("results"), expected_ids, errors)
    current = current and items_valid
    passed = {
        scenario_id
        for scenario_id, item in by_id.items()
        if item.get("status") == "passed"
    }
    return passed, current


def validate_completion(
    task_dir: Path | str, project_root: Path | str
) -> ScenarioGateResult:
    root = Path(project_root)
    policy, policy_errors = load_policy(root)
    if policy is None and not policy_errors:
        return ScenarioGateResult(False, True, (), ())
    if policy is None:
        return ScenarioGateResult(True, False, policy_errors, ())

    errors: list[str] = []
    resolved = _resolve_scenario_set(root, Path(task_dir), policy, errors)
    if resolved is None:
        return ScenarioGateResult(True, False, tuple(errors), ())
    required_scenarios = tuple(
        scenario["id"]
        for scenario in resolved.scenarios
        if isinstance(scenario.get("id"), str)
    )
    fingerprint, fingerprint_errors = _source_fingerprint(root.resolve(strict=True))
    errors.extend(fingerprint_errors)
    implementation_ids, verification_ids, evidence_approved = _validate_evidence(
        resolved, root.resolve(strict=True), fingerprint, errors
    )
    passed_scenarios, result_current = _validate_result(
        resolved, policy, fingerprint, errors
    )

    observations = _ordered_observations(resolved.scenarios)
    required_ids = {observation_id for observation_id, _ in observations}
    execution_ids = {
        observation_id
        for observation_id, scenario_id in observations
        if result_current and scenario_id in passed_scenarios
    }
    verified_ids = set()
    if evidence_approved and result_current:
        verified_ids = required_ids & implementation_ids & verification_ids & execution_ids

    for observation_id in sorted(required_ids - implementation_ids):
        errors.append(f"implementation evidence is missing: {observation_id}")
    for observation_id in sorted(required_ids - verification_ids):
        errors.append(f"verification evidence is missing: {observation_id}")
    for scenario_id in required_scenarios:
        if scenario_id not in passed_scenarios:
            errors.append(f"required scenario did not pass: {scenario_id}")

    required_count = len(required_ids)
    percentage = round((len(verified_ids) / required_count) * 100, 2) if required_count else 0.0
    coverage = ScenarioCoverage(
        required=required_count,
        implementation_mapped=len(required_ids & implementation_ids),
        verification_mapped=len(required_ids & verification_ids),
        execution_passed=len(required_ids & execution_ids),
        verified=len(verified_ids),
        percentage=percentage,
    )
    if percentage != 100.0:
        errors.append(f"scenario evidence coverage must be 100%, got {percentage:.2f}%")
    return ScenarioGateResult(
        True,
        not errors,
        tuple(dict.fromkeys(errors)),
        required_scenarios,
        coverage,
    )


def _gate_json(result: ScenarioGateResult) -> dict[str, Any]:
    return {
        "enabled": result.enabled,
        "allowed": result.allowed,
        "errors": list(result.errors),
        "required_scenarios": list(result.required_scenarios),
        "coverage": asdict(result.coverage) if result.coverage is not None else None,
    }


def _print_gate(result: ScenarioGateResult, as_json: bool) -> None:
    if as_json:
        print(json.dumps(_gate_json(result), ensure_ascii=False, sort_keys=True))
        return
    print("PASS" if result.allowed else "BLOCK")
    if result.coverage is not None:
        print(f"  scenario evidence coverage: {result.coverage.percentage:.2f}%")
    for error in result.errors:
        print(f"  error: {error}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    for name in ("readiness", "evidence-template", "run", "completion"):
        command = subparsers.add_parser(name)
        command.add_argument("task_dir", type=Path)
        command.add_argument("--project-root", required=True, type=Path)
        command.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.operation == "evidence-template":
        try:
            template = evidence_template(args.task_dir, args.project_root)
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

    run = run_scenarios(args.task_dir, args.project_root)
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
