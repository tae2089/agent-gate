#!/usr/bin/env python3
"""Managed local persistence for concrete Agent Loop packs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from loop_engine import (
    LoopDefinition,
    LoopResult,
    atomic_write,
    canonical_json,
    content_sha256,
    direct_workspace_task,
    resolve_active_run,
    transition,
    validate_iteration_state,
)

STATE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ManagedLoopDefinition:
    loop: LoopDefinition
    input_filename: str
    state_filename: str
    active_pointer_filename: str | None
    input_hash_field: str
    initial_status: str
    interrupt_terminals: frozenset[str]

    @property
    def state_fields(self) -> frozenset[str]:
        return frozenset(
            {
                "schema_version",
                "status",
                "iteration",
                "max_iterations",
                self.input_hash_field,
            }
        )


def _write_state(
    definition: ManagedLoopDefinition,
    task_dir: Path,
    state: Mapping[str, Any],
) -> None:
    atomic_write(
        task_dir / definition.state_filename,
        canonical_json(state),
    )


def _valid_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def load_managed_run(
    definition: ManagedLoopDefinition,
    task_dir: Path,
) -> LoopResult:
    task = Path(task_dir)
    path = task / definition.state_filename
    name = definition.loop.name
    if path.is_symlink():
        return LoopResult(False, (f"{name} state must not be a symlink",), {})
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return LoopResult(False, (f"cannot read {name} state: {exc}",), {})
    if not isinstance(value, dict):
        return LoopResult(False, (f"{name} state must be an object",), {})

    errors: list[str] = []
    unknown = sorted(value.keys() - definition.state_fields)
    missing = sorted(definition.state_fields - value.keys())
    if unknown:
        errors.append(f"{name} state has unknown fields: {', '.join(unknown)}")
    if missing:
        errors.append(f"{name} state is missing fields: {', '.join(missing)}")
    if value.get("schema_version") != STATE_SCHEMA_VERSION:
        errors.append(f"{name} state schema_version must be {STATE_SCHEMA_VERSION}")
    statuses = set(definition.loop.transitions) | definition.loop.terminal_statuses
    if value.get("status") not in statuses:
        errors.append(f"unsupported {name} status: {value.get('status')}")
    errors.extend(f"{name} state {error}" for error in validate_iteration_state(value))
    if not _valid_sha256(value.get(definition.input_hash_field)):
        errors.append(
            f"{name} state {definition.input_hash_field} must be a lowercase SHA-256"
        )
    input_path = task / definition.input_filename
    if input_path.is_symlink():
        errors.append(f"{name} input must not be a symlink")
    else:
        try:
            input_content = input_path.read_bytes()
        except OSError as exc:
            errors.append(f"cannot read {name} input: {exc}")
        else:
            expected_hash = value.get(definition.input_hash_field)
            if _valid_sha256(expected_hash) and (
                content_sha256(input_content) != expected_hash
            ):
                errors.append(f"{name} input hash is stale")
    return LoopResult(not errors, tuple(dict.fromkeys(errors)), value)


def resolve_managed_run(
    definition: ManagedLoopDefinition,
    project_root: Path,
) -> tuple[Path | None, tuple[str, ...]]:
    if definition.active_pointer_filename is None:
        return None, (f"{definition.loop.name} Subloop does not own a global pointer",)
    return resolve_active_run(
        Path(project_root),
        definition.active_pointer_filename,
        definition.loop.name,
    )


def start_managed_run(
    definition: ManagedLoopDefinition,
    task_dir: Path,
    input_value: Mapping[str, Any],
    max_iterations: int = 3,
) -> LoopResult:
    if definition.active_pointer_filename is None:
        return LoopResult(
            False,
            (f"{definition.loop.name} Subloop must attach to a parent invocation",),
            {},
        )
    if (
        isinstance(max_iterations, bool)
        or not isinstance(max_iterations, int)
        or not 1 <= max_iterations <= 10
    ):
        return LoopResult(
            False,
            ("max_iterations must be an integer from 1 through 10",),
            {},
        )

    task = Path(task_dir)
    if not task.is_dir() or task.is_symlink():
        return LoopResult(
            False,
            ("task directory must be a real directory",),
            {},
        )
    root = task.parent.parent.resolve(strict=True)
    direct_task, direct_errors = direct_workspace_task(task, root)
    if direct_task is None:
        return LoopResult(False, direct_errors, {})
    task = direct_task
    input_path = task / definition.input_filename
    state_path = task / definition.state_filename
    if input_path.exists() or input_path.is_symlink():
        return LoopResult(
            False,
            (f"{definition.loop.name} input already exists",),
            {},
        )
    if state_path.exists() or state_path.is_symlink():
        return LoopResult(
            False,
            (f"{definition.loop.name} state already exists",),
            {},
        )

    pointer = task.parent / definition.active_pointer_filename
    if pointer.exists() or pointer.is_symlink():
        active_task, active_errors = resolve_managed_run(definition, root)
        if active_task is None:
            return LoopResult(False, active_errors, {})
        active = load_managed_run(definition, active_task)
        if not active.allowed:
            return active
        if active.state["status"] in definition.loop.transitions:
            return LoopResult(
                False,
                (f"another {definition.loop.name} run is active",),
                active.state,
            )

    input_content = canonical_json(input_value)
    state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "status": definition.initial_status,
        "iteration": 1,
        "max_iterations": max_iterations,
        definition.input_hash_field: content_sha256(input_content),
    }
    created_paths: list[Path] = []
    try:
        atomic_write(input_path, input_content)
        created_paths.append(input_path)
        _write_state(definition, task, state)
        created_paths.append(state_path)
        relative = task.relative_to(root)
        atomic_write(
            pointer,
            (relative.as_posix() + "\n").encode("utf-8"),
        )
    except OSError as exc:
        cleanup_errors: list[str] = []
        for created_path in reversed(created_paths):
            try:
                created_path.unlink(missing_ok=True)
            except OSError as cleanup_exc:
                cleanup_errors.append(
                    f"cannot remove partial {created_path.name}: {cleanup_exc}"
                )
        errors = [f"cannot start {definition.loop.name} run: {exc}"]
        errors.extend(cleanup_errors)
        return LoopResult(
            False,
            tuple(errors),
            {},
        )
    return LoopResult(True, (), state)


def attach_managed_subloop(
    definition: ManagedLoopDefinition,
    task_dir: Path,
    project_root: Path,
    max_iterations: int = 3,
) -> LoopResult:
    if definition.active_pointer_filename is not None:
        return LoopResult(
            False,
            (f"{definition.loop.name} root run cannot attach as a Subloop",),
            {},
        )
    if (
        isinstance(max_iterations, bool)
        or not isinstance(max_iterations, int)
        or not 1 <= max_iterations <= 10
    ):
        return LoopResult(
            False,
            ("max_iterations must be an integer from 1 through 10",),
            {},
        )
    try:
        root = Path(project_root).resolve(strict=True)
        candidate = Path(task_dir)
        if not candidate.is_absolute():
            candidate = root / candidate
        if candidate.is_symlink() or candidate.parent.is_symlink():
            raise ValueError("Subloop task must not contain symlinks")
        task = candidate.resolve(strict=True)
        relative = task.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        return LoopResult(
            False,
            (f"cannot resolve nested Subloop task: {exc}",),
            {},
        )
    if (
        len(relative.parts) != 4
        or relative.parts[0] != "_workspace"
        or relative.parts[2] != "subloops"
        or not relative.parts[1]
        or not relative.parts[3]
        or not task.is_dir()
    ):
        return LoopResult(
            False,
            ("Subloop task must be nested under _workspace/<root>/subloops/<id>",),
            {},
        )
    input_path = task / definition.input_filename
    state_path = task / definition.state_filename
    if input_path.is_symlink():
        return LoopResult(
            False,
            (f"{definition.loop.name} input must not be a symlink",),
            {},
        )
    try:
        input_content = input_path.read_bytes()
    except OSError as exc:
        return LoopResult(
            False,
            (f"cannot read {definition.loop.name} input: {exc}",),
            {},
        )
    if state_path.exists() or state_path.is_symlink():
        return LoopResult(
            False,
            (f"{definition.loop.name} state already exists",),
            {},
        )
    state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "status": definition.initial_status,
        "iteration": 1,
        "max_iterations": max_iterations,
        definition.input_hash_field: content_sha256(input_content),
    }
    try:
        _write_state(definition, task, state)
    except OSError as exc:
        return LoopResult(
            False,
            (f"cannot attach {definition.loop.name} Subloop: {exc}",),
            {},
        )
    return LoopResult(True, (), state)


def transition_managed_run(
    definition: ManagedLoopDefinition,
    task_dir: Path,
    next_status: str,
) -> LoopResult:
    task = Path(task_dir)
    loaded = load_managed_run(definition, task)
    if not loaded.allowed:
        return loaded
    decision = transition(definition.loop, loaded.state, next_status)
    if not decision.allowed:
        return decision
    try:
        _write_state(definition, task, decision.state)
    except OSError as exc:
        return LoopResult(
            False,
            (f"cannot persist {definition.loop.name} state: {exc}",),
            loaded.state,
        )
    return decision


def terminate_managed_run(
    definition: ManagedLoopDefinition,
    task_dir: Path,
    status: str,
) -> LoopResult:
    task = Path(task_dir)
    loaded = load_managed_run(definition, task)
    if not loaded.allowed:
        return loaded
    state = dict(loaded.state)
    if state["status"] not in definition.loop.transitions:
        return LoopResult(
            False,
            (f"{definition.loop.name} run is already terminal",),
            state,
        )
    if status not in definition.interrupt_terminals:
        return LoopResult(
            False,
            (f"unsupported {definition.loop.name} terminal status: {status}",),
            state,
        )
    state["status"] = status
    try:
        _write_state(definition, task, state)
    except OSError as exc:
        return LoopResult(
            False,
            (f"cannot persist {definition.loop.name} state: {exc}",),
            loaded.state,
        )
    return LoopResult(True, (), state)
