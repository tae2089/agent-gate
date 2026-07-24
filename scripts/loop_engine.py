#!/usr/bin/env python3
"""Deterministic state-transition primitives shared by Agent Loop packs."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class LoopDefinition:
    name: str
    transitions: Mapping[str, frozenset[str]]
    terminal_statuses: frozenset[str]
    iteration_transitions: frozenset[tuple[str, str]]
    budget_terminal: str


@dataclass(frozen=True)
class LoopResult:
    allowed: bool
    errors: tuple[str, ...]
    state: Mapping[str, Any]


def canonical_json(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def content_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def atomic_write(path: Path, content: bytes) -> None:
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


def direct_workspace_task(
    raw_task: Path, project_root: Path
) -> tuple[Path | None, tuple[str, ...]]:
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


def resolve_active_run(
    project_root: Path,
    pointer_filename: str,
    loop_name: str,
) -> tuple[Path | None, tuple[str, ...]]:
    root = Path(project_root).resolve(strict=True)
    pointer = root / "_workspace" / pointer_filename
    if pointer.is_symlink():
        return None, (f"active {loop_name} pointer must not be a symlink",)
    try:
        raw = pointer.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, (f"no active {loop_name} run",)
    except (OSError, UnicodeError) as exc:
        return None, (f"cannot read active {loop_name} pointer: {exc}",)
    if not raw.endswith("\n") or not raw.strip() or "\n" in raw.rstrip("\n"):
        return None, (f"active {loop_name} pointer is malformed",)
    return direct_workspace_task(Path(raw.strip()), root)


def validate_iteration_state(
    current_state: Mapping[str, Any],
) -> tuple[str, ...]:
    errors: list[str] = []
    iteration = current_state.get("iteration")
    max_iterations = current_state.get("max_iterations")
    if (
        isinstance(iteration, bool)
        or not isinstance(iteration, int)
        or iteration < 1
    ):
        errors.append("iteration must be a positive integer")
    if (
        isinstance(max_iterations, bool)
        or not isinstance(max_iterations, int)
        or not 1 <= max_iterations <= 10
    ):
        errors.append("max_iterations must be an integer from 1 through 10")
    if (
        isinstance(iteration, int)
        and not isinstance(iteration, bool)
        and isinstance(max_iterations, int)
        and not isinstance(max_iterations, bool)
        and iteration > max_iterations
    ):
        errors.append("iteration exceeds max_iterations")
    return tuple(errors)


def transition(
    definition: LoopDefinition,
    current_state: Mapping[str, Any],
    next_status: str,
) -> LoopResult:
    """Return the next immutable run state allowed by ``definition``."""

    state = dict(current_state)
    errors = validate_iteration_state(state)
    iteration = state.get("iteration")
    max_iterations = state.get("max_iterations")
    if errors:
        return LoopResult(False, errors, state)

    status = state.get("status")
    if status in definition.terminal_statuses:
        return LoopResult(
            False,
            (f"terminal {definition.name} state cannot transition",),
            state,
        )
    allowed_targets = definition.transitions.get(status)
    if allowed_targets is None or next_status not in allowed_targets:
        return LoopResult(
            False,
            (
                f"{definition.name} transition "
                f"{status} -> {next_status} is not allowed",
            ),
            state,
        )

    if (status, next_status) in definition.iteration_transitions:
        assert isinstance(iteration, int) and not isinstance(iteration, bool)
        assert isinstance(max_iterations, int) and not isinstance(
            max_iterations, bool
        )
        if iteration >= max_iterations:
            state["status"] = definition.budget_terminal
            return LoopResult(True, (), state)
        state["iteration"] = iteration + 1
    state["status"] = next_status
    return LoopResult(True, (), state)
