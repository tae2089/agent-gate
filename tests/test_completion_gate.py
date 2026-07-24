"""Contract tests for executable Completion Gate results."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gate_helpers import IMPLEMENTATION, TASK, init_git_project

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "scenario_gate.py"
sys.path.insert(0, str(ROOT / "scripts"))

import scenario_gate  # noqa: E402
from scenario_gate import (  # noqa: E402
    run_scenarios,
    validate_completion,
    validate_current_result,
)


class CompletionGateTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.task = self.project / "_workspace" / "sample-task"
        self.task.mkdir(parents=True)
        (self.task / "task.md").write_text(TASK, encoding="utf-8")
        (self.task / "implementation.md").write_text(IMPLEMENTATION, encoding="utf-8")
        (self.task / "scenario-contract.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "scenarios": [
                        {
                            "id": "S-PASS",
                            "title": "Passing observable scenario",
                            "command": [sys.executable, "-c", "raise SystemExit(0)"],
                            "given": ["a valid design"],
                            "when": ["the scenario executes"],
                            "then": ["the result passes"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        init_git_project(self.project)

    def tearDown(self):
        self.temp.cleanup()

    def test_fresh_all_pass_result_is_100_percent(self):
        run = run_scenarios(self.task, self.project)
        result = validate_completion(self.task, self.project)

        self.assertTrue(run.result_written, run.errors)
        self.assertTrue(result.allowed, result.errors)
        self.assertIsNotNone(result.trace_completeness)
        assert result.trace_completeness is not None
        self.assertEqual(result.trace_completeness.required, 1)
        self.assertEqual(result.trace_completeness.passed, 1)
        self.assertEqual(result.trace_completeness.percentage, 100.0)
        self.assertTrue(result.trace_completeness.current)

    def test_current_result_reports_a_missing_design(self):
        missing = self.project / "_workspace" / "missing"

        result = validate_current_result(missing, self.project)

        self.assertFalse(result.allowed)
        self.assertIn(
            "task must be inside the project _workspace",
            " ".join(result.errors),
        )

    def test_scenario_argv_runs_from_project_root_without_shell(self):
        contract_path = self.task / "scenario-contract.json"
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        contract["scenarios"][0]["command"] = [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                f"raise SystemExit(0 if Path.cwd().resolve() == "
                f"Path({str(self.project)!r}).resolve() else 9)"
            ),
        ]
        contract_path.write_text(json.dumps(contract), encoding="utf-8")

        run = run_scenarios(self.task, self.project)
        result = validate_completion(self.task, self.project)

        self.assertTrue(run.result_written, run.errors)
        self.assertTrue(result.allowed, result.errors)

    def test_failed_scenario_blocks_completion(self):
        contract_path = self.task / "scenario-contract.json"
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        contract["scenarios"][0]["command"] = [
            sys.executable,
            "-c",
            "raise SystemExit(7)",
        ]
        contract_path.write_text(json.dumps(contract), encoding="utf-8")

        self.assertTrue(run_scenarios(self.task, self.project).result_written)
        result = validate_completion(self.task, self.project)

        self.assertFalse(result.allowed)
        self.assertIn("required scenario did not pass: S-PASS", result.errors)
        assert result.trace_completeness is not None
        self.assertEqual(result.trace_completeness.percentage, 0.0)

    def test_current_result_accepts_failed_scenarios_without_claiming_completion(self):
        contract_path = self.task / "scenario-contract.json"
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        contract["scenarios"][0]["command"] = [
            sys.executable,
            "-c",
            "raise SystemExit(7)",
        ]
        contract_path.write_text(json.dumps(contract), encoding="utf-8")
        self.assertTrue(run_scenarios(self.task, self.project).result_written)

        current = validate_current_result(self.task, self.project)
        completion = validate_completion(self.task, self.project)

        self.assertTrue(current.allowed, current.errors)
        assert current.trace_completeness is not None
        self.assertTrue(current.trace_completeness.current)
        self.assertEqual(current.trace_completeness.percentage, 0.0)
        self.assertFalse(completion.allowed)

    def test_source_change_makes_previous_result_stale(self):
        self.assertTrue(run_scenarios(self.task, self.project).result_written)
        (self.project / "src" / "app.txt").write_text("changed\n", encoding="utf-8")

        result = validate_completion(self.task, self.project)

        self.assertFalse(result.allowed)
        self.assertIn("scenario result source_fingerprint is stale", result.errors)
        assert result.trace_completeness is not None
        self.assertEqual(result.trace_completeness.passed, 1)
        self.assertEqual(result.trace_completeness.percentage, 100.0)
        self.assertFalse(result.trace_completeness.current)

        current = validate_current_result(self.task, self.project)
        self.assertFalse(current.allowed)
        self.assertIn("scenario result source_fingerprint is stale", current.errors)

    def test_design_change_makes_previous_result_stale(self):
        cases = (
            ("task.md", "scenario result task_sha256 is stale"),
            ("implementation.md", "scenario result flow_sha256 is stale"),
            ("scenario-contract.json", "scenario result contract_sha256 is stale"),
        )
        for filename, expected in cases:
            with self.subTest(filename=filename):
                self.assertTrue(run_scenarios(self.task, self.project).result_written)
                path = self.task / filename
                original = path.read_text(encoding="utf-8")
                if filename == "scenario-contract.json":
                    value = json.loads(original)
                    value["scenarios"][0]["title"] = "Changed observable scenario"
                    path.write_text(json.dumps(value), encoding="utf-8")
                else:
                    path.write_text(original + "\n", encoding="utf-8")

                result = validate_completion(self.task, self.project)

                self.assertFalse(result.allowed)
                self.assertIn(expected, result.errors)
                path.write_text(original, encoding="utf-8")

    def test_runner_bounds_become_infrastructure_errors(self):
        contract_path = self.task / "scenario-contract.json"
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        cases = (
            (
                [sys.executable, "-c", "import time; time.sleep(1)"],
                "RUNNER_TIMEOUT_SECONDS",
                0.01,
                "timed out",
            ),
            (
                [sys.executable, "-c", "print('x' * 10000)"],
                "RUNNER_MAX_OUTPUT_BYTES",
                32,
                "output exceeded",
            ),
        )
        for command, setting, value, reason in cases:
            with self.subTest(reason=reason):
                contract["scenarios"][0]["command"] = command
                contract_path.write_text(json.dumps(contract), encoding="utf-8")
                with patch.object(scenario_gate, setting, value):
                    run = run_scenarios(self.task, self.project)
                stored = json.loads(
                    (self.task / "scenario-result.json").read_text(encoding="utf-8")
                )
                self.assertTrue(run.result_written, run.errors)
                self.assertEqual(stored["results"][0]["status"], "infrastructure-error")
                self.assertIn(reason, stored["results"][0]["reason"])

    def test_cli_finish_uses_and_clears_the_active_task(self):
        self.assertTrue(
            scenario_gate.activate_design(self.task, self.project).allowed
        )
        pointer = self.project / "_workspace" / ".active-task"
        run = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "run",
                "--project-root",
                str(self.project),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        complete = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "completion",
                "--project-root",
                str(self.project),
                "--finish",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("100.00%", run.stdout)
        self.assertEqual(complete.returncode, 0, complete.stdout + complete.stderr)
        self.assertIn("100.00%", complete.stdout)
        self.assertFalse(pointer.exists())

    def test_finish_keeps_active_task_when_result_is_stale(self):
        self.assertTrue(
            scenario_gate.activate_design(self.task, self.project).allowed
        )
        pointer = self.project / "_workspace" / ".active-task"
        self.assertTrue(run_scenarios(self.task, self.project).result_written)
        (self.project / "src" / "app.txt").write_text("changed\n", encoding="utf-8")

        complete = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "completion",
                "--project-root",
                str(self.project),
                "--finish",
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        self.assertEqual(complete.returncode, 1)
        payload = json.loads(complete.stdout)
        self.assertFalse(payload["allowed"])
        self.assertFalse(payload["trace_completeness"]["current"])
        self.assertEqual(payload["trace_completeness"]["passed"], 1)
        self.assertEqual(payload["trace_completeness"]["percentage"], 100.0)
        self.assertTrue(pointer.is_file())


if __name__ == "__main__":
    unittest.main()
