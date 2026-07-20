"""Contract tests for hooks/handoff_reinject.py."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REINJECT = Path(__file__).resolve().parent.parent / "hooks" / "handoff_reinject.py"


class TestReinject(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def run_hook(self, stdin_raw=None, extra_args=None):
        args = [sys.executable, str(REINJECT)]
        if extra_args:
            args += extra_args
        data = stdin_raw if stdin_raw is not None else json.dumps(
            {"cwd": str(self.dir), "source": "compact", "hook_event_name": "SessionStart"})
        return subprocess.run(args, input=data, capture_output=True, text=True, timeout=30)

    def make_handoff(self, rel, content, age_seconds=0):
        path = self.dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        if age_seconds:
            past = time.time() - age_seconds
            os.utime(path, (past, past))
        return path

    def test_t1_recent_handoff_injected(self):
        self.make_handoff("_workspace/my-task/handoff.md", "B는 짜치는 작업 — 스킵", age_seconds=3600)
        self.make_handoff("_workspace/old-task/handoff.md", "OLD_MARKER", age_seconds=7200)
        proc = self.run_hook()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("B는 짜치는 작업", proc.stdout)
        self.assertIn("my-task/handoff.md", proc.stdout)  # path cited
        self.assertNotIn("OLD_MARKER", proc.stdout)

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


if __name__ == "__main__":
    unittest.main()
