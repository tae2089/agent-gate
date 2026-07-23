"""Contract and CLI tests for scripts/scenario_gate.py."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from readiness_helpers import write_ready_artifacts
from scenario_helpers import (
    init_git_project,
    parent_contract,
    write_child_project,
    write_parent_project,
    write_parent_scenarios,
)

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "scenario_gate.py"
sys.path.insert(0, str(ROOT / "scripts"))

from scenario_gate import validate_readiness  # noqa: E402


class ScenarioContractTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.task_dir = write_parent_project(self.project)

    def tearDown(self):
        self.temp.cleanup()

    def assert_invalid_contract(self, contract: dict, fragment: str) -> None:
        write_parent_scenarios(self.task_dir, contract)
        result = validate_readiness(self.task_dir, self.project)
        self.assertFalse(result.allowed, result)
        self.assertTrue(
            any(fragment in error for error in result.errors),
            f"missing {fragment!r} in {result.errors!r}",
        )

    def test_smallest_valid_parent_contract_is_ready(self):
        result = validate_readiness(self.task_dir, self.project)
        self.assertTrue(result.enabled)
        self.assertTrue(result.allowed, result.errors)
        self.assertEqual(
            result.required_scenarios,
            ("S-ALLOW-READY", "S-BLOCK-STALE"),
        )
        self.assertIsNone(result.trace_completeness)

    def test_full_task_outside_project_workspace_is_rejected(self):
        with tempfile.TemporaryDirectory() as outside_directory:
            outside = Path(outside_directory) / "external-task"
            write_ready_artifacts(outside)
            result = validate_readiness(outside, self.project)
        self.assertFalse(result.allowed)
        self.assertTrue(any("_workspace" in error for error in result.errors), result.errors)

    def test_contract_rejects_unknown_duplicate_missing_and_empty_content(self):
        cases: list[tuple[dict, str]] = []

        unknown = parent_contract()
        unknown["unexpected"] = True
        cases.append((unknown, "unknown fields"))

        duplicate_scenario = parent_contract()
        duplicate_scenario["scenarios"].append(
            copy.deepcopy(duplicate_scenario["scenarios"][0])
        )
        duplicate_scenario["scenarios"][-1]["runner"] = "stale-check"
        cases.append((duplicate_scenario, "duplicate scenario id"))

        legacy_observation = parent_contract()
        legacy_observation["scenarios"][0]["then"] = [
            {"id": "O-ALLOW-READY", "expectation": "an obsolete shape"}
        ]
        cases.append((legacy_observation, "then contains an invalid string"))

        missing_ac = parent_contract()
        missing_ac["scenarios"][1]["covers"]["acceptance"] = ["AC-2"]
        cases.append((missing_ac, "missing acceptance coverage: AC-1"))

        fake_flow = parent_contract()
        fake_flow["scenarios"][0]["covers"]["flow"] = ["P99"]
        cases.append((fake_flow, "flow reference P99"))

        empty_then = parent_contract()
        empty_then["scenarios"][0]["then"] = []
        cases.append((empty_then, "then must be a non-empty list"))

        legacy_metadata = parent_contract()
        legacy_metadata["scenarios"][0]["risk"] = "critical"
        cases.append((legacy_metadata, "unknown fields: risk"))

        shared_runner = parent_contract()
        shared_runner["scenarios"][1]["runner"] = "ready-check"
        cases.append((shared_runner, "runner ready-check must be exclusive"))

        for contract, fragment in cases:
            with self.subTest(fragment=fragment):
                self.assert_invalid_contract(contract, fragment)


class ParentCompletionBoundaryValidationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.parent = write_parent_project(self.project)
        self.child = write_child_project(self.parent)

    def tearDown(self):
        self.temp.cleanup()

    def test_child_readiness_uses_all_parent_scenarios(self):
        result = validate_readiness(self.child, self.project)
        self.assertTrue(result.allowed, result.errors)
        self.assertEqual(
            result.required_scenarios,
            ("S-ALLOW-READY", "S-BLOCK-STALE"),
        )

    def test_child_rejects_any_independent_scenario_artifact(self):
        for filename in (
            "scenario-overlay.json",
            "scenario-contract.json",
            "scenario-result.json",
        ):
            with self.subTest(filename=filename):
                path = self.child / filename
                path.write_text("{}", encoding="utf-8")
                result = validate_readiness(self.child, self.project)
                self.assertFalse(result.allowed)
                self.assertTrue(any(filename in error for error in result.errors))
                path.unlink()

    def test_child_ignores_obsolete_scenario_evidence(self):
        (self.child / "scenario-evidence.json").write_text("not json", encoding="utf-8")

        result = validate_readiness(self.child, self.project)

        self.assertTrue(result.allowed, result.errors)


class ScenarioGateCliTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.task_dir = write_parent_project(self.project)
        init_git_project(self.project)

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, operation: str, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                operation,
                str(self.task_dir),
                "--project-root",
                str(self.project),
                *extra,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_removed_evidence_template_cli_is_rejected(self):
        process = self.cli("evidence-template")

        self.assertNotEqual(process.returncode, 0)
        self.assertIn("invalid choice", process.stderr)

    def test_run_and_completion_cli_return_trace_status_without_evidence(self):
        run = self.cli("run", "--json")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        value = json.loads(run.stdout)
        self.assertTrue(value["result_written"])
        self.assertEqual(
            value["completion"]["trace_completeness"]["percentage"],
            100.0,
        )
        complete = self.cli("completion", "--json")
        self.assertEqual(complete.returncode, 0, complete.stdout + complete.stderr)
        self.assertTrue(json.loads(complete.stdout)["allowed"])


if __name__ == "__main__":
    unittest.main()
