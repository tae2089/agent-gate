"""Contract tests for the structural Design Gate."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from gate_helpers import IMPLEMENTATION, TASK

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "hooks" / "design_gate_hook.py"
SCRIPT = ROOT / "scripts" / "scenario_gate.py"
sys.path.insert(0, str(ROOT / "scripts"))

import scenario_gate  # noqa: E402


class DesignGateTest(unittest.TestCase):
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
                            "id": "S-DESIGN",
                            "title": "Structural design exists",
                            "command": [sys.executable, "-c", "raise SystemExit(0)"],
                            "given": ["a task and flow"],
                            "when": ["design is checked"],
                            "then": ["the design is accepted"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_structural_design_passes_without_assessment(self):
        result = scenario_gate.validate_design(self.task, self.project)

        self.assertTrue(result.allowed, result.errors)
        self.assertEqual(result.required_scenarios, ("S-DESIGN",))
        self.assertFalse((self.task / "assessment.json").exists())

    def test_activation_uses_one_project_local_pointer(self):
        result = scenario_gate.activate_design(self.task, self.project)

        self.assertTrue(result.allowed, result.errors)
        pointer = self.project / "_workspace" / ".active-task"
        self.assertEqual(
            pointer.read_text(encoding="utf-8"), "_workspace/sample-task\n"
        )
        active, errors = scenario_gate.resolve_active_task(self.project)
        self.assertEqual(errors, ())
        self.assertEqual(active, self.task.resolve())
        self.assertFalse((self.project / "_workspace" / ".readiness-sessions").exists())

    def test_activation_preserves_a_different_active_design(self):
        self.assertTrue(scenario_gate.activate_design(self.task, self.project).allowed)
        other = self.project / "_workspace" / "other-task"
        other.mkdir()
        for filename in ("task.md", "implementation.md", "scenario-contract.json"):
            (other / filename).write_bytes((self.task / filename).read_bytes())

        blocked = scenario_gate.activate_design(other, self.project)
        repeated = scenario_gate.activate_design(self.task, self.project)

        self.assertFalse(blocked.allowed)
        self.assertIn("another design is active", blocked.errors)
        self.assertTrue(repeated.allowed, repeated.errors)
        active, errors = scenario_gate.resolve_active_task(self.project)
        self.assertFalse(errors)
        self.assertEqual(active, self.task.resolve())

    def test_release_cli_clears_only_the_exact_active_design(self):
        self.assertTrue(scenario_gate.activate_design(self.task, self.project).allowed)
        other = self.project / "_workspace" / "other-task"
        other.mkdir()
        pointer = self.project / "_workspace" / ".active-task"

        wrong = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "release",
                str(other),
                "--project-root",
                str(self.project),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        released = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "release",
                str(self.task),
                "--project-root",
                str(self.project),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        self.assertEqual(wrong.returncode, 1)
        self.assertIn("not the active design", wrong.stdout)
        self.assertTrue(released.returncode == 0, released.stdout + released.stderr)
        self.assertFalse(pointer.exists())

    def test_pre_edit_hook_uses_active_design_without_session_id(self):
        event = {
            "cwd": str(self.project),
            "tool_name": "Edit",
            "tool_input": {"file_path": str(self.project / "src" / "app.py")},
        }

        blocked = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(event),
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = json.loads(blocked.stdout)["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "deny")
        self.assertIn("no active design", output["permissionDecisionReason"])

        self.assertTrue(scenario_gate.activate_design(self.task, self.project).allowed)
        allowed = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(event),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertEqual(allowed.stdout, "")

    def test_design_cli_activates_only_valid_structure(self):
        process = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "design",
                str(self.task),
                "--project-root",
                str(self.project),
                "--activate",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertIn("PASS", process.stdout)
        self.assertTrue((self.project / "_workspace" / ".active-task").is_file())

        (self.task / "implementation.md").unlink()
        invalid = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "design",
                str(self.task),
                "--project-root",
                str(self.project),
                "--activate",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(invalid.returncode, 1)
        self.assertIn("cannot lint implementation.md", invalid.stdout)

    def test_each_design_artifact_is_required(self):
        for filename, expected in (
            ("task.md", "cannot lint task.md"),
            ("implementation.md", "cannot lint implementation.md"),
            ("scenario-contract.json", "cannot read scenario-contract.json"),
        ):
            with self.subTest(filename=filename):
                path = self.task / filename
                content = path.read_bytes()
                path.unlink()
                result = scenario_gate.validate_design(self.task, self.project)
                path.write_bytes(content)
                self.assertFalse(result.allowed)
                self.assertTrue(
                    any(expected in error for error in result.errors),
                    result.errors,
                )

    def test_active_pointer_rejects_escape_and_symlink(self):
        pointer = self.project / "_workspace" / ".active-task"
        pointer.write_text("../outside\n", encoding="utf-8")
        task, errors = scenario_gate.resolve_active_task(self.project)
        self.assertIsNone(task)
        self.assertIn("must name _workspace/<task>", errors[0])

        pointer.unlink()
        outside = self.project / "outside"
        outside.write_text("foreign", encoding="utf-8")
        pointer.symlink_to(outside)
        task, errors = scenario_gate.resolve_active_task(self.project)
        self.assertIsNone(task)
        self.assertIn("must not be a symlink", errors[0])


if __name__ == "__main__":
    unittest.main()
