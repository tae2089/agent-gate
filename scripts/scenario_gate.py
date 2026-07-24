#!/usr/bin/env python3
"""Structural Design Gate and executable Completion Gate."""

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

from artifact_lint import lint_file

SCHEMA_VERSION = 1
RESULT_SCHEMA_VERSION = 1
CONTRACT_FILENAME = "scenario-contract.json"
RESULT_FILENAME = "scenario-result.json"
ACTIVE_TASK_RELATIVE = Path("_workspace/.active-task")
SCENARIO_ID = re.compile(r"S-[A-Z0-9][A-Z0-9-]*")
CONTRACT_FIELDS = frozenset({"schema_version", "scenarios"})
SCENARIO_FIELDS = frozenset({"id", "title", "command", "given", "when", "then"})
RESULT_FIELDS = frozenset(
    {
        "schema_version",
        "task_sha256",
        "flow_sha256",
        "contract_sha256",
        "source_fingerprint",
        "results",
    }
)
RESULT_ITEM_FIELDS = frozenset({"id", "status", "duration_ms", "reason"})
RESULT_STATUSES = frozenset({"passed", "failed", "infrastructure-error"})
RUNNER_TIMEOUT_SECONDS = 300
RUNNER_MAX_OUTPUT_BYTES = 1_048_576
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
class ScenarioTraceCompleteness:
    required: int
    passed: int
    percentage: float
    current: bool


@dataclass(frozen=True)
class ScenarioGateResult:
    allowed: bool
    errors: tuple[str, ...]
    required_scenarios: tuple[str, ...]
    trace_completeness: ScenarioTraceCompleteness | None = None


@dataclass(frozen=True)
class ScenarioRunResult:
    result_written: bool
    errors: tuple[str, ...]
    result_path: Path | None


@dataclass(frozen=True)
class _Design:
    task_dir: Path
    task_content: bytes
    flow_content: bytes
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


def _string_list(value: Any, label: str, errors: list[str]) -> None:
    if not isinstance(value, list) or not value:
        errors.append(f"{label} must be a non-empty list")
        return
    if any(not isinstance(item, str) or not item.strip() for item in value):
        errors.append(f"{label} contains an invalid string")


def _project_task_dir(
    project_root: Path, task_dir: Path, errors: list[str]
) -> Path | None:
    try:
        root = project_root.resolve(strict=True)
        candidate = task_dir if task_dir.is_absolute() else root / task_dir
        if candidate.is_symlink() or candidate.parent.is_symlink():
            errors.append("task directory must not be a symlink")
            return None
        task = candidate.resolve(strict=True)
        relative = task.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        errors.append(f"task must be inside the project _workspace: {exc}")
        return None
    if (
        len(relative.parts) != 2
        or relative.parts[0] != "_workspace"
        or not relative.parts[1]
        or relative.parts[1].startswith(".")
        or not task.is_dir()
    ):
        errors.append("task must be a direct _workspace/<task> directory")
        return None
    return task


