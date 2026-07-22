"""Contract and policy tests for scripts/scenario_gate.py."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scenario_helpers import (
    digest,
    parent_contract,
    write_child_project,
    write_child_scenarios,
    init_git_project,
    write_json,
    write_parent_project,
    write_parent_scenarios,
)

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "scenario_gate.py"
sys.path.insert(0, str(ROOT / "scripts"))

from scenario_gate import review_template, validate_readiness  # noqa: E402


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
        self.assertEqual(result.mode, "enforce")
        self.assertEqual(result.required_scenarios, ("S-ALLOW-READY", "S-BLOCK-STALE"))

    def test_full_task_outside_project_workspace_is_rejected(self):
        with tempfile.TemporaryDirectory() as outside_directory:
            outside = Path(outside_directory) / "external-task"
            from readiness_helpers import write_ready_artifacts

            write_ready_artifacts(outside)
            result = validate_readiness(outside, self.project)
        self.assertFalse(result.allowed)
        self.assertTrue(any("_workspace" in error for error in result.errors), result.errors)

    def test_contract_rejects_unknown_duplicate_missing_and_empty_content(self):
        cases: list[tuple[dict, str]] = []

        unknown = parent_contract()
        unknown["unexpected"] = True
        cases.append((unknown, "unknown fields"))

        duplicate = parent_contract()
        duplicate["scenarios"].append(copy.deepcopy(duplicate["scenarios"][0]))
        cases.append((duplicate, "duplicate scenario id"))

        missing_ac = parent_contract()
        missing_ac["scenarios"][0]["covers"]["acceptance"] = ["AC-1"]
        cases.append((missing_ac, "missing acceptance coverage: AC-2"))

        fake_flow = parent_contract()
        fake_flow["scenarios"][0]["covers"]["flow"] = ["P99"]
        cases.append((fake_flow, "flow reference P99"))

        empty_then = parent_contract()
        empty_then["scenarios"][0]["then"] = []
        cases.append((empty_then, "then must be a non-empty list"))

        unknown_scenario_field = parent_contract()
        unknown_scenario_field["scenarios"][0]["function_name"] = "validate_task"
        cases.append((unknown_scenario_field, "unknown fields"))

        for contract, fragment in cases:
            with self.subTest(fragment=fragment):
                self.assert_invalid_contract(contract, fragment)

    def test_critical_scenario_requires_an_exclusive_runner(self):
        contract = parent_contract()
        contract["scenarios"][1]["runner"] = "integration"
        self.assert_invalid_contract(contract, "critical runner integration must be exclusive")


class ScenarioReviewTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.task_dir = write_parent_project(self.project)
        self.review_path = self.task_dir / "scenario-review.json"

    def tearDown(self):
        self.temp.cleanup()

    def review(self) -> dict:
        import json

        return json.loads(self.review_path.read_text(encoding="utf-8"))

    def assert_invalid_review(self, review: dict | None, fragment: str) -> None:
        if review is None:
            self.review_path.unlink()
        else:
            write_json(self.review_path, review)
        result = validate_readiness(self.task_dir, self.project)
        self.assertFalse(result.allowed, result)
        self.assertTrue(
            any(fragment in error for error in result.errors),
            f"missing {fragment!r} in {result.errors!r}",
        )

    def test_review_must_be_current_complete_and_passing(self):
        mutations = []

        stale_task = self.review()
        stale_task["task_sha256"] = "0" * 64
        mutations.append((stale_task, "task_sha256 is stale"))

        stale_flow = self.review()
        stale_flow["flow_sha256"] = "0" * 64
        mutations.append((stale_flow, "flow_sha256 is stale"))

        stale_subject = self.review()
        stale_subject["subject_sha256"] = "0" * 64
        mutations.append((stale_subject, "subject_sha256 is stale"))

        stale_runner_config = self.review()
        stale_runner_config["runner_config_sha256"] = "0" * 64
        mutations.append((stale_runner_config, "runner_config_sha256 is stale"))

        wrong_parent = self.review()
        wrong_parent["parent_contract_sha256"] = "0" * 64
        mutations.append((wrong_parent, "parent_contract_sha256 must be empty"))

        missing_id = self.review()
        missing_id["reviewed_scenarios"] = ["S-ALLOW-READY"]
        mutations.append((missing_id, "reviewed_scenarios must exactly match"))

        revised = self.review()
        revised["verdict"] = "revise"
        mutations.append((revised, "verdict must be 'pass'"))

        findings = self.review()
        findings["blocking_findings"] = ["rollback outcome is missing"]
        mutations.append((findings, "blocking_findings must be empty"))

        unknown = self.review()
        unknown["confidence"] = 0.99
        mutations.append((unknown, "unknown fields"))

        self.assert_invalid_review(None, "cannot read scenario-review.json")
        for review, fragment in mutations:
            with self.subTest(fragment=fragment):
                write_parent_scenarios(self.task_dir)
                self.assert_invalid_review(review, fragment)

    def test_review_becomes_stale_after_runner_config_change(self):
        policy_path = self.project / ".agent-gate" / "scenario-gate.json"
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        policy["runners"]["integration"]["timeout_seconds"] = 31
        write_json(policy_path, policy)

        result = validate_readiness(self.task_dir, self.project)

        self.assertFalse(result.allowed, result)
        self.assertTrue(
            any("runner_config_sha256 is stale" in error for error in result.errors),
            result.errors,
        )


class ChildScenarioOverlayTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.parent = write_parent_project(self.project)
        self.child = write_child_project(self.parent)
        self.overlay_path = self.child / "scenario-overlay.json"

    def tearDown(self):
        self.temp.cleanup()

    def overlay(self) -> dict:
        import json

        return json.loads(self.overlay_path.read_text(encoding="utf-8"))

    def assert_invalid_overlay(self, overlay: dict, fragment: str) -> None:
        write_json(self.overlay_path, overlay)
        result = validate_readiness(self.child, self.project)
        self.assertFalse(result.allowed, result)
        self.assertTrue(
            any(fragment in error for error in result.errors),
            f"missing {fragment!r} in {result.errors!r}",
        )

    def test_child_inherits_parent_and_adds_reviewed_local_scenario(self):
        result = validate_readiness(self.child, self.project)
        self.assertTrue(result.allowed, result.errors)
        self.assertEqual(
            result.required_scenarios,
            ("S-BLOCK-STALE", "S-CHILD-LOCAL"),
        )

    def test_child_overlay_rejects_stale_unknown_duplicate_and_out_of_scope_content(self):
        mutations = []

        stale = self.overlay()
        stale["parent_contract_sha256"] = "0" * 64
        mutations.append((stale, "parent_contract_sha256 is stale"))

        unknown_parent_id = self.overlay()
        unknown_parent_id["inherited_scenarios"] = ["S-NOT-REAL"]
        mutations.append((unknown_parent_id, "inherited scenario S-NOT-REAL"))

        duplicate_id = self.overlay()
        duplicate_id["local_scenarios"][0]["id"] = "S-BLOCK-STALE"
        mutations.append((duplicate_id, "duplicate effective scenario id"))

        outside_ac = self.overlay()
        outside_ac["local_scenarios"][0]["covers"]["acceptance"] = ["AC-2"]
        mutations.append((outside_ac, "outside child acceptance scope"))

        outside_flow = self.overlay()
        outside_flow["local_scenarios"][0]["covers"]["flow"] = ["P99"]
        mutations.append((outside_flow, "outside child flow scope"))

        malformed_flow = self.overlay()
        malformed_flow["local_scenarios"][0]["covers"]["flow"] = [["P1"]]
        mutations.append((malformed_flow, "invalid string"))

        unknown_field = self.overlay()
        unknown_field["promotion_score"] = 0.9
        mutations.append((unknown_field, "unknown fields"))

        for overlay, fragment in mutations:
            with self.subTest(fragment=fragment):
                write_child_scenarios(self.parent, self.child)
                self.assert_invalid_overlay(overlay, fragment)

    def test_child_cannot_reuse_an_inherited_critical_runner(self):
        overlay = self.overlay()
        overlay["local_scenarios"][0]["runner"] = "critical"
        write_json(self.overlay_path, overlay)
        review_path = self.child / "scenario-review.json"
        review = json.loads(review_path.read_text(encoding="utf-8"))
        review["subject_sha256"] = digest(self.overlay_path)
        write_json(review_path, review)

        result = validate_readiness(self.child, self.project)

        self.assertFalse(result.allowed, result)
        self.assertTrue(
            any("critical runner critical must be exclusive" in error for error in result.errors),
            result.errors,
        )


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

    def test_review_template_binds_current_artifacts_without_approving(self):
        (self.task_dir / "scenario-review.json").unlink()
        process = self.cli("review-template")
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        template = json.loads(process.stdout)
        self.assertEqual(template["verdict"], "revise")
        self.assertTrue(template["blocking_findings"])
        self.assertEqual(
            template["reviewed_scenarios"],
            ["S-ALLOW-READY", "S-BLOCK-STALE"],
        )
        self.assertEqual(
            template["runner_config_sha256"],
            digest(self.project / ".agent-gate" / "scenario-gate.json"),
        )

        template["verdict"] = "pass"
        template["blocking_findings"] = []
        write_json(self.task_dir / "scenario-review.json", template)
        ready = self.cli("readiness", "--json")
        self.assertEqual(ready.returncode, 0, ready.stdout + ready.stderr)
        self.assertTrue(json.loads(ready.stdout)["allowed"])

    def test_review_template_rejects_unsafe_parent_before_reading_it(self):
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
                review_template(child, self.project)

    def test_run_and_completion_cli_return_policy_status(self):
        run = self.cli("run", "--json")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertTrue(json.loads(run.stdout)["result_written"])
        complete = self.cli("completion", "--json")
        self.assertEqual(complete.returncode, 0, complete.stdout + complete.stderr)
        self.assertTrue(json.loads(complete.stdout)["allowed"])


if __name__ == "__main__":
    unittest.main()
