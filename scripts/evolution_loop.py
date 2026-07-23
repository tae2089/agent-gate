#!/usr/bin/env python3
"""Deterministic contracts for the agent-gate evolutionary loop."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

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
    }
)
CANDIDATE_ALLOWED_FIELDS = CANDIDATE_REQUIRED_FIELDS | {"request"}
WORK_KINDS = frozenset(
    {"feature", "bug", "contract-violation", "technical-debt"}
)
SOURCES = frozenset(
    {"manual", "github", "jira", "ci", "repository", "code-analysis"}
)
FEATURE_SOURCES = frozenset({"manual", "github", "jira"})
STATE_REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "iteration",
        "max_iterations",
        "candidate_sha256",
        "pr_url",
    }
)
STATE_FIELDS = STATE_REQUIRED_FIELDS | {"github_repository"}
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
        "publish-blocked",
        "publish-uncertain",
        "pr-opened",
    }
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


@dataclass(frozen=True)
class RunResult:
    allowed: bool
    errors: tuple[str, ...]
    state: Mapping[str, Any]


@dataclass(frozen=True)
class DiscoveryResult:
    records: tuple[Mapping[str, Any], ...]
    errors: tuple[str, ...]
    github_repository: Optional[str]


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
    labels = _string_list(value.get("labels"), "candidate labels", errors)

    if kind is not None and kind not in WORK_KINDS:
        errors.append(f"unsupported candidate kind: {kind}")
    if source is not None and source not in SOURCES:
        errors.append(f"unsupported candidate source: {source}")
    if not evidence:
        errors.append("candidate evidence must be non-empty")

    if kind == "feature":
        if source not in FEATURE_SOURCES:
            errors.append("product features require manual, GitHub, or Jira evidence")
        if source == "manual":
            _non_empty_string(
                value.get("request"), "manual feature request", errors
            )
        elif source in {"github", "jira"} and "agent-ready" not in {
            label.lower() for label in labels
        }:
            errors.append(
                "GitHub and Jira product features require the agent-ready label"
            )

    normalized = {
        field: value[field]
        for field in CANDIDATE_ALLOWED_FIELDS
        if field in value
    }
    return CandidateValidation(not errors, tuple(dict.fromkeys(errors)), normalized)


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary_path.replace(path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


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
    missing = sorted(STATE_REQUIRED_FIELDS - value.keys())
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
    github_repository = value.get("github_repository")
    if (
        "github_repository" in value
        and not _valid_github_repository(github_repository)
    ):
        errors.append(
            "evolution state github_repository must use owner/repo format"
        )
    if errors:
        return RunResult(False, tuple(errors), value)
    return RunResult(True, (), value)


def _active_evolution_task(
    project_root: Path,
) -> tuple[Optional[Path], tuple[str, ...]]:
    root = Path(project_root).resolve(strict=True)
    pointer = root / "_workspace" / ACTIVE_EVOLUTION_FILENAME
    if pointer.is_symlink():
        return None, ("active evolution pointer must not be a symlink",)
    try:
        raw = pointer.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, ("no active evolution run",)
    except (OSError, UnicodeError) as exc:
        return None, (f"cannot read active evolution pointer: {exc}",)
    if not raw.endswith("\n") or not raw.strip() or "\n" in raw.rstrip("\n"):
        return None, ("active evolution pointer is malformed",)
    task, errors = _direct_task(Path(raw.strip()), root)
    if task is None:
        return None, errors
    return task, ()


def start_run(
    task_dir: Path,
    candidate: Any,
    max_iterations: int = 3,
    approved_records: Sequence[Mapping[str, Any]] = (),
    github_repository: Optional[str] = None,
) -> RunResult:
    validation = validate_candidate(candidate)
    if not validation.allowed:
        return RunResult(False, validation.errors, {})
    normalized_candidate = validation.candidate
    if (
        normalized_candidate.get("kind") == "feature"
        and normalized_candidate.get("source") in {"github", "jira"}
    ):
        approved = any(
            record.get("source") == normalized_candidate["source"]
            and record.get("source_ref") == normalized_candidate["source_ref"]
            and isinstance(record.get("labels"), list)
            and "agent-ready"
            in {
                label.lower()
                for label in record["labels"]
                if isinstance(label, str)
            }
            for record in approved_records
        )
        if not approved:
            return RunResult(
                False,
                (
                    "external product feature is not present in live discovery "
                    "with agent-ready",
                ),
                {},
            )
    if (
        isinstance(max_iterations, bool)
        or not isinstance(max_iterations, int)
        or not 1 <= max_iterations <= 10
    ):
        return RunResult(
            False, ("max_iterations must be an integer from 1 through 10",), {}
        )
    if (
        github_repository is not None
        and not _valid_github_repository(github_repository)
    ):
        return RunResult(
            False,
            ("GitHub repository must use owner/repo format",),
            {},
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
        "candidate_sha256": hashlib.sha256(candidate_content).hexdigest(),
        "pr_url": None,
    }
    if github_repository is not None:
        state["github_repository"] = github_repository
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
    state = dict(loaded.state)
    status = state["status"]
    if status not in PHASE_TRANSITIONS:
        return RunResult(False, ("terminal evolution state cannot transition",), state)
    if next_phase not in PHASE_TRANSITIONS[status]:
        return RunResult(
            False,
            (f"evolution transition {status} -> {next_phase} is not allowed",),
            state,
        )

    if next_phase == "interview":
        if state["iteration"] >= state["max_iterations"]:
            state["status"] = "budget-exhausted"
        else:
            state["status"] = "interview"
            state["iteration"] += 1
    else:
        state["status"] = next_phase
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


def _run_discovery_command(
    argv: list[str],
    project_root: Path,
    command_runner: Any,
    label: str,
    errors: list[str],
) -> list[dict[str, Any]]:
    try:
        completed = command_runner(
            argv,
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        errors.append(f"{label} discovery could not start")
        return []
    if completed.returncode != 0:
        errors.append(
            f"{label} discovery failed with exit code {completed.returncode}"
        )
        return []
    try:
        value = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError):
        errors.append(f"{label} discovery returned invalid JSON")
        return []
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        errors.append(f"{label} discovery must return a JSON array of objects")
        return []
    return value


def _valid_github_repository(value: Any) -> bool:
    if not isinstance(value, str) or value != value.strip():
        return False
    parts = value.split("/")
    return (
        len(parts) == 2
        and all(parts)
        and not any(character.isspace() for character in value)
        and not any(part.startswith("-") for part in parts)
    )


def resolve_github_repository(
    project_root: Path,
    requested_repository: Optional[str] = None,
    command_runner: Any = subprocess.run,
) -> tuple[Optional[str], tuple[str, ...]]:
    if (
        requested_repository is not None
        and not _valid_github_repository(requested_repository)
    ):
        return None, ("GitHub repository must use owner/repo format",)

    root = Path(project_root).resolve(strict=True)
    authentication, errors = _command_receipt(
        ["gh", "auth", "status"],
        root,
        command_runner,
        "GitHub authentication",
    )
    if errors:
        return None, errors
    assert authentication is not None

    resolved, errors = _command_receipt(
        ["gh", "repo", "view", "--json", "nameWithOwner"],
        root,
        command_runner,
        "GitHub repository resolution",
    )
    if errors:
        return None, errors
    assert resolved is not None
    try:
        payload = json.loads(resolved.stdout)
    except json.JSONDecodeError:
        return None, ("GitHub repository resolution returned invalid JSON",)
    if (
        not isinstance(payload, dict)
        or not _valid_github_repository(payload.get("nameWithOwner"))
    ):
        return None, (
            "GitHub repository resolution returned an invalid JSON object",
        )
    repository = payload["nameWithOwner"]
    if (
        requested_repository is not None
        and requested_repository.casefold() != repository.casefold()
    ):
        return None, (
            "requested GitHub repository does not match the project repository",
        )
    return repository, ()


def _github_records(
    project_root: Path,
    github_repository: str,
    command_runner: Any,
    errors: list[str],
) -> list[Mapping[str, Any]]:
    records: list[Mapping[str, Any]] = []
    issues = _run_discovery_command(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            github_repository,
            "--state",
            "open",
            "--label",
            "agent-ready",
            "--limit",
            "50",
            "--json",
            "number,title,body,labels,url,updatedAt",
        ],
        project_root,
        command_runner,
        "GitHub issue",
        errors,
    )
    for issue in issues:
        labels_value = issue.get("labels", [])
        labels = []
        if isinstance(labels_value, list):
            for label in labels_value:
                if isinstance(label, dict) and isinstance(label.get("name"), str):
                    labels.append(label["name"])
                elif isinstance(label, str):
                    labels.append(label)
        title = issue.get("title")
        url = issue.get("url")
        if not isinstance(title, str) or not title.strip():
            errors.append("GitHub issue discovery returned an invalid title")
            continue
        if not isinstance(url, str) or not url.strip():
            errors.append("GitHub issue discovery returned an invalid URL")
            continue
        records.append(
            {
                "source": "github",
                "source_ref": url,
                "title": title,
                "body": issue.get("body") if isinstance(issue.get("body"), str) else "",
                "labels": labels,
                "updated_at": (
                    issue.get("updatedAt")
                    if isinstance(issue.get("updatedAt"), str)
                    else ""
                ),
            }
        )

    runs = _run_discovery_command(
        [
            "gh",
            "run",
            "list",
            "--repo",
            github_repository,
            "--status",
            "failure",
            "--limit",
            "20",
            "--json",
            "databaseId,displayTitle,conclusion,status,url,workflowName,updatedAt",
        ],
        project_root,
        command_runner,
        "GitHub CI",
        errors,
    )
    for run in runs:
        title = run.get("displayTitle")
        url = run.get("url")
        if not isinstance(title, str) or not title.strip():
            errors.append("GitHub CI discovery returned an invalid title")
            continue
        if not isinstance(url, str) or not url.strip():
            errors.append("GitHub CI discovery returned an invalid URL")
            continue
        records.append(
            {
                "source": "ci",
                "source_ref": url,
                "title": title,
                "body": (
                    f"workflow={run.get('workflowName', '')}; "
                    f"status={run.get('status', '')}; "
                    f"conclusion={run.get('conclusion', '')}"
                ),
                "labels": [],
                "updated_at": (
                    run.get("updatedAt")
                    if isinstance(run.get("updatedAt"), str)
                    else ""
                ),
            }
        )
    return records


def _adf_text(value: Any) -> str:
    text: list[str] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "text" and isinstance(node.get("text"), str):
                text.append(node["text"])
            for child in node.get("content", []):
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    if isinstance(value, str):
        return value
    visit(value)
    return "\n".join(part for part in text if part)


def _jira_records(
    environment: Mapping[str, str], jira_opener: Any, errors: list[str]
) -> list[Mapping[str, Any]]:
    names = (
        "AGENT_GATE_JIRA_BASE_URL",
        "AGENT_GATE_JIRA_EMAIL",
        "AGENT_GATE_JIRA_API_TOKEN",
    )
    configured = [bool(environment.get(name)) for name in names]
    if not any(configured):
        errors.append("Jira discovery is not configured")
        return []
    if not all(configured):
        errors.append("Jira discovery configuration is incomplete")
        return []

    base_url = environment[names[0]].rstrip("/")
    parsed = urlparse(base_url)
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.hostname
        or not parsed.hostname.endswith(".atlassian.net")
    ):
        errors.append("Jira base URL must be an HTTPS atlassian.net site URL")
        return []

    credentials = (
        f"{environment[names[1]]}:{environment[names[2]]}".encode("utf-8")
    )
    request = Request(
        f"{base_url}/rest/api/3/search/jql",
        data=json.dumps(
            {
                "jql": 'labels = "agent-ready" AND statusCategory != Done '
                "ORDER BY updated DESC",
                "maxResults": 50,
                "fields": [
                    "summary",
                    "description",
                    "labels",
                    "issuetype",
                    "status",
                ],
            }
        ).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": (
                "Basic " + base64.b64encode(credentials).decode("ascii")
            ),
        },
        method="POST",
    )
    try:
        with jira_opener(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        errors.append(f"Jira discovery failed with HTTP {exc.code}")
        return []
    except (URLError, TimeoutError, OSError):
        errors.append("Jira discovery request failed")
        return []
    except (UnicodeError, json.JSONDecodeError):
        errors.append("Jira discovery returned invalid JSON")
        return []

    if not isinstance(payload, dict) or not isinstance(payload.get("issues"), list):
        errors.append("Jira discovery must return an issues array")
        return []
    records: list[Mapping[str, Any]] = []
    for issue in payload["issues"]:
        if not isinstance(issue, dict) or not isinstance(issue.get("fields"), dict):
            errors.append("Jira discovery returned an invalid issue")
            continue
        key = issue.get("key")
        fields = issue["fields"]
        title = fields.get("summary")
        if not isinstance(key, str) or not key.strip():
            errors.append("Jira discovery returned an invalid issue key")
            continue
        if not isinstance(title, str) or not title.strip():
            errors.append("Jira discovery returned an invalid summary")
            continue
        labels = fields.get("labels", [])
        if not isinstance(labels, list) or any(
            not isinstance(label, str) for label in labels
        ):
            errors.append(f"Jira issue {key} returned invalid labels")
            continue
        records.append(
            {
                "source": "jira",
                "source_ref": f"{base_url}/browse/{key}",
                "title": title,
                "body": _adf_text(fields.get("description")),
                "labels": labels,
                "updated_at": "",
            }
        )
    return records


def discover_evidence(
    project_root: Path,
    github_repository: Optional[str] = None,
    environment: Optional[Mapping[str, str]] = None,
    command_runner: Any = subprocess.run,
    jira_opener: Any = urlopen,
) -> DiscoveryResult:
    root = Path(project_root).resolve(strict=True)
    source_environment = os.environ if environment is None else environment
    resolved_repository, preflight_errors = resolve_github_repository(
        root,
        requested_repository=github_repository,
        command_runner=command_runner,
    )
    errors = list(preflight_errors)
    records: list[Mapping[str, Any]] = []
    if resolved_repository is not None:
        records.extend(
            _github_records(
                root,
                resolved_repository,
                command_runner,
                errors,
            )
        )
    records.extend(_jira_records(source_environment, jira_opener, errors))
    return DiscoveryResult(
        tuple(records),
        tuple(dict.fromkeys(errors)),
        resolved_repository,
    )


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
    result_hash = hashlib.sha256(scenario_result_content).hexdigest()
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


def _command_receipt(
    argv: list[str],
    project_root: Path,
    command_runner: Any,
    label: str,
) -> tuple[Optional[subprocess.CompletedProcess], tuple[str, ...]]:
    try:
        completed = command_runner(
            argv,
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None, (f"{label} could not start",)
    if completed.returncode != 0:
        return completed, (
            f"{label} failed with exit code {completed.returncode}",
        )
    return completed, ()


def _publication_state(
    task: Path,
    original: Mapping[str, Any],
    status: str,
    pr_url: Optional[str] = None,
) -> RunResult:
    state = dict(original)
    state["status"] = status
    state["pr_url"] = pr_url
    try:
        _write_state(task, state)
    except OSError as exc:
        return RunResult(
            False,
            (f"cannot persist publication state: {exc}",),
            original,
        )
    return RunResult(status == "pr-opened", (), state)


def _pr_url(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    parsed = urlparse(stripped)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not parsed.path
        or "/pull/" not in parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return None
    return stripped


def _pr_input(task: Path) -> tuple[Optional[str], Optional[Path], tuple[str, ...]]:
    title_path = task / "pr-title.txt"
    body_path = task / "pr-body.md"
    if title_path.is_symlink() or body_path.is_symlink():
        return None, None, ("PR title and body must not be symlinks",)
    try:
        title = title_path.read_text(encoding="utf-8").strip()
        body = body_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return None, None, (f"cannot read PR title or body: {exc}",)
    if not title or "\n" in title or len(title) > 256:
        return None, None, ("PR title must be one non-empty line up to 256 characters",)
    if not body.strip():
        return None, None, ("PR body must be non-empty",)
    return title, body_path, ()


def publish_run(
    task_dir: Path,
    project_root: Path,
    base_branch: str = "main",
    command_runner: Any = subprocess.run,
) -> RunResult:
    task = Path(task_dir)
    root = Path(project_root).resolve(strict=True)
    loaded = _load_state(task)
    if not loaded.allowed:
        return loaded
    if (
        loaded.state["status"] == "pr-opened"
        and _pr_url(loaded.state.get("pr_url")) is not None
    ):
        return RunResult(True, (), loaded.state)
    if loaded.state["status"] != "pr-ready":
        return RunResult(
            False, ("evolution run is not pr-ready",), loaded.state
        )
    if (
        not isinstance(base_branch, str)
        or not base_branch
        or base_branch.startswith("-")
        or any(character.isspace() for character in base_branch)
    ):
        return RunResult(False, ("base branch is invalid",), loaded.state)

    status, errors = _command_receipt(
        ["git", "status", "--porcelain"], root, command_runner, "git status"
    )
    if errors:
        return RunResult(False, errors, loaded.state)
    assert status is not None
    if status.stdout.strip():
        return RunResult(False, ("worktree must be clean",), loaded.state)

    branch_receipt, errors = _command_receipt(
        ["git", "branch", "--show-current"],
        root,
        command_runner,
        "git branch",
    )
    if errors:
        return RunResult(False, errors, loaded.state)
    assert branch_receipt is not None
    branch = branch_receipt.stdout.strip()
    if not branch or branch == base_branch:
        return RunResult(
            False,
            ("publication requires a non-base named branch",),
            loaded.state,
        )
    ahead, errors = _command_receipt(
        ["git", "rev-list", "--count", f"{base_branch}..HEAD"],
        root,
        command_runner,
        "git rev-list",
    )
    if errors:
        return RunResult(False, errors, loaded.state)
    assert ahead is not None
    try:
        ahead_count = int(ahead.stdout.strip())
    except ValueError:
        return RunResult(
            False, ("git rev-list returned an invalid count",), loaded.state
        )
    if ahead_count < 1:
        return RunResult(
            False, ("publication branch has no commits above base",), loaded.state
        )

    completion = validate_completion(task, root)
    if not completion.allowed:
        return RunResult(
            False,
            ("scenario completion is not current and complete",)
            + completion.errors,
            loaded.state,
        )
    title, body_path, errors = _pr_input(task)
    if errors:
        return RunResult(False, errors, loaded.state)
    assert title is not None and body_path is not None

    github_repository, errors = resolve_github_repository(
        root,
        requested_repository=loaded.state.get("github_repository"),
        command_runner=command_runner,
    )
    if errors:
        blocked = _publication_state(
            task, loaded.state, "publish-blocked"
        )
        return RunResult(False, errors, blocked.state)
    assert github_repository is not None

    existing, errors = _command_receipt(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            github_repository,
            "--state",
            "all",
            "--head",
            branch,
            "--base",
            base_branch,
            "--limit",
            "2",
            "--json",
            "url,state,headRefName,baseRefName",
        ],
        root,
        command_runner,
        "GitHub PR lookup",
    )
    if errors:
        return RunResult(False, errors, loaded.state)
    assert existing is not None
    try:
        prs = json.loads(existing.stdout)
    except json.JSONDecodeError:
        return RunResult(
            False, ("GitHub PR lookup returned invalid JSON",), loaded.state
        )
    if not isinstance(prs, list) or any(not isinstance(pr, dict) for pr in prs):
        return RunResult(
            False, ("GitHub PR lookup must return an array of objects",), loaded.state
        )
    exact = [
        pr
        for pr in prs
        if pr.get("headRefName") == branch and pr.get("baseRefName") == base_branch
    ]
    if len(exact) > 1:
        return RunResult(
            False, ("multiple pull requests exist for the exact head and base",), loaded.state
        )
    if exact:
        existing_url = _pr_url(exact[0].get("url"))
        if existing_url is None:
            return RunResult(
                False, ("existing pull request has an invalid URL",), loaded.state
            )
        return _publication_state(task, loaded.state, "pr-opened", existing_url)

    _, errors = _command_receipt(
        ["git", "push", "--set-upstream", "origin", branch],
        root,
        command_runner,
        "git push",
    )
    if errors:
        blocked = _publication_state(
            task, loaded.state, "publish-blocked"
        )
        return RunResult(False, errors, blocked.state)

    created, errors = _command_receipt(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            github_repository,
            "--base",
            base_branch,
            "--head",
            branch,
            "--title",
            title,
            "--body-file",
            str(body_path),
        ],
        root,
        command_runner,
        "GitHub PR creation",
    )
    if errors:
        blocked = _publication_state(
            task, loaded.state, "publish-blocked"
        )
        return RunResult(False, errors, blocked.state)
    assert created is not None
    created_url = _pr_url(created.stdout)
    if created_url is None:
        uncertain = _publication_state(
            task, loaded.state, "publish-uncertain"
        )
        return RunResult(
            False,
            ("GitHub PR creation returned an invalid URL",),
            uncertain.state,
        )
    return _publication_state(task, loaded.state, "pr-opened", created_url)


def _direct_task(
    raw_task: Path, project_root: Path
) -> tuple[Optional[Path], tuple[str, ...]]:
    try:
        root = project_root.resolve(strict=True)
        candidate = raw_task if raw_task.is_absolute() else root / raw_task
        if candidate.is_symlink() or candidate.parent.is_symlink():
            return None, ("task must be a direct _workspace task",)
        task = candidate.resolve(strict=True)
        relative = task.relative_to(root)
    except (OSError, ValueError):
        return None, ("task must be a direct _workspace task",)
    if len(relative.parts) != 2 or relative.parts[0] != "_workspace":
        return None, ("task must be a direct _workspace task",)
    return task, ()


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

    discover = subparsers.add_parser("discover")
    discover.add_argument("--project-root", required=True, type=Path)
    discover.add_argument("--github-repo")
    discover.add_argument("--json", action="store_true")

    start = subparsers.add_parser("start")
    start.add_argument("task", type=Path)
    start.add_argument("--candidate", required=True, type=Path)
    start.add_argument("--project-root", required=True, type=Path)
    start.add_argument("--github-repo")
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

    publish = subparsers.add_parser("publish")
    publish.add_argument("task", type=Path)
    publish.add_argument("--project-root", required=True, type=Path)
    publish.add_argument("--base-branch", default="main")
    publish.add_argument("--json", action="store_true")

    args = parser.parse_args()
    if args.operation == "discover":
        try:
            result = discover_evidence(
                args.project_root,
                github_repository=args.github_repo,
            )
        except OSError as exc:
            payload = {"allowed": False, "errors": [f"cannot discover evidence: {exc}"], "records": []}
            _print_payload(payload, args.json)
            return 1
        payload = {
            "allowed": result.github_repository is not None,
            "errors": list(result.errors),
            "records": [dict(record) for record in result.records],
            "github_repository": result.github_repository,
        }
        _print_payload(payload, args.json)
        return 0 if result.github_repository is not None else 1

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
            validation = validate_candidate(candidate_value)
            approved_records: Sequence[Mapping[str, Any]] = ()
            if not validation.allowed:
                result = RunResult(False, validation.errors, {})
            else:
                github_repository, preflight_errors = resolve_github_repository(
                    args.project_root,
                    requested_repository=args.github_repo,
                )
                if preflight_errors:
                    result = RunResult(False, preflight_errors, {})
                else:
                    if (
                        validation.candidate.get("kind") == "feature"
                        and validation.candidate.get("source")
                        in {"github", "jira"}
                    ):
                        approved_records = discover_evidence(
                            args.project_root,
                            github_repository=github_repository,
                        ).records
                    result = start_run(
                        task,
                        candidate_value,
                        args.max_iterations,
                        approved_records=approved_records,
                        github_repository=github_repository,
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
    elif args.operation == "publish":
        result = publish_run(
            task,
            args.project_root,
            base_branch=args.base_branch,
        )
    else:
        result = _load_state(task)

    _print_payload(_run_payload(result), args.json)
    return 0 if result.allowed else 1


if __name__ == "__main__":
    sys.exit(main())
