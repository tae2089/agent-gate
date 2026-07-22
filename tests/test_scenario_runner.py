"""Process-level tests for the language-neutral scenario runner."""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from scenario_helpers import (
    init_git_project,
    parent_contract,
    write_parent_project,
    write_parent_scenarios,
    write_policy,
)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from scenario_gate import load_policy, run_scenarios  # noqa: E402


class ScenarioRunnerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.task_dir = write_parent_project(self.project)
        init_git_project(self.project)

    def tearDown(self):
        self.temp.cleanup()

    def configure(self, runner: dict) -> None:
        write_policy(self.project, runners={"integration": runner})

    def result(self) -> dict:
        return json.loads(
            (self.task_dir / "scenario-result.json").read_text(encoding="utf-8")
        )

    def test_exit_code_maps_all_selected_scenarios(self):
        for exit_code, expected in ((0, "passed"), (7, "failed")):
            with self.subTest(exit_code=exit_code):
                self.configure(
                    {
                        "command": [sys.executable, "-c", f"raise SystemExit({exit_code})"],
                        "format": "exit-code",
                    }
                )
                run = run_scenarios(self.task_dir, self.project)
                self.assertTrue(run.result_written, run.errors)
                statuses = {item["status"] for item in self.result()["results"]}
                self.assertEqual(statuses, {expected})

    def test_timeout_and_oversized_output_are_infrastructure_errors(self):
        cases = (
            (
                {
                    "command": [sys.executable, "-c", "import time; time.sleep(2)"],
                    "format": "exit-code",
                    "timeout_seconds": 1,
                },
                "timed out",
            ),
            (
                {
                    "command": [sys.executable, "-c", "print('x' * 2048)"],
                    "format": "exit-code",
                    "max_output_bytes": 1024,
                },
                "output exceeded",
            ),
        )
        for runner, reason in cases:
            with self.subTest(reason=reason):
                self.configure(runner)
                run = run_scenarios(self.task_dir, self.project)
                self.assertTrue(run.result_written, run.errors)
                results = self.result()["results"]
                self.assertEqual({item["status"] for item in results}, {"infrastructure-error"})
                self.assertTrue(all(reason in item["reason"] for item in results))

    def test_output_limit_stops_a_still_running_process_early(self):
        program = (
            "import sys, time\n"
            "while True:\n"
            "    sys.stdout.write('x' * 2048)\n"
            "    sys.stdout.flush()\n"
            "    time.sleep(0.01)\n"
        )
        self.configure(
            {
                "command": [sys.executable, "-c", program],
                "format": "exit-code",
                "timeout_seconds": 3,
                "max_output_bytes": 1024,
            }
        )
        started = time.monotonic()
        run = run_scenarios(self.task_dir, self.project)
        elapsed = time.monotonic() - started
        self.assertTrue(run.result_written, run.errors)
        self.assertLess(elapsed, 2.0)
        self.assertTrue(any("output exceeded" in error for error in run.errors), run.errors)

    def test_runner_config_rejects_invalid_shapes_and_formats(self):
        cases = (
            ({"command": "go test ./...", "format": "exit-code"}, "string array"),
            (
                {
                    "command": ["go", "test", "./..."],
                    "format": "agent-gate-json",
                    "report_path": ".agent-gate/report.json",
                },
                "format must be one of",
            ),
            (
                {
                    "command": ["go", "test", "./..."],
                    "format": "junit-xml",
                    "report_path": ".agent-gate/report.xml",
                },
                "format must be one of",
            ),
            (
                {
                    "command": ["go", "test", "./..."],
                    "format": "exit-code",
                    "report_path": ".agent-gate/report.xml",
                },
                "report_path",
            ),
            (
                {
                    "command": ["go", "test", "./..."],
                    "format": "exit-code",
                    "shell": True,
                },
                "unknown fields",
            ),
            (
                {"command": ["go", "test\0bad"], "format": "exit-code"},
                "NUL",
            ),
        )
        for runner, fragment in cases:
            with self.subTest(fragment=fragment):
                write_policy(self.project, runners={"integration": runner})
                policy, errors = load_policy(self.project)
                self.assertIsNone(policy)
                self.assertTrue(any(fragment in error for error in errors), errors)

class ScenarioResultFreshnessTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.task_dir = write_parent_project(self.project)
        init_git_project(self.project)

    def tearDown(self):
        self.temp.cleanup()

    def run_and_assert_fresh(self) -> None:
        run = run_scenarios(self.task_dir, self.project)
        self.assertTrue(run.result_written, run.errors)
        from scenario_gate import validate_completion

        completion = validate_completion(self.task_dir, self.project)
        self.assertTrue(completion.allowed, completion.errors)

    def assert_stale(self, fragment: str) -> None:
        from scenario_gate import validate_completion

        completion = validate_completion(self.task_dir, self.project)
        self.assertFalse(completion.allowed, completion)
        self.assertTrue(
            any(fragment in error for error in completion.errors),
            f"missing {fragment!r} in {completion.errors!r}",
        )

    def test_result_becomes_stale_after_effective_scenario_change(self):
        self.run_and_assert_fresh()
        changed = parent_contract()
        changed["scenarios"][0]["title"] = "Changed observable contract"
        write_parent_scenarios(self.task_dir, changed)
        self.assert_stale("effective_scenarios_sha256 is stale")

    def test_result_becomes_stale_after_runner_config_change(self):
        self.run_and_assert_fresh()
        write_policy(self.project, mode="critical-enforce")
        self.assert_stale("runner_config_sha256 is stale")

    def test_result_becomes_stale_after_tracked_or_untracked_source_change(self):
        self.run_and_assert_fresh()
        (self.project / "src" / "app.txt").write_text("changed\n", encoding="utf-8")
        self.assert_stale("source_fingerprint is stale")

        self.run_and_assert_fresh()
        untracked = self.project / "src" / "new.txt"
        untracked.write_text("new\n", encoding="utf-8")
        self.assert_stale("source_fingerprint is stale")


if __name__ == "__main__":
    unittest.main()
