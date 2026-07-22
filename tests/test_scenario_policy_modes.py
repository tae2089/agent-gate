"""Single-policy behavior for observable scenario completion."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scenario_helpers import (
    init_git_project,
    write_parent_project,
    write_passing_evidence,
    write_policy,
)

import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from scenario_gate import load_policy, run_scenarios, validate_completion  # noqa: E402


class SinglePolicyTest(unittest.TestCase):
    def test_legacy_rollout_modes_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            for mode in ("advisory", "critical-enforce", "enforce"):
                with self.subTest(mode=mode):
                    write_policy(project, mode=mode)
                    policy, errors = load_policy(project)
                    self.assertIsNone(policy)
                    self.assertTrue(any("unknown fields: mode" in error for error in errors))

    def test_any_required_scenario_failure_blocks_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            task = write_parent_project(project)
            init_git_project(project)
            write_passing_evidence(task, project)
            policy_path = project / ".agent-gate" / "scenario-gate.json"
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
            policy["runners"]["stale-check"]["command"] = [
                sys.executable,
                "-c",
                "raise SystemExit(7)",
            ]
            policy_path.write_text(json.dumps(policy), encoding="utf-8")
            # Runner configuration changed after evidence creation, which is allowed:
            # evidence judges code mapping, while results bind the runner config.
            run = run_scenarios(task, project)
            self.assertTrue(run.result_written, run.errors)

            result = validate_completion(task, project)

            self.assertFalse(result.allowed)
            self.assertTrue(any("S-BLOCK-STALE" in error for error in result.errors))
            assert result.coverage is not None
            self.assertEqual(result.coverage.execution_passed, 1)


if __name__ == "__main__":
    unittest.main()
