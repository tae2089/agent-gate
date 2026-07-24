"""Contract tests for the deterministic review Loop Pack."""

from __future__ import annotations

import hashlib
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

import review_loop  # noqa: E402
import scenario_gate  # noqa: E402


def request(**overrides):
    value = {
        "schema_version": 1,
        "source": "manual",
        "source_ref": "conversation:review-current-change",
        "request": "Review and address the current change until it is clean.",
        "target": "HEAD compared with main",
        "scope": ["changed production code", "related tests"],
        "evidence": ["The user requested an iterative review."],
    }
    value.update(overrides)
    return value


def finding(**overrides):
    value = {
        "id": "R-001",
        "severity": "P1",
        "title": "Observable defect",
        "evidence": ["src/app.txt demonstrates the defect."],
        "action": "Correct the behavior and add a regression test.",
    }
    value.update(overrides)
    return value


class ReviewValidationTest(unittest.TestCase):
    def test_request_requires_manual_authority_and_exact_fields(self):
        valid = review_loop.validate_request(request())
        external = review_loop.validate_request(request(source="github"))
        unknown = review_loop.validate_request(request(provider="github"))
        empty_scope = review_loop.validate_request(request(scope=[]))

        self.assertTrue(valid.allowed, valid.errors)
        self.assertFalse(external.allowed)
        self.assertIn("source must be manual", " ".join(external.errors))
        self.assertFalse(unknown.allowed)
        self.assertFalse(empty_scope.allowed)

    def test_report_verdict_and_findings_must_agree(self):
        base = {
            "schema_version": 1,
            "request_sha256": "a" * 64,
            "scenario_result_sha256": "b" * 64,
        }
        actionable = review_loop.validate_report(
            {**base, "verdict": "actionable", "findings": [finding()]}
        )
        clean = review_loop.validate_report(
            {**base, "verdict": "clean", "findings": []}
        )
        contradictory = review_loop.validate_report(
            {**base, "verdict": "clean", "findings": [finding()]}
        )
        duplicate = review_loop.validate_report(
            {
                **base,
                "verdict": "actionable",
                "findings": [finding(), finding(title="Second")],
            }
        )

        self.assertTrue(actionable.allowed, actionable.errors)
        self.assertTrue(clean.allowed, clean.errors)
        self.assertFalse(contradictory.allowed)
        self.assertFalse(duplicate.allowed)


class ReviewRunTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        init_git_project(self.project)
        self.task = self.project / "_workspace" / "review"
        self.task.mkdir(parents=True)
        (self.task / "task.md").write_text(TASK, encoding="utf-8")
        (self.task / "implementation.md").write_text(
            IMPLEMENTATION,
            encoding="utf-8",
        )
        self.contract_path = self.task / "scenario-contract.json"
        self.contract_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "scenarios": [
                        {
                            "id": "S-REVIEW-UNIT",
                            "title": "Requested review checks pass",
                            "command": [
                                sys.executable,
                                "-c",
                                "raise SystemExit(0)",
                            ],
                            "given": ["the reviewed change"],
                            "when": ["the repository check runs"],
                            "then": ["the process exits successfully"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def start_review(self, max_iterations=2):
        result = review_loop.start_run(
            self.task,
            request(),
            max_iterations=max_iterations,
        )
        self.assertTrue(result.allowed, result.errors)
        self.assertTrue(review_loop.transition_run(self.task, "review").allowed)
        return result

    def report(self, verdict, findings):
        state = review_loop.load_run(self.task)
        content = (self.task / "scenario-result.json").read_bytes()
        return {
            "schema_version": 1,
            "request_sha256": state.state["request_sha256"],
            "scenario_result_sha256": hashlib.sha256(content).hexdigest(),
            "verdict": verdict,
            "findings": findings,
        }

    def set_scenario_exit(self, code):
        contract = json.loads(self.contract_path.read_text(encoding="utf-8"))
        contract["scenarios"][0]["command"] = [
            sys.executable,
            "-c",
            f"raise SystemExit({code})",
        ]
        self.contract_path.write_text(json.dumps(contract), encoding="utf-8")

    def test_start_persists_inspect_state_and_active_pointer(self):
        result = review_loop.start_run(self.task, request())

        self.assertTrue(result.allowed, result.errors)
        self.assertEqual(result.state["status"], "inspect")
        self.assertEqual(len(result.state["request_sha256"]), 64)
        self.assertTrue((self.task / "review-request.json").is_file())
        self.assertEqual(
            (self.task.parent / ".active-review").read_text(encoding="utf-8"),
            "_workspace/review\n",
        )

    def test_actionable_report_can_capture_current_failed_scenarios(self):
        self.start_review()
        self.set_scenario_exit(7)
        self.assertTrue(
            scenario_gate.run_scenarios(self.task, self.project).result_written
        )

        submitted = review_loop.submit_review(
            self.task,
            self.project,
            self.report("actionable", [finding()]),
        )

        self.assertTrue(submitted.allowed, submitted.errors)
        self.assertEqual(submitted.state["status"], "address")
        self.assertTrue((self.task / "iterations" / "001" / "review.json").is_file())

    def test_clean_report_requires_current_100_percent_completion(self):
        self.start_review()
        missing = review_loop.submit_review(
            self.task,
            self.project,
            {
                "schema_version": 1,
                "request_sha256": review_loop.load_run(self.task).state[
                    "request_sha256"
                ],
                "scenario_result_sha256": "a" * 64,
                "verdict": "clean",
                "findings": [],
            },
        )
        self.assertFalse(missing.allowed)
        self.assertEqual(missing.state["status"], "review")

        self.assertTrue(
            scenario_gate.run_scenarios(self.task, self.project).result_written
        )
        clean = review_loop.submit_review(
            self.task,
            self.project,
            self.report("clean", []),
        )

        self.assertTrue(clean.allowed, clean.errors)
        self.assertEqual(clean.state["status"], "review-clean")

    def test_stale_report_cannot_change_state(self):
        self.start_review()
        self.assertTrue(
            scenario_gate.run_scenarios(self.task, self.project).result_written
        )
        report = self.report("actionable", [finding()])
        (self.project / "src" / "app.txt").write_text(
            "changed\n",
            encoding="utf-8",
        )

        result = review_loop.submit_review(
            self.task,
            self.project,
            report,
        )

        self.assertFalse(result.allowed)
        self.assertEqual(result.state["status"], "review")
        self.assertIn("source_fingerprint is stale", " ".join(result.errors))

    def test_verify_rechecks_completion_and_consumes_retry_budget(self):
        self.start_review(max_iterations=2)
        self.assertTrue(
            scenario_gate.run_scenarios(self.task, self.project).result_written
        )
        self.assertTrue(
            review_loop.submit_review(
                self.task,
                self.project,
                self.report("actionable", [finding()]),
            ).allowed
        )
        self.assertTrue(review_loop.transition_run(self.task, "verify").allowed)
        self.assertTrue(
            scenario_gate.run_scenarios(self.task, self.project).result_written
        )

        retry = review_loop.verify_run(self.task, self.project)

        self.assertTrue(retry.allowed, retry.errors)
        self.assertEqual(retry.state["status"], "review")
        self.assertEqual(retry.state["iteration"], 2)

        self.assertTrue(
            review_loop.submit_review(
                self.task,
                self.project,
                self.report("actionable", [finding(id="R-002")]),
            ).allowed
        )
        self.assertTrue(review_loop.transition_run(self.task, "verify").allowed)
        self.assertTrue(
            scenario_gate.run_scenarios(self.task, self.project).result_written
        )
        exhausted = review_loop.verify_run(self.task, self.project)

        self.assertTrue(exhausted.allowed, exhausted.errors)
        self.assertEqual(exhausted.state["status"], "budget-exhausted")

    def test_cli_start_and_status_use_the_direct_workspace_task(self):
        input_path = self.task / "request-input.json"
        input_path.write_text(json.dumps(request()), encoding="utf-8")
        common = ["--project-root", str(self.project), "--json"]
        started = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "review_loop.py"),
                "start",
                "_workspace/review",
                "--request",
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
                str(ROOT / "scripts" / "review_loop.py"),
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
        payload = json.loads(status.stdout)
        self.assertEqual(payload["state"]["status"], "inspect")
        self.assertEqual(payload["task"], str(self.task.resolve()))


if __name__ == "__main__":
    unittest.main()
