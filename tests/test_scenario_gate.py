"""Contract and CLI tests for scripts/scenario_gate.py."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from readiness_helpers import write_ready_artifacts
from scenario_helpers import (
    digest,
    init_git_project,
    parent_contract,
    write_child_project,
    write_json,
    write_parent_project,
    write_parent_scenarios,
    write_passing_evidence,
)

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "scenario_gate.py"
sys.path.insert(0, str(ROOT / "scripts"))

from scenario_gate import evidence_template, validate_readiness  # noqa: E402


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
        self.assertIsNone(result.coverage)

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

        duplicate_observation = parent_contract()
        duplicate_observation["scenarios"][1]["then"][0]["id"] = "O-ALLOW-READY"
        cases.append((duplicate_observation, "duplicate observation id"))

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
            "scenario-evidence.json",
            "scenario-result.json",
        ):
            with self.subTest(filename=filename):
                path = self.child / filename
                path.write_text("{}", encoding="utf-8")
                result = validate_readiness(self.child, self.project)
                self.assertFalse(result.allowed)
                self.assertTrue(any(filename in error for error in result.errors))
                path.unlink()


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

    def test_evidence_template_binds_current_artifacts_without_runner_review(self):
        template = evidence_template(self.task_dir, self.project)
        self.assertEqual(template["verdict"], "revise")
        self.assertTrue(template["blocking_findings"])
        self.assertEqual(
            [item["id"] for item in template["observations"]],
            ["O-ALLOW-READY", "O-BLOCK-STALE"],
        )
        self.assertEqual(template["contract_sha256"], digest(self.task_dir / "scenario-contract.json"))
        self.assertNotIn("runner_config_sha256", template)

        process = self.cli("evidence-template")
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertEqual(json.loads(process.stdout), template)

    def test_evidence_template_rejects_unsafe_parent_before_reading_it(self):
        child = write_child_project(self.task_dir)
        inheritance_path = child / "inherited-readiness.json"
        inheritance = json.loads(inheritance_path.read_text(encoding="utf-8"))
        inheritance["parent_task"] = "../outside-parent"
        write_json(inheritance_path, inheritance)
        outside_contract = self.project / "outside-parent" / "scenario-contract.json"
        outside_contract.parent.mkdir(parents=True)
        outside_contract.write_text("SENSITIVE", encoding="utf-8")
        original_read_bytes = Path.read_bytes

        def guarded_read_bytes(path: Path) -> bytes:
            if path.resolve() == outside_contract.resolve():
                raise AssertionError("unsafe parent content was read")
            return original_read_bytes(path)

        with patch.object(Path, "read_bytes", guarded_read_bytes):
            with self.assertRaises(ValueError):
                evidence_template(child, self.project)

    def test_run_and_completion_cli_return_coverage_status(self):
        write_passing_evidence(self.task_dir, self.project)
        run = self.cli("run", "--json")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        value = json.loads(run.stdout)
        self.assertTrue(value["result_written"])
        self.assertEqual(value["completion"]["coverage"]["percentage"], 100.0)
        complete = self.cli("completion", "--json")
        self.assertEqual(complete.returncode, 0, complete.stdout + complete.stderr)
        self.assertTrue(json.loads(complete.stdout)["allowed"])


if __name__ == "__main__":
    unittest.main()
