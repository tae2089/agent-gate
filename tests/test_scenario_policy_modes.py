"""Rollout-mode behavior for advisory, critical, and full enforcement."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

from scenario_helpers import (
    init_git_project,
    parent_contract,
    write_child_project,
    write_parent_project,
    write_parent_scenarios,
    write_policy,
)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from scenario_gate import run_scenarios, validate_completion, validate_readiness  # noqa: E402


def change_status(task_dir: Path, scenario_id: str, status: str) -> None:
    path = task_dir / "scenario-result.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    for result in value["results"]:
        if result["id"] == scenario_id:
            result["status"] = status
            result["reason"] = "forced scenario outcome"
    path.write_text(json.dumps(value), encoding="utf-8")


class RolloutModeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_advisory_reports_missing_review_and_result_without_blocking(self):
        task = write_parent_project(self.project, mode="advisory")
        (task / "scenario-review.json").unlink()
        readiness = validate_readiness(task, self.project)
        completion = validate_completion(task, self.project)
        self.assertTrue(readiness.allowed)
        self.assertTrue(completion.allowed)
        self.assertTrue(readiness.warnings)
        self.assertTrue(completion.warnings)

    def test_critical_mode_blocks_critical_but_warns_on_standard_failure(self):
        task = write_parent_project(self.project, mode="critical-enforce")
        init_git_project(self.project)
        self.assertTrue(run_scenarios(task, self.project).result_written)

        change_status(task, "S-ALLOW-READY", "failed")
        standard = validate_completion(task, self.project)
        self.assertTrue(standard.allowed, standard.errors)
        self.assertTrue(any("S-ALLOW-READY" in warning for warning in standard.warnings))

        change_status(task, "S-BLOCK-STALE", "failed")
        critical = validate_completion(task, self.project)
        self.assertFalse(critical.allowed)
        self.assertTrue(any("S-BLOCK-STALE" in error for error in critical.errors))

    def test_critical_mode_warns_when_standard_scenario_was_not_run(self):
        task = write_parent_project(self.project, mode="critical-enforce")
        init_git_project(self.project)
        run = run_scenarios(task, self.project, ("S-BLOCK-STALE",))
        self.assertTrue(run.result_written, run.errors)
        completion = validate_completion(task, self.project)
        self.assertTrue(completion.allowed, completion.errors)
        self.assertTrue(
            any("S-ALLOW-READY" in warning and "missing" in warning for warning in completion.warnings),
            completion.warnings,
        )

    def test_enforce_mode_blocks_any_required_failure(self):
        task = write_parent_project(self.project, mode="enforce")
        init_git_project(self.project)
        self.assertTrue(run_scenarios(task, self.project).result_written)
        change_status(task, "S-ALLOW-READY", "failed")
        completion = validate_completion(task, self.project)
        self.assertFalse(completion.allowed)
        self.assertTrue(any("S-ALLOW-READY" in error for error in completion.errors))

    def test_large_standard_runner_group_warns_without_blocking(self):
        task = write_parent_project(self.project, mode="enforce")
        contract = parent_contract()
        for item in contract["scenarios"]:
            item["risk"] = "standard"
            item["runner"] = "integration"
        for index in range(3, 7):
            item = copy.deepcopy(contract["scenarios"][0])
            item["id"] = f"S-STANDARD-{index}"
            contract["scenarios"].append(item)
        write_parent_scenarios(task, contract)

        readiness = validate_readiness(task, self.project)
        self.assertTrue(readiness.allowed, readiness.errors)
        self.assertTrue(
            any("integration" in warning and "6 scenarios" in warning for warning in readiness.warnings),
            readiness.warnings,
        )

        init_git_project(self.project)
        self.assertTrue(run_scenarios(task, self.project).result_written)
        completion = validate_completion(task, self.project)
        self.assertTrue(completion.allowed, completion.errors)
        self.assertTrue(
            any("integration" in warning and "6 scenarios" in warning for warning in completion.warnings),
            completion.warnings,
        )

    def test_parent_candidate_blocks_enforce_but_not_critical_mode(self):
        for mode, expected in (("critical-enforce", True), ("enforce", False)):
            with self.subTest(mode=mode):
                project = self.project / mode
                parent = write_parent_project(project, mode=mode)
                child = write_child_project(parent, ownership="parent-candidate")
                init_git_project(project)
                self.assertTrue(run_scenarios(child, project).result_written)
                completion = validate_completion(child, project)
                self.assertEqual(completion.allowed, expected, completion)
                if not expected:
                    self.assertTrue(
                        any("parent-candidate" in error for error in completion.errors)
                    )


if __name__ == "__main__":
    unittest.main()
