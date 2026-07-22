"""Observable scenario evidence coverage contracts."""

from __future__ import annotations

import copy
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
    write_passing_evidence,
    write_policy,
)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from scenario_gate import (  # noqa: E402
    evidence_template,
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


class AtomicObservationContractTest(unittest.TestCase):
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
                            "then": [
                                {
                                    "id": "O-COMPLETION-ALLOWED",
                                    "expectation": "completion is observably allowed"
                                }
                            ],
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


class ScenarioEvidenceCoverageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.task = write_full_project(self.project)
        init_git_project(self.project)

    def tearDown(self):
        self.temp.cleanup()

    def test_completion_reports_100_percent_only_with_current_complete_evidence(self):
        write_passing_evidence(self.task, self.project)
        run = run_scenarios(self.task, self.project)
        self.assertTrue(run.result_written, run.errors)

        result = validate_completion(self.task, self.project)

        self.assertTrue(result.allowed, result.errors)
        self.assertIsNotNone(result.coverage)
        assert result.coverage is not None
        self.assertEqual(result.coverage.required, 2)
        self.assertEqual(result.coverage.implementation_mapped, 2)
        self.assertEqual(result.coverage.verification_mapped, 2)
        self.assertEqual(result.coverage.execution_passed, 2)
        self.assertEqual(result.coverage.verified, 2)
        self.assertEqual(result.coverage.percentage, 100.0)

    def test_missing_verification_mapping_blocks_and_lowers_coverage(self):
        write_passing_evidence(self.task, self.project)
        evidence_path = self.task / "scenario-evidence.json"
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        evidence["observations"][1]["verification"] = []
        write_json(evidence_path, evidence)
        self.assertTrue(run_scenarios(self.task, self.project).result_written)

        result = validate_completion(self.task, self.project)

        self.assertFalse(result.allowed)
        assert result.coverage is not None
        self.assertEqual(result.coverage.verification_mapped, 1)
        self.assertEqual(result.coverage.verified, 1)
        self.assertEqual(result.coverage.percentage, 50.0)

    def test_source_change_invalidates_evidence_and_execution(self):
        write_passing_evidence(self.task, self.project)
        self.assertTrue(run_scenarios(self.task, self.project).result_written)
        (self.project / "src" / "app.txt").write_text("changed\n", encoding="utf-8")

        result = validate_completion(self.task, self.project)

        self.assertFalse(result.allowed)
        self.assertTrue(any("source_fingerprint is stale" in error for error in result.errors))
        assert result.coverage is not None
        self.assertEqual(result.coverage.verified, 0)

    def test_invalid_evidence_is_rejected_with_specific_reasons(self):
        base = evidence_template(self.task, self.project)
        for item in base["observations"]:
            item["implementation"] = [{"path": "src/app.txt", "line": 1}]
            item["verification"] = [{"path": "tests/scenario.txt", "line": 1}]
        base["verdict"] = "pass"
        base["blocking_findings"] = []
        cases: list[tuple[dict, str]] = []

        unknown = copy.deepcopy(base)
        unknown["runner_config_sha256"] = "0" * 64
        cases.append((unknown, "unknown fields: runner_config_sha256"))

        duplicate = copy.deepcopy(base)
        duplicate["observations"][1]["id"] = duplicate["observations"][0]["id"]
        cases.append((duplicate, "observation ids must exactly match"))

        unsafe = copy.deepcopy(base)
        unsafe["observations"][0]["implementation"] = [
            {"path": "../outside.py", "line": 1}
        ]
        cases.append((unsafe, "safe project-relative source path"))

        bad_line = copy.deepcopy(base)
        bad_line["observations"][0]["verification"] = [
            {"path": "tests/scenario.txt", "line": 999}
        ]
        cases.append((bad_line, "existing source line"))

        revised = copy.deepcopy(base)
        revised["verdict"] = "revise"
        cases.append((revised, "verdict must be 'pass'"))

        findings = copy.deepcopy(base)
        findings["blocking_findings"] = ["O-READY-ALLOWED lacks a real assertion"]
        cases.append((findings, "blocking_findings must be empty"))

        stale_contract = copy.deepcopy(base)
        stale_contract["contract_sha256"] = "0" * 64
        cases.append((stale_contract, "contract_sha256 is stale"))

        self.assertTrue(run_scenarios(self.task, self.project).result_written)
        for evidence, fragment in cases:
            with self.subTest(fragment=fragment):
                write_json(self.task / "scenario-evidence.json", evidence)
                result = validate_completion(self.task, self.project)
                self.assertFalse(result.allowed)
                self.assertTrue(
                    any(fragment in error for error in result.errors),
                    result.errors,
                )


class ScenarioExecutionIsolationTest(unittest.TestCase):
    def test_each_scenario_keeps_its_own_runner_result(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            task = write_full_project(project, exit_codes=(0, 7))
            init_git_project(project)
            write_passing_evidence(task, project)

            run = run_scenarios(task, project)
            result = json.loads((task / "scenario-result.json").read_text(encoding="utf-8"))

            self.assertTrue(run.result_written, run.errors)
            self.assertEqual(
                {item["id"]: item["status"] for item in result["results"]},
                {"S-ALLOW-READY": "passed", "S-BLOCK-STALE": "failed"},
            )
            completion = validate_completion(task, project)
            self.assertFalse(completion.allowed)
            assert completion.coverage is not None
            self.assertEqual(completion.coverage.execution_passed, 1)


class ParentCompletionBoundaryTest(unittest.TestCase):
    def test_child_uses_parent_contract_result_and_evidence(self):
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
            write_passing_evidence(parent, project)

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
