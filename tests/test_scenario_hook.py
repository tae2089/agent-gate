"""Hook-level integration tests for scenario readiness and completion."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from readiness_helpers import write_ready_artifacts
from scenario_helpers import (
    init_git_project,
    write_parent_project,
    write_policy,
)

ROOT = Path(__file__).resolve().parent.parent
READINESS_HOOK = ROOT / "hooks" / "readiness_gate_hook.py"
SCENARIO_HOOK = ROOT / "hooks" / "scenario_gate_hook.py"
sys.path.insert(0, str(ROOT / "scripts"))

from scenario_gate import run_scenarios  # noqa: E402


def marker_path(root: Path, session_id: str = "session-1") -> Path:
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return root / "_workspace" / ".readiness-sessions" / f"{digest}.json"


class ScenarioHookTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.session_id = "session-1"

    def tearDown(self):
        self.temp.cleanup()

    def event(self, path: Path | None = None) -> dict:
        value = {"cwd": str(self.project), "session_id": self.session_id}
        if path is not None:
            value.update(
                {"tool_name": "Write", "tool_input": {"file_path": str(path)}}
            )
        return value

    def run_hook(self, script: Path, event: dict, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(script), *args],
            input=json.dumps(event),
            capture_output=True,
            text=True,
            timeout=30,
        )

    def bind(self, path: Path) -> subprocess.CompletedProcess[str]:
        return self.run_hook(READINESS_HOOK, self.event(path), "--mode", "bind")

    def pre(self, path: Path) -> subprocess.CompletedProcess[str]:
        return self.run_hook(READINESS_HOOK, self.event(path), "--mode", "pre")

    def stop(self) -> subprocess.CompletedProcess[str]:
        return self.run_hook(SCENARIO_HOOK, self.event())

    def assert_block(self, process: subprocess.CompletedProcess[str], fragment: str) -> None:
        self.assertEqual(process.returncode, 0, process.stderr)
        value = json.loads(process.stdout)
        if "hookSpecificOutput" in value:
            reason = value["hookSpecificOutput"]["permissionDecisionReason"]
        else:
            self.assertEqual(value["decision"], "block")
            reason = value["reason"]
        self.assertIn(fragment, reason)

    def test_enforce_binds_review_then_blocks_stale_contract_edit(self):
        task = write_parent_project(self.project, mode="enforce")
        bound = self.bind(task / "scenario-review.json")
        self.assertEqual(bound.stdout.strip(), "", bound.stdout + bound.stderr)
        self.assertTrue(marker_path(self.project).is_file())
        allowed = self.pre(self.project / "src" / "app.py")
        self.assertEqual(allowed.stdout.strip(), "", allowed.stdout + allowed.stderr)

        contract = task / "scenario-contract.json"
        contract.write_text(contract.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        self.assert_block(self.pre(self.project / "src" / "app.py"), "scenario")

    def test_enforce_does_not_bind_readiness_without_scenario_proof(self):
        task = self.project / "_workspace" / "task"
        write_ready_artifacts(task)
        write_policy(self.project, mode="enforce")
        process = self.bind(task / "assessment.json")
        self.assert_block(process, "scenario")
        self.assertFalse(marker_path(self.project).exists())

    def test_advisory_binds_even_when_scenario_proof_is_missing(self):
        task = self.project / "_workspace" / "task"
        write_ready_artifacts(task)
        write_policy(self.project, mode="advisory")
        process = self.bind(task / "assessment.json")
        self.assertEqual(process.stdout.strip(), "", process.stdout + process.stderr)
        self.assertTrue(marker_path(self.project).is_file())
        self.assertIn("scenario advisory", process.stderr)

    def test_stop_blocks_until_fresh_result_exists(self):
        task = write_parent_project(self.project, mode="enforce")
        self.assertEqual(self.bind(task / "scenario-review.json").stdout.strip(), "")
        init_git_project(self.project)
        self.assert_block(self.stop(), "scenario completion")

        run = run_scenarios(task, self.project)
        self.assertTrue(run.result_written, run.errors)
        completed = self.stop()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "")

    def test_disabled_or_malformed_stop_input_fails_open(self):
        disabled = self.stop()
        self.assertEqual(disabled.stdout.strip(), "")
        malformed = subprocess.run(
            [sys.executable, str(SCENARIO_HOOK)],
            input="not json",
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(malformed.returncode, 0)
        self.assertEqual(malformed.stdout.strip(), "")
        self.assertIn("fail-open", malformed.stderr)


if __name__ == "__main__":
    unittest.main()
