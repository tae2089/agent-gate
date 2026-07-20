"""Contract tests for hooks/handoff_reinject.py."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
from handoff_state import marker_path  # noqa: E402

REINJECT = Path(__file__).resolve().parent.parent / "hooks" / "handoff_reinject.py"


class TestReinject(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.session_id = "session-1"

    def run_hook(self, stdin_raw=None, extra_args=None):
        args = [sys.executable, str(REINJECT)]
        if extra_args:
            args += extra_args
        data = stdin_raw if stdin_raw is not None else json.dumps(
            {"cwd": str(self.dir), "source": "compact", "hook_event_name": "SessionStart",
             "session_id": self.session_id})
        return subprocess.run(args, input=data, capture_output=True, text=True, timeout=30)

    def make_handoff(self, rel, content, age_seconds=0):
        path = self.dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        if age_seconds:
            past = time.time() - age_seconds
            os.utime(path, (past, past))
        return path

    def mark_session_handoff(self, rel):
        marker = marker_path(self.dir, self.session_id)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"path": rel}), encoding="utf-8")
        return marker

    def test_t1_recent_handoff_injected(self):
        self.make_handoff("_workspace/my-task/handoff.md", "B는 짜치는 작업 — 스킵", age_seconds=7200)
        self.make_handoff("_workspace/other-task/handoff.md", "OTHER_MARKER", age_seconds=3600)
        self.mark_session_handoff("_workspace/my-task/handoff.md")
        proc = self.run_hook()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("B는 짜치는 작업", proc.stdout)
        self.assertIn("my-task/handoff.md", proc.stdout)  # path cited
        self.assertNotIn("OTHER_MARKER", proc.stdout)

    def test_t2_no_handoff_silent(self):
        proc = self.run_hook()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_t3_stale_handoff_ignored(self):
        self.make_handoff("_workspace/my-task/handoff.md", "ancient", age_seconds=3 * 24 * 3600)
        proc = self.run_hook()
        self.assertEqual(proc.stdout.strip(), "")

    def test_t4_size_capped(self):
        self.make_handoff("handoff.md", "x" * 20000)
        proc = self.run_hook()
        self.assertLess(len(proc.stdout), 10000)
        self.assertIn("truncated", proc.stdout)

    def test_t5_bad_stdin_fail_open(self):
        proc = self.run_hook(stdin_raw="not json {")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_t6_multiple_fresh_handoffs_without_marker_are_ambiguous(self):
        self.make_handoff("_workspace/task-a/handoff.md", "TASK_A")
        self.make_handoff("_workspace/task-b/handoff.md", "TASK_B")
        proc = self.run_hook()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_t7_symlink_handoff_is_rejected(self):
        outside = Path(tempfile.mkdtemp()) / "outside.md"
        outside.write_text("BENIGN_OUTSIDE_MARKER", encoding="utf-8")
        link = self.dir / "_workspace" / "task" / "handoff.md"
        link.parent.mkdir(parents=True)
        link.symlink_to(outside)
        self.mark_session_handoff("_workspace/task/handoff.md")
        proc = self.run_hook()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_t8_non_utf8_handoff_fails_open_silently(self):
        handoff = self.dir / "_workspace" / "task" / "handoff.md"
        handoff.parent.mkdir(parents=True)
        handoff.write_bytes(b"\xff\xfe")
        self.mark_session_handoff("_workspace/task/handoff.md")
        proc = self.run_hook()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_t9_invalid_session_marker_does_not_fall_back_to_other_task(self):
        self.make_handoff("_workspace/current/handoff.md", "stale", age_seconds=3 * 24 * 3600)
        self.make_handoff("_workspace/other/handoff.md", "OTHER_MARKER")
        self.mark_session_handoff("_workspace/current/handoff.md")
        proc = self.run_hook()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
