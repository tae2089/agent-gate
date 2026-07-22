"""Shared project artifacts for scenario-gate integration tests."""

from __future__ import annotations

import hashlib
import json
import subprocess
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
    risk: str = "standard",
    runner: str = "integration",
) -> dict:
    return {
        "id": scenario_id,
        "title": f"Observable behavior for {scenario_id}",
        "covers": {"acceptance": acceptance, "flow": flow},
        "risk": risk,
        "level": "integration",
        "runner": runner,
        "given": ["a controlled project state"],
        "when": ["the public operation runs"],
        "then": ["an observable result is returned"],
    }


def parent_contract() -> dict:
    return {
        "schema_version": 1,
        "scenarios": [
            scenario(
                "S-ALLOW-READY",
                acceptance=["AC-2"],
                flow=["P1"],
            ),
            scenario(
                "S-BLOCK-STALE",
                acceptance=["AC-1"],
                flow=["P2"],
                risk="critical",
                runner="critical",
            ),
        ],
    }


def review_for(task_dir: Path, subject_path: Path, scenario_ids: list[str]) -> dict:
    implementation = task_dir / "implementation.md"
    return {
        "schema_version": 1,
        "task_sha256": digest(task_dir / "task.md"),
        "flow_sha256": digest(implementation),
        "subject_sha256": digest(subject_path),
        "parent_contract_sha256": "",
        "runner_config_sha256": digest(
            task_dir.parent.parent / ".agent-gate" / "scenario-gate.json"
        ),
        "reviewed_scenarios": scenario_ids,
        "verdict": "pass",
        "blocking_findings": [],
    }


def write_policy(project: Path, *, mode: str = "enforce", runners: dict | None = None) -> Path:
    if runners is None:
        definition = {
            "command": ["python3", "-c", "raise SystemExit(0)"],
            "format": "exit-code",
            "timeout_seconds": 30,
            "max_output_bytes": 65536,
        }
        runners = {"integration": definition, "critical": definition}
    path = project / ".agent-gate" / "scenario-gate.json"
    write_json(path, {"schema_version": 1, "mode": mode, "runners": runners})
    return path


def write_parent_scenarios(task_dir: Path, contract: dict | None = None) -> Path:
    value = parent_contract() if contract is None else contract
    contract_path = task_dir / "scenario-contract.json"
    write_json(contract_path, value)
    scenario_ids = [item["id"] for item in value.get("scenarios", [])]
    write_json(
        task_dir / "scenario-review.json",
        review_for(task_dir, contract_path, scenario_ids),
    )
    return contract_path


def write_parent_project(project: Path, *, mode: str = "enforce") -> Path:
    task_dir = project / "_workspace" / "parent-task"
    write_ready_artifacts(task_dir)
    write_policy(project, mode=mode)
    write_parent_scenarios(task_dir)
    return task_dir


def write_child_scenarios(parent: Path, child: Path, *, ownership: str = "child") -> Path:
    contract_path = parent / "scenario-contract.json"
    overlay = {
        "schema_version": 1,
        "parent_task": parent.name,
        "parent_contract_sha256": digest(contract_path),
        "inherited_scenarios": ["S-BLOCK-STALE"],
        "local_scenarios": [
            {
                **scenario(
                    "S-CHILD-LOCAL",
                    acceptance=["AC-1"],
                    flow=["P1"],
                ),
                "ownership": ownership,
            }
        ],
    }
    overlay_path = child / "scenario-overlay.json"
    write_json(overlay_path, overlay)
    review = {
        "schema_version": 1,
        "task_sha256": digest(child / "task.md"),
        "flow_sha256": digest(parent / "implementation.md"),
        "subject_sha256": digest(overlay_path),
        "parent_contract_sha256": digest(contract_path),
        "runner_config_sha256": digest(
            child.parent.parent / ".agent-gate" / "scenario-gate.json"
        ),
        "reviewed_scenarios": ["S-BLOCK-STALE", "S-CHILD-LOCAL"],
        "verdict": "pass",
        "blocking_findings": [],
    }
    write_json(child / "scenario-review.json", review)
    return overlay_path


def write_child_project(parent: Path, *, ownership: str = "child") -> Path:
    child = parent.parent / "child-task"
    child.mkdir(parents=True)
    (child / "task.md").write_text(CHILD_TASK, encoding="utf-8")
    write_json(
        child / "inherited-readiness.json",
        inheritance_for(child, parent_task=parent.name),
    )
    write_child_scenarios(parent, child, ownership=ownership)
    return child


def init_git_project(project: Path) -> None:
    source = project / "src" / "app.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("initial\n", encoding="utf-8")
    commands = (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "scenario@example.invalid"],
        ["git", "config", "user.name", "Scenario Test"],
        ["git", "add", "."],
        ["git", "commit", "-qm", "fixture"],
    )
    for command in commands:
        subprocess.run(command, cwd=project, check=True, capture_output=True, text=True)
