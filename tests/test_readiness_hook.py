"""Contract tests for session-bound pre-edit readiness hooks."""

import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from readiness_helpers import (
    CHILD_TASK,
    assessment_for,
    inheritance_for,
    write_artifacts,
)

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "hooks" / "readiness_gate_hook.py"
sys.path.insert(0, str(ROOT / "hooks"))
sys.path.insert(0, str(ROOT / "scripts"))
import readiness_gate_hook as gate_hook  # noqa: E402


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

    def inherited_task(self):
        child = self.project / "_workspace" / "child-task"
        child.mkdir(parents=True, exist_ok=True)
        (child / "task.md").write_text(CHILD_TASK, encoding="utf-8")
        manifest = inheritance_for(child, parent_task="sample-task")
        path = child / "inherited-readiness.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return child, path

    def assert_blocked(self, proc, fragment):
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        verdict = json.loads(proc.stdout)
        self.assertEqual(verdict["decision"], "block")
        self.assertIn(fragment, verdict["reason"])
        return verdict

    def assert_pretool_blocked(self, proc, fragment):
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        verdict = json.loads(proc.stdout)
        output = verdict["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "PreToolUse")
        self.assertEqual(output["permissionDecision"], "deny")
        self.assertIn(fragment, output["permissionDecisionReason"])
        return output


class TestPreEditGate(ReadinessHookHarness):
    def test_unbound_source_edit_is_blocked(self):
        proc = self.run_hook("pre", self.event(self.project / "src" / "app.py"))
        verdict = self.assert_pretool_blocked(proc, "no readiness task is bound")
        self.assertIn("artifact-judge", verdict["permissionDecisionReason"])

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
        self.assert_pretool_blocked(proc, "task.sha256")

    def test_workspace_authoring_is_allowed_before_binding(self):
        proc = self.run_hook("pre", self.event(self.task_dir / "notes.py"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_non_source_edit_is_not_guarded(self):
        proc = self.run_hook("pre", self.event(self.project / "README.md"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_unbound_non_document_edits_are_guarded_by_default(self):
        protected = (
            "Makefile",
            "Dockerfile",
            "infra/main.tf",
            "schema/query.sql",
            "api/service.proto",
            "build.gradle",
            ".gitignore",
        )
        for relative in protected:
            with self.subTest(relative=relative):
                proc = self.run_hook("pre", self.event(self.project / relative))
                self.assert_pretool_blocked(proc, "no readiness task is bound")

    def test_explicit_project_document_exemptions_are_allowed(self):
        documents = (
            "README.md",
            "guide.rst",
            "notes.txt",
            "README",
            "LICENSE",
            "NOTICE",
            "AUTHORS",
            "CONTRIBUTORS",
            "CHANGELOG",
            "CONTRIBUTING",
            "CODE_OF_CONDUCT",
        )
        for relative in documents:
            with self.subTest(relative=relative):
                proc = self.run_hook("pre", self.event(self.project / relative))
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout.strip(), "")

    def test_document_target_outside_project_is_blocked(self):
        outside = Path(tempfile.mkdtemp()) / "README.md"
        proc = self.run_hook("pre", self.event(outside))
        self.assert_pretool_blocked(proc, "outside the project")

    def test_mixed_patch_with_a_protected_target_is_blocked(self):
        event = {
            "session_id": "session-1",
            "cwd": str(self.project),
            "tool_name": "apply_patch",
            "tool_input": {
                "command": (
                    "*** Begin Patch\n"
                    "*** Update File: README.md\n"
                    "*** Update File: Makefile\n"
                    "*** End Patch"
                )
            },
        }
        proc = self.run_hook("pre", event)
        self.assert_pretool_blocked(proc, "no readiness task is bound")

    def test_guarded_path_outside_project_is_blocked(self):
        outside = Path(tempfile.mkdtemp()) / "outside.py"
        proc = self.run_hook("pre", self.event(outside))
        self.assert_pretool_blocked(proc, "outside the project")

    def test_edit_tool_without_a_target_fails_open_with_diagnostic(self):
        proc = self.run_hook("pre", self.event(tool_name="Edit", tool_input={}))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")
        self.assertIn("fail-open", proc.stderr)

    def test_malformed_hook_input_fails_open_with_diagnostic(self):
        proc = subprocess.run(
            [sys.executable, str(HOOK), "--mode", "pre"],
            input="not json {",
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")
        self.assertIn("fail-open", proc.stderr)

    def test_non_object_hook_input_fails_open_with_diagnostic(self):
        proc = self.run_hook("pre", [])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")
        self.assertIn("fail-open", proc.stderr)

    def test_missing_project_root_fails_open_with_diagnostic(self):
        event = self.event(self.project / "src" / "app.py")
        event.pop("cwd")
        proc = self.run_hook("pre", event)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")
        self.assertIn("fail-open", proc.stderr)

    def test_internal_failure_after_protected_target_is_fail_closed(self):
        output = io.StringIO()
        with patch.object(gate_hook, "load_binding", side_effect=RuntimeError("boom")):
            with redirect_stdout(output):
                return_code = gate_hook.run_pre(
                    self.event(self.project / "src" / "app.py")
                )

        self.assertEqual(return_code, 0)
        verdict = json.loads(output.getvalue())
        hook_output = verdict["hookSpecificOutput"]
        self.assertEqual(hook_output["permissionDecision"], "deny")
        self.assertIn("failed safely", hook_output["permissionDecisionReason"])

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
        self.assert_pretool_blocked(proc, "no readiness task is bound")

    def test_symlink_marker_is_rejected_without_overwrite(self):
        marker = marker_path(self.project, "session-1")
        marker.parent.mkdir(parents=True)
        outside = Path(tempfile.mkdtemp()) / "outside.json"
        outside.write_text("DO_NOT_OVERWRITE", encoding="utf-8")
        marker.symlink_to(outside)

        self.bind_ready()
        proc = self.run_hook("pre", self.event(self.project / "src" / "app.py"))
        self.assert_pretool_blocked(proc, "unsafe readiness session marker")
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

    def test_valid_inheritance_write_binds_child_task(self):
        child, manifest = self.inherited_task()
        proc = self.run_hook("bind", self.event(manifest))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")
        marker = marker_path(self.project, "session-1")
        self.assertEqual(
            json.loads(marker.read_text(encoding="utf-8")),
            {"task_dir": "_workspace/child-task"},
        )
        allowed = self.run_hook("pre", self.event(self.project / "src" / "app.py"))
        self.assertEqual(allowed.stdout.strip(), "")

    def test_invalid_inheritance_does_not_bind(self):
        child, manifest = self.inherited_task()
        value = json.loads(manifest.read_text(encoding="utf-8"))
        value["flow_refs"] = ["P99"]
        manifest.write_text(json.dumps(value), encoding="utf-8")
        proc = self.run_hook("bind", self.event(manifest))
        self.assert_blocked(proc, "readiness proof is not ready")
        self.assertFalse(marker_path(self.project, "session-1").exists())

    def test_parent_change_blocks_child_bound_session(self):
        _, manifest = self.inherited_task()
        proc = self.run_hook("bind", self.event(manifest))
        self.assertEqual(proc.stdout.strip(), "")
        with (self.task_dir / "implementation.md").open("a", encoding="utf-8") as stream:
            stream.write("\n- parent changed after child binding\n")
        blocked = self.run_hook("pre", self.event(self.project / "src" / "app.py"))
        self.assert_pretool_blocked(blocked, "parent readiness")


if __name__ == "__main__":
    unittest.main()
