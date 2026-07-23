"""Shared structural design fixtures for two-gate tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

TASK = """# Contract

- AC-1: Protected edits require a structural design.
- AC-2: Completion requires fresh executable scenarios.

# Test Plan

- T-1 [Todo]: exercise the observable gate contract.

# Implementation

- [Todo] Implement the declared behavior.

# Verification

- [Todo] Run the declared scenario.
"""

IMPLEMENTATION = """# Design Approach

- Design: Keep structure validation and completion behind one gate module.
- Assumption: one worktree has one active task.
- Mapping: Implement AC-1 and AC-2 in scripts/scenario_gate.py.
- Change: hooks/design_gate_hook.py validates protected edits.
- Risk: unsafe paths and stale results must fail closed.

# Pseudocode

```text
P1  receive a gate request
P2  IF required structure or results are missing -> block
```

# Flow Diagram

```mermaid
flowchart TD
    A["Receive request"] --> B{"Gate satisfied?"}
    B -- no --> C["Block"]
    B -- yes --> D["Allow"]
```
"""


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
        subprocess.run(
            command,
            cwd=project,
            check=True,
            capture_output=True,
            text=True,
        )
