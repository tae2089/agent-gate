"""Observable scenario completion contracts."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from readiness_helpers import CHILD_TASK, inheritance_for, write_ready_artifacts
from scenario_helpers import (
    init_git_project,
    parent_contract,
    runner_definition,
    write_json,
    write_parent_project,
    write_policy,
)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from scenario_gate import (  # noqa: E402
    run_scenarios,
    validate_completion,
    validate_readiness,
)


def write_full_project(project: Path, *, exit_codes: tuple[int, int] = (0, 0)) -> Path:
    task = write_parent_project(project)
    if exit_codes != (0, 0):
        write_policy(
            project,
            runners={
                "ready-check": runner_definition(exit_codes[0]),
                "stale-check": runner_definition(exit_codes[1]),
            },
        )
    return task


class ObservableScenarioContractTest(unittest.TestCase):
    def test_contract_is_ready_without_rollout_or_review_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            task = project / "_workspace" / "full-task"
            write_ready_artifacts(task)
            write_json(
                project / ".agent-gate" / "scenario-gate.json",
                {
                    "schema_version": 1,
                    "runners": {
                        "observable-check": {
                            "command": [sys.executable, "-c", "raise SystemExit(0)"]
                        }
                    },
                },
            )
            write_json(
                task / "scenario-contract.json",
                {
                    "schema_version": 1,
                    "scenarios": [
                        {
                            "id": "S-OBSERVABLE",
                            "title": "Observable completion",
                            "covers": {
                                "acceptance": ["AC-1", "AC-2"],
                                "flow": ["P1", "P2"],
                            },
                            "runner": "observable-check",
                            "given": ["a ready task"],
                            "when": ["completion is evaluated"],
                            "then": ["completion is observably allowed"],
                        }
                    ],
                },
            )

            result = validate_readiness(task, project)

            self.assertTrue(result.allowed, result.errors)
            self.assertEqual(result.required_scenarios, ("S-OBSERVABLE",))

    def test_shared_runner_and_legacy_policy_fields_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            task = write_full_project(project)
            value = parent_contract()
            value["scenarios"][1]["runner"] = "ready-check"
            value["scenarios"][0]["risk"] = "critical"
            write_json(task / "scenario-contract.json", value)
            policy_path = project / ".agent-gate" / "scenario-gate.json"
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
            policy["mode"] = "enforce"
            write_json(policy_path, policy)

            result = validate_readiness(task, project)

            self.assertFalse(result.allowed)
            self.assertTrue(any("unknown fields: mode" in error for error in result.errors))


class ScenarioTraceCompletenessTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.task = write_full_project(self.project)
        init_git_project(self.project)

    def tearDown(self):
        self.temp.cleanup()

    def test_completion_reports_100_percent_without_scenario_evidence(self):
        run = run_scenarios(self.task, self.project)
        self.assertTrue(run.result_written, run.errors)
        self.assertFalse((self.task / "scenario-evidence.json").exists())
        stored = json.loads((self.task / "scenario-result.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["schema_version"], 2)

        result = validate_completion(self.task, self.project)

        self.assertTrue(result.allowed, result.errors)
        self.assertIsNotNone(result.trace_completeness)
        assert result.trace_completeness is not None
        self.assertEqual(result.trace_completeness.required, 2)
        self.assertEqual(result.trace_completeness.passed, 2)
        self.assertEqual(result.trace_completeness.percentage, 100.0)

    def test_legacy_result_schema_requires_rerun(self):
        self.assertTrue(run_scenarios(self.task, self.project).result_written)
        path = self.task / "scenario-result.json"
        stored = json.loads(path.read_text(encoding="utf-8"))
        stored["schema_version"] = 1
        write_json(path, stored)

        result = validate_completion(self.task, self.project)

        self.assertFalse(result.allowed)
        self.assertTrue(any("schema_version must be 2" in error for error in result.errors))

    def test_failed_scenario_blocks_and_lowers_trace_completeness(self):
        policy_path = self.project / ".agent-gate" / "scenario-gate.json"
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        policy["runners"]["stale-check"]["command"] = [
            sys.executable,
            "-c",
            "raise SystemExit(7)",
        ]
        write_json(policy_path, policy)
        self.assertTrue(run_scenarios(self.task, self.project).result_written)

        result = validate_completion(self.task, self.project)

        self.assertFalse(result.allowed)
        assert result.trace_completeness is not None
        self.assertEqual(result.trace_completeness.passed, 1)
        self.assertEqual(result.trace_completeness.percentage, 50.0)
        self.assertTrue(any("S-BLOCK-STALE" in error for error in result.errors))

    def test_source_change_invalidates_execution(self):
        self.assertTrue(run_scenarios(self.task, self.project).result_written)
        (self.project / "src" / "app.txt").write_text("changed\n", encoding="utf-8")

        result = validate_completion(self.task, self.project)

        self.assertFalse(result.allowed)
        self.assertTrue(any("source_fingerprint is stale" in error for error in result.errors))
        assert result.trace_completeness is not None
        self.assertEqual(result.trace_completeness.passed, 0)

    def test_task_and_flow_changes_invalidate_execution_result(self):
        self.assertTrue(run_scenarios(self.task, self.project).result_written)
        for filename, fragment in (
            ("task.md", "task_sha256 is stale"),
            ("implementation.md", "flow_sha256 is stale"),
        ):
            with self.subTest(filename=filename):
                path = self.task / filename
                original = path.read_text(encoding="utf-8")
                path.write_text(original + "\n", encoding="utf-8")
                result = validate_completion(self.task, self.project)
                self.assertFalse(result.allowed)
                self.assertTrue(
                    any(fragment in error for error in result.errors),
                    result.errors,
                )
                path.write_text(original, encoding="utf-8")

    def test_obsolete_scenario_evidence_file_is_ignored(self):
        (self.task / "scenario-evidence.json").write_text("not json", encoding="utf-8")
        self.assertTrue(run_scenarios(self.task, self.project).result_written)

        result = validate_completion(self.task, self.project)

        self.assertTrue(result.allowed, result.errors)


class ScenarioExecutionIsolationTest(unittest.TestCase):
    def test_each_scenario_keeps_its_own_runner_result(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            task = write_full_project(project, exit_codes=(0, 7))
            init_git_project(project)

            run = run_scenarios(task, project)
            result = json.loads((task / "scenario-result.json").read_text(encoding="utf-8"))

            self.assertTrue(run.result_written, run.errors)
            self.assertEqual(
                {item["id"]: item["status"] for item in result["results"]},
                {"S-ALLOW-READY": "passed", "S-BLOCK-STALE": "failed"},
            )
            completion = validate_completion(task, project)
            self.assertFalse(completion.allowed)
            assert completion.trace_completeness is not None
            self.assertEqual(completion.trace_completeness.passed, 1)


class ParentCompletionBoundaryTest(unittest.TestCase):
    def test_child_uses_parent_contract_and_result(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            parent = write_full_project(project)
            child = parent.parent / "child-task"
            child.mkdir(parents=True)
            (child / "task.md").write_text(CHILD_TASK, encoding="utf-8")
            write_json(
                child / "inherited-readiness.json",
                inheritance_for(child, parent_task=parent.name),
            )
            init_git_project(project)

            run = run_scenarios(child, project)
            completion = validate_completion(child, project)

            self.assertTrue(run.result_written, run.errors)
            self.assertEqual(run.result_path, (parent / "scenario-result.json").resolve())
            self.assertFalse((child / "scenario-result.json").exists())
            self.assertTrue(completion.allowed, completion.errors)
            self.assertEqual(
                completion.required_scenarios,
                ("S-ALLOW-READY", "S-BLOCK-STALE"),
            )


if __name__ == "__main__":
    unittest.main()