def _load_contract(
    task_dir: Path, errors: list[str]
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

    scenarios: list[dict[str, Any]] = []
    ids: list[str] = []
    for index, raw in enumerate(raw_scenarios):
        label = f"scenario[{index}]"
        scenario = _object(raw, label, errors)
        _fields(scenario, SCENARIO_FIELDS, SCENARIO_FIELDS, label, errors)
        scenario_id = scenario.get("id")
        if not isinstance(scenario_id, str) or SCENARIO_ID.fullmatch(scenario_id) is None:
            errors.append(f"{label}.id must match {SCENARIO_ID.pattern}")
        else:
            ids.append(scenario_id)
        title = scenario.get("title")
        if not isinstance(title, str) or not title.strip():
            errors.append(f"{label}.title must be a non-empty string")
        command = scenario.get("command")
        if (
            not isinstance(command, list)
            or not command
            or any(
                not isinstance(argument, str) or not argument or "\0" in argument
                for argument in command
            )
        ):
            errors.append(
                f"{label}.command must be a non-empty string array without NUL bytes"
            )
        for field in ("given", "when", "then"):
            _string_list(scenario.get(field), f"{label}.{field}", errors)
        scenarios.append(scenario)
    duplicates = sorted({scenario_id for scenario_id in ids if ids.count(scenario_id) > 1})
    if duplicates:
        errors.append("duplicate scenario id: " + ", ".join(duplicates))
    return tuple(scenarios), content


def _resolve_design(
    task_dir: Path | str, project_root: Path | str
) -> tuple[_Design | None, tuple[str, ...]]:
    errors: list[str] = []
    task = _project_task_dir(Path(project_root), Path(task_dir), errors)
    if task is None:
        return None, tuple(errors)
    for filename, artifact_type in (
        ("task.md", "task"),
        ("implementation.md", "implementation"),
    ):
        result = lint_file(task / filename, artifact_type)
        if result is None:
            errors.append(f"cannot lint {filename}")
        elif not result["passed"]:
            failed = [key for key, passed in result["checks"].items() if not passed]
            errors.append(f"{filename} fails structural lint: {', '.join(failed)}")
    scenarios, contract_content = _load_contract(task, errors)
    try:
        task_content = (task / "task.md").read_bytes()
        flow_content = (task / "implementation.md").read_bytes()
    except OSError as exc:
        errors.append(f"cannot read design artifacts: {exc}")
        task_content = b""
        flow_content = b""
    if errors:
        return None, tuple(errors)
    return (
        _Design(task, task_content, flow_content, contract_content, scenarios),
        (),
    )


def validate_design(
    task_dir: Path | str, project_root: Path | str
) -> ScenarioGateResult:
    design, errors = _resolve_design(task_dir, project_root)
    required = (
        tuple(scenario["id"] for scenario in design.scenarios)
        if design is not None
        else ()
    )
    return ScenarioGateResult(design is not None, errors, required)


def resolve_active_task(
    project_root: Path | str,
) -> tuple[Path | None, tuple[str, ...]]:
    try:
        root = Path(project_root).resolve(strict=True)
    except (OSError, RuntimeError):
        return None, ("project root is unavailable",)
    pointer = root / ACTIVE_TASK_RELATIVE
    if not pointer.exists() and not pointer.is_symlink():
        return None, ()
    if pointer.is_symlink():
        return None, ("active task pointer must not be a symlink",)
    try:
        raw = pointer.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        return None, (f"cannot read active task pointer: {exc}",)
    relative = Path(raw)
    if (
        relative.is_absolute()
        or len(relative.parts) != 2
        or relative.parts[0] != "_workspace"
        or not relative.parts[1]
        or relative.parts[1].startswith(".")
    ):
        return None, ("active task pointer must name _workspace/<task>",)
    errors: list[str] = []
    task = _project_task_dir(root, root / relative, errors)
    return task, tuple(errors)


def _atomic_write_text(path: Path, value: str) -> None:
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
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
            temporary = Path(stream.name)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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


def activate_design(
    task_dir: Path | str, project_root: Path | str
) -> ScenarioGateResult:
    result = validate_design(task_dir, project_root)
    if not result.allowed:
        return result
    root = Path(project_root).resolve(strict=True)
    task = Path(task_dir)
    if not task.is_absolute():
        task = root / task
    relative = task.resolve(strict=True).relative_to(root)
    pointer = root / ACTIVE_TASK_RELATIVE
    try:
        pointer.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(pointer, relative.as_posix() + "\n")
    except (OSError, RuntimeError, ValueError) as exc:
        return ScenarioGateResult(
            False,
            (f"cannot activate design: {exc}",),
            result.required_scenarios,
        )
    return result


def deactivate_design(
    task_dir: Path | str, project_root: Path | str
) -> tuple[str, ...]:
    root = Path(project_root).resolve(strict=True)
    active, active_errors = resolve_active_task(root)
    if active_errors:
        return active_errors
    errors: list[str] = []
    task = _project_task_dir(root, Path(task_dir), errors)
    if task is None:
        return tuple(errors)
    if active is None or active != task:
        return ("cannot finish a task that is not the active design",)
    pointer = root / ACTIVE_TASK_RELATIVE
    try:
        if pointer.is_symlink():
            raise OSError("active task pointer must not be a symlink")
        pointer.unlink()
    except OSError as exc:
        return (f"cannot deactivate design: {exc}",)
    return ()


def _git_output(
    root: Path, arguments: tuple[str, ...]
) -> tuple[bytes | None, str | None]:
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
        if output.tell() > max_bytes:
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
        ("diff", "--binary", "--no-ext-diff", "HEAD", "--", ".", ":(exclude)_workspace/**"),
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


def _execute_scenario(root: Path, scenario: dict[str, Any]) -> dict[str, Any]:
    environment = {key: value for key, value in os.environ.items() if key in SAFE_ENVIRONMENT}
    started = time.monotonic()
    timed_out = False
    output_exceeded = False
    launch_error: OSError | None = None
    return_code = 1
    with tempfile.TemporaryFile() as output:
        try:
            process = subprocess.Popen(
                tuple(scenario["command"]),
                cwd=root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=output,
                shell=False,
                start_new_session=os.name == "posix",
            )
            deadline = started + RUNNER_TIMEOUT_SECONDS
            while True:
                polled = process.poll()
                if polled is not None:
                    return_code = polled
                    break
                if os.fstat(output.fileno()).st_size > RUNNER_MAX_OUTPUT_BYTES:
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

    item: dict[str, Any] = {"id": scenario["id"], "duration_ms": duration_ms}
    if launch_error is not None:
        item.update(status="infrastructure-error", reason=f"runner launch failed: {launch_error}")
    elif timed_out:
        item.update(
            status="infrastructure-error",
            reason=f"runner timed out after {RUNNER_TIMEOUT_SECONDS}s",
        )
    elif output_exceeded or output_size > RUNNER_MAX_OUTPUT_BYTES:
        item.update(
            status="infrastructure-error",
            reason=f"runner output exceeded {RUNNER_MAX_OUTPUT_BYTES} bytes",
        )
    elif return_code == 0:
        item["status"] = "passed"
    else:
        item.update(status="failed", reason=f"runner exited with code {return_code}")
    return item


def run_scenarios(
    task_dir: Path | str, project_root: Path | str
) -> ScenarioRunResult:
    root = Path(project_root).resolve(strict=True)
    design, design_errors = _resolve_design(task_dir, root)
    if design is None:
        return ScenarioRunResult(False, design_errors, None)
    results = [_execute_scenario(root, scenario) for scenario in design.scenarios]
    fingerprint, fingerprint_errors = _source_fingerprint(root)
    if fingerprint is None:
        return ScenarioRunResult(False, fingerprint_errors, None)
    value = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "task_sha256": _sha256(design.task_content),
        "flow_sha256": _sha256(design.flow_content),
        "contract_sha256": _sha256(design.contract_content),
        "source_fingerprint": fingerprint,
        "results": results,
    }
    path = design.task_dir / RESULT_FILENAME
    try:
        _atomic_write_json(path, value)
    except OSError as exc:
        return ScenarioRunResult(False, (f"cannot write {RESULT_FILENAME}: {exc}",), None)
    return ScenarioRunResult(True, (), path)


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
            errors.append(f"{label}.id is not a declared scenario")
            valid = False
            continue
        if scenario_id in by_id:
            errors.append(f"scenario result has duplicate scenario id: {scenario_id}")
            valid = False
            continue
        if item.get("status") not in RESULT_STATUSES:
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


