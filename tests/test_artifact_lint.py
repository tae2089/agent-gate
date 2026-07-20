"""Contract tests for scripts/artifact_lint.py."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

LINT = Path(__file__).resolve().parent.parent / "scripts" / "artifact_lint.py"

GOOD_HANDOFF = """# handoff

## 목표
agent-gate에 artifact-lint 추가

## 완료 작업
- scripts/artifact_lint.py 구현
- tests/test_artifact_lint.py 작성

## 결정
- floor 방식 채택 — 평균의 함정 방지

## 검증 상태
- unittest 21/21 통과, lint 스모크 미실행

## 다음 단계
- watermark 연동 후 커밋
"""


class TestArtifactLint(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def lint(self, content, artifact_type="handoff", json_out=False):
        f = self.dir / "handoff.md"
        f.write_text(content, encoding="utf-8")
        args = [sys.executable, str(LINT), "--type", artifact_type, str(f)]
        if json_out:
            args.append("--json")
        return subprocess.run(args, capture_output=True, text=True, timeout=30)

    def test_t1_good_handoff_passes(self):
        proc = self.lint(GOOD_HANDOFF)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PASS", proc.stdout)

    def test_t2_missing_next_steps_fails_floor(self):
        content = GOOD_HANDOFF.split("## 다음 단계")[0]
        proc = self.lint(content)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("next", proc.stdout)

    def test_t3_empty_file_fails(self):
        proc = self.lint("")
        self.assertEqual(proc.returncode, 1)

    def test_t4_unknown_type_errors(self):
        proc = self.lint(GOOD_HANDOFF, artifact_type="nope")
        self.assertEqual(proc.returncode, 2)

    def test_t5_json_output(self):
        proc = self.lint(GOOD_HANDOFF, json_out=True)
        data = json.loads(proc.stdout)
        self.assertGreaterEqual(data["score"], 0.8)
        self.assertTrue(data["passed"])
        self.assertIn("checks", data)


if __name__ == "__main__":
    unittest.main()
