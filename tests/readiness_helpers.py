"""Shared valid readiness artifacts for validator and hook tests."""

import hashlib
import json
from pathlib import Path

TASK = """# Contract

- AC-1: Guarded source edits require a fresh readiness assessment.
- AC-2: State artifacts stay writable before readiness.
- Acceptance: The gate blocks an unbound source edit.
- Grounding: Claude and Codex direct edit hooks are the affected runtimes.

# Test Plan

- T-1 [Todo]: prove unbound and stale sessions are blocked.

# Implementation

- [Todo] Add deterministic validation and a thin hook adapter.

# Verification

- [Todo] Run focused and full repository tests.
"""

IMPLEMENTATION = """# Design Approach

- design: Use scripts/readiness_gate.py as the only validation module.
- assumption: hook inputs expose a stable session identifier and direct file path.
- Mapping: Implement AC-1 and AC-2 in hooks/readiness_gate_hook.py.
- Change: hooks/readiness_gate_hook.py receives normalized direct-edit events.
- risk: stale assessments could authorize an edit unless both artifact hashes are checked.

# Pseudocode

```text
P1  receive a normalized direct-edit event
P2  IF no fresh assessment exists -> block the edit
```

# Flow Diagram

```mermaid
flowchart TD
    A["Receive direct edit"] --> B{"Fresh assessment?"}
    B -- no --> C["Block edit"]
    B -- yes --> D["Allow edit"]
```
"""

CHILD_TASK = """# Contract

- AC-1: This child implements only the guarded-edit portion of its parent flow.
- Acceptance: The child remains governed by the parent's ready design.
- Grounding: Parent flow references identify the assigned implementation steps.

# Test Plan

- T-1 [Todo]: prove inherited governance remains ready and fresh.

# Implementation

- [Todo] Implement only the referenced parent-flow steps.

# Verification

- [Todo] Run the child-focused tests and the parent acceptance tests.
"""


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_artifacts(task_dir: Path) -> None:
    task_dir.mkdir(parents=True)
    (task_dir / "task.md").write_text(TASK, encoding="utf-8")
    (task_dir / "implementation.md").write_text(IMPLEMENTATION, encoding="utf-8")


def write_ready_artifacts(task_dir: Path) -> None:
    write_artifacts(task_dir)
    (task_dir / "assessment.json").write_text(
        json.dumps(assessment_for(task_dir)), encoding="utf-8"
    )


def inheritance_for(child_dir: Path, parent_task="parent-task", **overrides) -> dict:
    value = {
        "schema_version": 1,
        "mode": "inherit-full",
        "parent_task": parent_task,
        "child_task_sha256": digest(child_dir / "task.md"),
        "flow_refs": ["P1", "P2"],
        "acceptance_refs": ["AC-1"],
    }
    value.update(overrides)
    return value


def assessment_for(task_dir: Path) -> dict:
    return {
        "schema_version": 1,
        "task": {
            "sha256": digest(task_dir / "task.md"),
            "dimensions": {
                "outcome_clarity": {
                    "score": 0.9,
                    "evidence": "AC-1: Guarded source edits require a fresh readiness assessment.",
                },
                "constraint_clarity": {
                    "score": 0.9,
                    "evidence": "AC-2: State artifacts stay writable before readiness.",
                },
                "acceptance_clarity": {
                    "score": 0.9,
                    "evidence": "Acceptance: The gate blocks an unbound source edit.",
                },
                "grounding_clarity": {
                    "score": 0.9,
                    "evidence": "Grounding: Claude and Codex direct edit hooks are the affected runtimes.",
                },
            },
            "blocking_unknowns": [],
        },
        "implementation": {
            "sha256": digest(task_dir / "implementation.md"),
            "dimensions": {
                "decision_closure": {
                    "score": 0.9,
                    "evidence": "design: Use scripts/readiness_gate.py as the only validation module.",
                },
                "change_specificity": {
                    "score": 0.9,
                    "evidence": "Change: hooks/readiness_gate_hook.py receives normalized direct-edit events.",
                },
                "risk_response": {
                    "score": 0.9,
                    "evidence": "risk: stale assessments could authorize an edit unless both artifact hashes are checked.",
                },
            },
            "unresolved_decisions": [],
        },
    }