def _validate_current_result(
    task_dir: Path | str, project_root: Path | str
) -> tuple[ScenarioGateResult, dict[str, dict[str, Any]]]:
    root = Path(project_root).resolve(strict=True)
    design, design_errors = _resolve_design(task_dir, root)
    if design is None:
        return ScenarioGateResult(False, design_errors, ()), {}
    required = tuple(scenario["id"] for scenario in design.scenarios)
    errors: list[str] = []
    loaded = _load_json(design.task_dir / RESULT_FILENAME, RESULT_FILENAME, errors)
    fingerprint, fingerprint_errors = _source_fingerprint(root)
    errors.extend(fingerprint_errors)
    current = loaded is not None and fingerprint is not None
    result: dict[str, Any] = {}
    if loaded is not None:
        result, _ = loaded
        current = _fields(result, RESULT_FIELDS, RESULT_FIELDS, "scenario result", errors) and current
        if result.get("schema_version") != RESULT_SCHEMA_VERSION:
            errors.append(f"scenario result schema_version must be {RESULT_SCHEMA_VERSION}")
            current = False
        expected_hashes = {
            "task_sha256": _sha256(design.task_content),
            "flow_sha256": _sha256(design.flow_content),
            "contract_sha256": _sha256(design.contract_content),
            "source_fingerprint": fingerprint,
        }
        for field, expected in expected_hashes.items():
            if result.get(field) != expected:
                errors.append(f"scenario result {field} is stale")
                current = False
    by_id, items_valid = _stored_results(result.get("results"), required, errors)
    current = current and items_valid
    passed = {
        scenario_id
        for scenario_id, item in by_id.items()
        if item.get("status") == "passed"
    }
    completed = passed if items_valid else set()
    percentage = round((len(completed) / len(required)) * 100, 2) if required else 0.0
    trace = ScenarioTraceCompleteness(
        len(required),
        len(completed),
        percentage,
        current,
    )
    return (
        ScenarioGateResult(
            not errors,
            tuple(dict.fromkeys(errors)),
            required,
            trace,
        ),
        by_id,
    )


