"""Process and freshness tests for exclusive scenario runners."""

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

from scenario_gate import load_policy, run_scenarios, validate_completion  # noqa: E402


class ScenarioRunnerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.task_dir = write_parent_project(self.project)
        init_git_project(self.project)

    def tearDown(self):
        self.temp.cleanup()

    def configure(self, definition: dict) -> None:
        write_policy(
            self.project,
            runners={"ready-check": definition, "stale-check": definition},
        )

    def result(self) -> dict:
        return json.loads((self.task_dir / "scenario-result.json").read_text(encoding="utf-8"))

    def test_exit_code_is_recorded_once_per_exclusive_scenario(self):
        write_policy(
            self.project,
            runners={
                "ready-check": {
                    "command": [sys.executable, "-c", "raise SystemExit(0)"]
                },
                "stale-check": {
                    "command": [sys.executable, "-c", "raise SystemExit(7)"]
                },
            },
        )
        run = run_scenarios(self.task_dir, self.project)
        self.assertTrue(run.result_written, run.errors)
        self.assertEqual(
            {item["id"]: item["status"] for item in self.result()["results"]},
            {"S-ALLOW-READY": "passed", "S-BLOCK-STALE": "failed"},
        )

    def test_timeout_and_oversized_output_are_infrastructure_errors(self):
        cases = (
            (
                {
                    "command": [sys.executable, "-c", "import time; time.sleep(2)"],
                    "timeout_seconds": 1,
                },
                "timed out",
            ),
            (
                {
                    "command": [sys.executable, "-c", "print('x' * 2048)"],
                    "max_output_bytes": 1024,
                },
                "output exceeded",
            ),
        )
        for definition, reason in cases:
            with self.subTest(reason=reason):
                self.configure(definition)
                run = run_scenarios(self.task_dir, self.project)
                self.assertTrue(run.result_written, run.errors)
                results = self.result()["results"]
                self.assertEqual(
                    {item["status"] for item in results},
                    {"infrastructure-error"},
                )
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

    def test_runner_config_rejects_invalid_or_legacy_shapes(self):
        cases = (
            ({"command": "go test ./..."}, "string array"),
            (
                {"command": ["go", "test", "./..."], "format": "exit-code"},
                "unknown fields: format",
            ),
            (
                {"command": ["go", "test", "./..."], "report_path": "report.xml"},
                "report_path",
            ),
            (
                {"command": ["go", "test", "./..."], "shell": True},
                "unknown fields",
            ),
            ({"command": ["go", "test\0bad"]}, "NUL"),
        )
        for definition, fragment in cases:
            with self.subTest(fragment=fragment):
                write_policy(self.project, runners={"ready-check": definition})
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
        completion = validate_completion(self.task_dir, self.project)
        self.assertTrue(completion.allowed, completion.errors)

    def assert_stale(self, fragment: str) -> None:
        completion = validate_completion(self.task_dir, self.project)
        self.assertFalse(completion.allowed, completion)
        self.assertTrue(
            any(fragment in error for error in completion.errors),
            f"missing {fragment!r} in {completion.errors!r}",
        )

    def test_result_becomes_stale_after_contract_change(self):
        self.run_and_assert_fresh()
        changed = parent_contract()
        changed["scenarios"][0]["title"] = "Changed observable contract"
        write_parent_scenarios(self.task_dir, changed)
        self.assert_stale("scenario result contract_sha256 is stale")

    def test_result_becomes_stale_after_runner_config_change_without_runner_review(self):
        self.run_and_assert_fresh()
        policy_path = self.project / ".agent-gate" / "scenario-gate.json"
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        policy["runners"]["ready-check"]["timeout_seconds"] = 31
        policy_path.write_text(json.dumps(policy), encoding="utf-8")
        self.assert_stale("scenario result runner_config_sha256 is stale")
        completion = validate_completion(self.task_dir, self.project)
        self.assertFalse(
            any("scenario evidence source_fingerprint is stale" in error for error in completion.errors),
            completion.errors,
        )

    def test_result_becomes_stale_after_source_change(self):
        self.run_and_assert_fresh()
        (self.project / "src" / "app.txt").write_text("changed\n", encoding="utf-8")
        self.assert_stale("source_fingerprint is stale")


if __name__ == "__main__":
    unittest.main()
