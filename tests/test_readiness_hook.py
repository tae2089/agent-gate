"""Contract tests for session-bound pre-edit readiness hooks."""

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from readiness_helpers import assessment_for, write_artifacts

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "hooks" / "readiness_gate_hook.py"


def marker_path(root: Path, session_id: str) -> Path:
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return root / "_workspace" / ".readiness-sessions" / f"{digest}.json"


class ReadinessHookHarness(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.task_dir = self.project / "_workspace" / "sample-task"
        write_artifacts(self.task_dir)
        self.assessment_path = self.task_dir / "assessment.json"
        self.write_assessment()

    def tearDown(self):
        self.temp.cleanup()

    def write_assessment(self, value=None):
        assessment = assessment_for(self.task_dir) if value is None else value
        self.assessment_path.write_text(json.dumps(assessment), encoding="utf-8")

    def event(self, path=None, session_id="session-1", **overrides):
        value = {
            "session_id": session_id,
            "cwd": str(self.project),
            "tool_name": "Write",
            "tool_input": {"file_path": str(path)} if path is not None else {},
        }
        value.update(overrides)
        return value

    def run_hook(self, mode, event):
        return subprocess.run(
            [sys.executable, str(HOOK), "--mode", mode],
            input=json.dumps(event), capture_output=True, text=True, timeout=30,
        )

    def bind_ready(self, session_id="session-1", **overrides):
        event = self.event(self.assessment_path, session_id=session_id, **overrides)
        proc = self.run_hook("bind", event)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def assert_blocked(self, proc, fragment):
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        verdict = json.loads(proc.stdout)
        self.assertEqual(verdict["decision"], "block")
        self.assertIn(fragment, verdict["reason"])
        return verdict


class TestPreEditGate(ReadinessHookHarness):
    def test_unbound_source_edit_is_blocked(self):
        proc = self.run_hook("pre", self.event(self.project / "src" / "app.py"))
        verdict = self.assert_blocked(proc, "no readiness task is bound")
        self.assertIn("artifact-judge", verdict["reason"])

    def test_valid_bound_assessment_allows_source_edit(self):
        self.bind_ready()
        proc = self.run_hook("pre", self.event(self.project / "src" / "app.py"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_stale_bound_assessment_blocks_with_validator_reason(self):
        self.bind_ready()
        with (self.task_dir / "task.md").open("a", encoding="utf-8") as stream:
            stream.write("\n- Clarification added after assessment.\n")
        proc = self.run_hook("pre", self.event(self.project / "src" / "app.py"))
        self.assert_blocked(proc, "task.sha256")

    def test_workspace_authoring_is_allowed_before_binding(self):
        proc = self.run_hook("pre", self.event(self.task_dir / "notes.py"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_non_source_edit_is_not_guarded(self):
        proc = self.run_hook("pre", self.event(self.project / "README.md"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_guarded_path_outside_project_is_blocked(self):
        outside = Path(tempfile.mkdtemp()) / "outside.py"
        proc = self.run_hook("pre", self.event(outside))
        self.assert_blocked(proc, "outside the project")

    def test_edit_tool_without_a_target_is_fail_closed(self):
        proc = self.run_hook("pre", self.event(tool_name="Edit", tool_input={}))
        self.assert_blocked(proc, "could not determine")

    def test_codex_apply_patch_command_payload_is_normalized(self):
        self.bind_ready()
        event = {
            "session_id": "session-1",
            "cwd": str(self.project),
            "tool_name": "apply_patch",
            "tool_input": {
                "command": "*** Begin Patch\n*** Update File: src/app.py\n@@\n-old\n+new\n*** End Patch"
            },
        }
        proc = self.run_hook("pre", event)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")


class TestPostEditBinding(ReadinessHookHarness):
    def test_valid_assessment_write_binds_relative_task_directory(self):
        self.bind_ready()
        marker = marker_path(self.project, "session-1")
        self.assertTrue(marker.is_file())
        self.assertEqual(
            json.loads(marker.read_text(encoding="utf-8")),
            {"task_dir": "_workspace/sample-task"},
        )

    def test_invalid_assessment_reports_errors_and_does_not_bind(self):
        self.write_assessment({"schema_version": 1})
        proc = self.run_hook("bind", self.event(self.assessment_path))
        verdict = self.assert_blocked(proc, "assessment is not ready")
        self.assertIn("task must be an object", verdict["reason"])
        self.assertFalse(marker_path(self.project, "session-1").exists())

    def test_failed_tool_response_does_not_bind(self):
        self.bind_ready(tool_response={"is_error": True})
        self.assertFalse(marker_path(self.project, "session-1").exists())

    def test_binding_is_isolated_by_session(self):
        self.bind_ready(session_id="session-1")
        proc = self.run_hook(
            "pre", self.event(self.project / "src" / "app.py", session_id="session-2")
        )
        self.assert_blocked(proc, "no readiness task is bound")

    def test_symlink_marker_is_rejected_without_overwrite(self):
        marker = marker_path(self.project, "session-1")
        marker.parent.mkdir(parents=True)
        outside = Path(tempfile.mkdtemp()) / "outside.json"
        outside.write_text("DO_NOT_OVERWRITE", encoding="utf-8")
        marker.symlink_to(outside)

        self.bind_ready()
        proc = self.run_hook("pre", self.event(self.project / "src" / "app.py"))
        self.assert_blocked(proc, "unsafe readiness session marker")
        self.assertEqual(outside.read_text(encoding="utf-8"), "DO_NOT_OVERWRITE")

    def test_symlink_task_directory_does_not_bind(self):
        outside_root = Path(tempfile.mkdtemp())
        outside_task = outside_root / "linked-task"
        write_artifacts(outside_task)
        assessment = assessment_for(outside_task)
        (outside_task / "assessment.json").write_text(json.dumps(assessment), encoding="utf-8")
        linked = self.project / "_workspace" / "linked-task"
        linked.symlink_to(outside_task)

        proc = self.run_hook("bind", self.event(linked / "assessment.json"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(marker_path(self.project, "session-1").exists())


if __name__ == "__main__":
    unittest.main()