def validate_current_result(
    task_dir: Path | str, project_root: Path | str
) -> ScenarioGateResult:
    result, _ = _validate_current_result(task_dir, project_root)
    return result


def validate_completion(
    task_dir: Path | str, project_root: Path | str
) -> ScenarioGateResult:
    current, by_id = _validate_current_result(task_dir, project_root)
    errors = list(current.errors)
    for scenario_id in current.required_scenarios:
        item = by_id.get(scenario_id, {})
        if item.get("status") != "passed":
            errors.append(f"required scenario did not pass: {scenario_id}")
    trace = current.trace_completeness
    if trace is None or trace.percentage != 100.0:
        percentage = trace.percentage if trace is not None else 0.0
        errors.append(
            "scenario trace completeness must be 100%, "
            f"got {percentage:.2f}%"
        )
    return ScenarioGateResult(
        not errors,
        tuple(dict.fromkeys(errors)),
        current.required_scenarios,
        trace,
    )


def _gate_json(result: ScenarioGateResult) -> dict[str, Any]:
    return {
        "allowed": result.allowed,
        "errors": list(result.errors),
        "required_scenarios": list(result.required_scenarios),
        "trace_completeness": (
            asdict(result.trace_completeness)
            if result.trace_completeness is not None
            else None
        ),
    }


def _print_gate(result: ScenarioGateResult, as_json: bool) -> None:
    if as_json:
        print(json.dumps(_gate_json(result), ensure_ascii=False, sort_keys=True))
        return
    print("PASS" if result.allowed else "BLOCK")
    if result.trace_completeness is not None:
        print(f"  scenario trace completeness: {result.trace_completeness.percentage:.2f}%")
    for error in result.errors:
        print(f"  error: {error}")


def _selected_task(raw_task: Path | None, root: Path) -> tuple[Path | None, tuple[str, ...]]:
    if raw_task is not None:
        errors: list[str] = []
        return _project_task_dir(root, raw_task, errors), tuple(errors)
    task, errors = resolve_active_task(root)
    if task is None and not errors:
        return None, ("no active design",)
    return task, errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    design_command = subparsers.add_parser("design")
    design_command.add_argument("task_dir", type=Path)
    design_command.add_argument("--project-root", required=True, type=Path)
    design_command.add_argument("--activate", action="store_true")
    design_command.add_argument("--json", action="store_true")
    for name in ("run", "completion"):
        command = subparsers.add_parser(name)
        command.add_argument("task_dir", type=Path, nargs="?")
        command.add_argument("--project-root", required=True, type=Path)
        command.add_argument("--json", action="store_true")
        if name == "completion":
            command.add_argument("--finish", action="store_true")
    args = parser.parse_args()

    if args.operation == "design":
        result = (
            activate_design(args.task_dir, args.project_root)
            if args.activate
            else validate_design(args.task_dir, args.project_root)
        )
        _print_gate(result, args.json)
        return 0 if result.allowed else 1

    root = args.project_root.resolve(strict=True)
    task, task_errors = _selected_task(args.task_dir, root)
    if task is None:
        result = ScenarioGateResult(False, task_errors, ())
        _print_gate(result, args.json)
        return 1
    if args.operation == "completion":
        result = validate_completion(task, root)
        if result.allowed and args.finish:
            finish_errors = deactivate_design(task, root)
            if finish_errors:
                result = ScenarioGateResult(
                    False,
                    finish_errors,
                    result.required_scenarios,
                    result.trace_completeness,
                )
        _print_gate(result, args.json)
        return 0 if result.allowed else 1

    run = run_scenarios(task, root)
    if not run.result_written:
        if args.json:
            print(json.dumps({"result_written": False, "errors": list(run.errors)}))
        else:
            print("RUNNER_ERROR")
            for error in run.errors:
                print(f"  error: {error}")
        return 1
    completion = validate_completion(task, root)
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
