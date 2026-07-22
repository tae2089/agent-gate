"""Shared project artifacts for scenario-gate integration tests."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from readiness_helpers import CHILD_TASK, inheritance_for, write_ready_artifacts


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def scenario(
    scenario_id: str,
    *,
    acceptance: list[str],
    flow: list[str],
    runner: str,
    observation_id: str | None = None,
) -> dict:
    observation_id = observation_id or f"O-{scenario_id.removeprefix('S-')}"
    return {
        "id": scenario_id,
        "title": f"Observable behavior for {scenario_id}",
        "covers": {"acceptance": acceptance, "flow": flow},
        "runner": runner,
        "given": ["a controlled project state"],
        "when": ["the public operation runs"],
        "then": [
            {
                "id": observation_id,
                "expectation": "an observable result is returned",
            }
        ],
    }


def parent_contract() -> dict:
    return {
        "schema_version": 1,
        "scenarios": [
            scenario(
                "S-ALLOW-READY",
                acceptance=["AC-2"],
                flow=["P1"],
                runner="ready-check",
            ),
            scenario(
                "S-BLOCK-STALE",
                acceptance=["AC-1"],
                flow=["P2"],
                runner="stale-check",
            ),
        ],
    }


def runner_definition(exit_code: int = 0) -> dict:
    return {
        "command": [sys.executable, "-c", f"raise SystemExit({exit_code})"],
        "timeout_seconds": 30,
        "max_output_bytes": 65536,
    }


def write_policy(project: Path, *, runners: dict | None = None, **legacy: object) -> Path:
    if runners is None:
        runners = {
            "ready-check": runner_definition(),
            "stale-check": runner_definition(),
        }
    value = {"schema_version": 1, "runners": runners, **legacy}
    path = project / ".agent-gate" / "scenario-gate.json"
    write_json(path, value)
    return path


def write_parent_scenarios(task_dir: Path, contract: dict | None = None) -> Path:
    contract_path = task_dir / "scenario-contract.json"
    write_json(contract_path, parent_contract() if contract is None else contract)
    return contract_path


def write_parent_project(project: Path, **legacy: object) -> Path:
    task_dir = project / "_workspace" / "parent-task"
    write_ready_artifacts(task_dir)
    write_policy(project, **legacy)
    write_parent_scenarios(task_dir)
    return task_dir


def write_child_project(parent: Path, **obsolete: object) -> Path:
    if obsolete:
        raise TypeError("child scenario overlays are obsolete")
    child = parent.parent / "child-task"
    child.mkdir(parents=True)
    (child / "task.md").write_text(CHILD_TASK, encoding="utf-8")
    write_json(
        child / "inherited-readiness.json",
        inheritance_for(child, parent_task=parent.name),
    )
    return child


def init_git_project(project: Path) -> None:
    source = project / "src" / "app.txt"
    verification = project / "tests" / "scenario.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    verification.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("initial\n", encoding="utf-8")
    verification.write_text("assertion\n", encoding="utf-8")
    commands = (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "scenario@example.invalid"],
        ["git", "config", "user.name", "Scenario Test"],
        ["git", "add", "."],
        ["git", "commit", "-qm", "fixture"],
    )
    for command in commands:
        subprocess.run(command, cwd=project, check=True, capture_output=True, text=True)


def write_passing_evidence(task_dir: Path, project: Path) -> Path:
    scripts = Path(__file__).resolve().parent.parent / "scripts"
    sys.path.insert(0, str(scripts))
    from scenario_gate import evidence_template

    value = evidence_template(task_dir, project)
    for item in value["observations"]:
        item["implementation"] = [{"path": "src/app.txt", "line": 1}]
        item["verification"] = [{"path": "tests/scenario.txt", "line": 1}]
    value["verdict"] = "pass"
    value["blocking_findings"] = []
    path = task_dir / "scenario-evidence.json"
    write_json(path, value)
    return path
