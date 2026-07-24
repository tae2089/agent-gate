"""Contract tests for the deterministic CI repair loop pack."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts"))

from gate_helpers import IMPLEMENTATION, TASK, init_git_project  # noqa: E402

import ci_repair_loop  # noqa: E402
import scenario_gate  # noqa: E402


def failure(**overrides):
    value = {
        "schema_version": 1,
        "source": "manual",
        "source_ref": "conversation:repair-ci",
        "title": "Unit test job is failing",
        "failing_checks": ["unit-tests"],
        "evidence": ["The requested unit-test check exits non-zero."],
        "request": "CI 실패를 고쳐줘.",
    }
    value.update(overrides)
    return value


class FailurePolicyTest(unittest.TestCase):
    def test_manual_request_and_failing_checks_are_required(self):
        valid = ci_repair_loop.validate_failure(failure())
        external = ci_repair_loop.validate_failure(failure(source="ci"))
        missing_request = failure()
        del missing_request["request"]
        no_checks = ci_repair_loop.validate_failure(failure(failing_checks=[]))

        self.assertTrue(valid.allowed, valid.errors)
        self.assertEqual(valid.failure["request"], "CI 실패를 고쳐줘.")
        self.assertFalse(external.allowed)
        self.assertIn("source must be manual", " ".join(external.errors))
        self.assertFalse(ci_repair_loop.validate_failure(missing_request).allowed)
        self.assertFalse(no_checks.allowed)

    def test_unknown_fields_and_malformed_evidence_are_rejected(self):
        unknown = ci_repair_loop.validate_failure(failure(provider="github"))
        malformed = ci_repair_loop.validate_failure(failure(evidence=[""]))

        self.assertFalse(unknown.allowed)
        self.assertFalse(malformed.allowed)


class CIRepairRunTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        init_git_project(self.project)
        self.task = self.project / "_workspace" / "ci-repair"
        self.task.mkdir(parents=True)
        (self.task / "task.md").write_text(TASK, encoding="utf-8")
        (self.task / "implementation.md").write_text(
            IMPLEMENTATION,
            encoding="utf-8",
        )
        (self.task / "scenario-contract.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "scenarios": [
                        {
                            "id": "S-CI-UNIT",
                            "title": "Requested CI check passes",
                            "command": [
                                sys.executable,
                                "-c",
                                "raise SystemExit(0)",
                            ],
                            "given": ["the requested failing check"],
                            "when": ["the check is rerun"],
                            "then": ["the process exits successfully"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def start(self, max_iterations=2):
        result = ci_repair_loop.start_run(
            self.task,
            failure(),
            max_iterations=max_iterations,
        )
        self.assertTrue(result.allowed, result.errors)
        return result

    def reach_verify(self):
        self.start()
        self.assertTrue(
            ci_repair_loop.transition_run(self.task, "repair").allowed
        )
        result = ci_repair_loop.transition_run(self.task, "verify")
        self.assertTrue(result.allowed, result.errors)
        return result

    def test_start_persists_inspect_state_and_failure_hash(self):
        result = self.start()

        self.assertEqual(result.state["status"], "inspect")
        self.assertEqual(result.state["iteration"], 1)
        self.assertEqual(len(result.state["failure_sha256"]), 64)
        self.assertTrue((self.task / "ci-failure.json").is_file())
        self.assertEqual(
            (self.task.parent / ".active-ci-repair").read_text(encoding="utf-8"),
            "_workspace/ci-repair\n",
        )

    def test_only_one_ci_repair_run_is_active_per_worktree(self):
        self.start()
        other = self.task.parent / "other"
        other.mkdir()

        result = ci_repair_loop.start_run(other, failure())

        self.assertFalse(result.allowed)
        self.assertIn("another CI repair run is active", result.errors)

    def test_failed_verification_retries_until_budget_is_exhausted(self):
        self.reach_verify()

        retry = ci_repair_loop.transition_run(self.task, "repair")
        self.assertTrue(retry.allowed, retry.errors)
        self.assertEqual(retry.state["iteration"], 2)
        self.assertTrue(
            ci_repair_loop.transition_run(self.task, "verify").allowed
        )
        exhausted = ci_repair_loop.transition_run(self.task, "repair")

        self.assertTrue(exhausted.allowed, exhausted.errors)
        self.assertEqual(exhausted.state["status"], "budget-exhausted")
        self.assertEqual(exhausted.state["iteration"], 2)

    def test_completion_must_be_current_and_complete(self):
        self.reach_verify()

        missing = ci_repair_loop.complete_run(self.task, self.project)

        self.assertFalse(missing.allowed)
        self.assertEqual(missing.state["status"], "verify")
        self.assertIn("cannot read scenario-result.json", " ".join(missing.errors))

        self.assertTrue(
            scenario_gate.run_scenarios(self.task, self.project).result_written
        )
        completed = ci_repair_loop.complete_run(self.task, self.project)

        self.assertTrue(completed.allowed, completed.errors)
        self.assertEqual(completed.state["status"], "checks-green")

    def test_stale_completion_cannot_finish_the_repair(self):
        self.reach_verify()
        self.assertTrue(
            scenario_gate.run_scenarios(self.task, self.project).result_written
        )
        (self.project / "src" / "app.txt").write_text(
            "changed\n",
            encoding="utf-8",
        )

        result = ci_repair_loop.complete_run(self.task, self.project)

        self.assertFalse(result.allowed)
        self.assertEqual(result.state["status"], "verify")
        self.assertIn("source_fingerprint is stale", " ".join(result.errors))

    def test_complete_is_rejected_outside_verify(self):
        self.start()

        result = ci_repair_loop.complete_run(self.task, self.project)

        self.assertFalse(result.allowed)
        self.assertIn("must be in verify phase", " ".join(result.errors))

    def test_cli_start_and_status_use_the_direct_workspace_task(self):
        input_path = self.task / "failure-input.json"
        input_path.write_text(json.dumps(failure()), encoding="utf-8")
        common = ["--project-root", str(self.project), "--json"]

        started = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "ci_repair_loop.py"),
                "start",
                "_workspace/ci-repair",
                "--failure",
                str(input_path),
                *common,
            ],
            cwd=self.project,
            text=True,
            capture_output=True,
            check=False,
        )
        status = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "ci_repair_loop.py"),
                "status",
                *common,
            ],
            cwd=self.project,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(started.returncode, 0, started.stderr)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["state"]["status"], "inspect")


if __name__ == "__main__":
    unittest.main()
