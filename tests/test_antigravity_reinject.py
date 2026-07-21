"""Tests for hooks/antigravity_reinject.py — PreInvocation reinject that
injects the handoff once per Antigravity compaction (CHECKPOINT) via
injectSteps. Output contract pinned to antigravity.google/docs/hooks.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REINJECT = Path(__file__).resolve().parent.parent / "hooks" / "antigravity_reinject.py"

HANDOFF = """# handoff

## 목표
작업 이어가기

## 완료 작업
- hooks/x.py 수정

## 결정
- 사용자: "B는 짜치는 작업"이라 스킵

## 검증 상태
- 테스트 실행

## 다음 단계
- 남은 것 진행
"""


def ag_line(step, etype, **extra):
    return {"step_index": step, "source": "SYSTEM", "type": etype, "status": "DONE", **extra}


class ReinjectHarness(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.transcript = self.dir / "transcript_full.jsonl"

    def write_transcript(self, entries):
        self.transcript.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")

    def write_handoff(self):
        (self.dir / "handoff.md").write_text(HANDOFF, encoding="utf-8")

    def run_hook(self, session_id="c1", stdin_raw=None):
        event = {"transcriptPath": str(self.transcript), "workspacePaths": [str(self.dir)],
                 "conversationId": session_id, "invocationNum": 1}
        data = stdin_raw if stdin_raw is not None else json.dumps(event)
        return subprocess.run([sys.executable, str(REINJECT)], input=data,
                              capture_output=True, text=True, timeout=30)

    def injected(self, proc):
        out = json.loads(proc.stdout)
        steps = out.get("injectSteps")
        return steps[0]["ephemeralMessage"] if steps else None


class ReinjectTest(ReinjectHarness):
    def test_no_checkpoint_no_injection(self):
        self.write_transcript([ag_line(0, "USER_INPUT")])
        self.write_handoff()
        self.assertIsNone(self.injected(self.run_hook()))

    def test_checkpoint_with_handoff_injects_once(self):
        self.write_transcript([ag_line(0, "USER_INPUT"), ag_line(4, "CHECKPOINT", content="{{ CHECKPOINT 0 }}")])
        self.write_handoff()
        message = self.injected(self.run_hook())
        self.assertIsNotNone(message)
        self.assertIn("Context was just compacted", message)
        self.assertIn("짜치는 작업", message)  # the value judgment survives

    def test_same_checkpoint_not_reinjected(self):
        self.write_transcript([ag_line(4, "CHECKPOINT")])
        self.write_handoff()
        self.assertIsNotNone(self.injected(self.run_hook()))   # first fires
        self.assertIsNone(self.injected(self.run_hook()))      # marker suppresses repeat

    def test_new_checkpoint_reinjects(self):
        self.write_transcript([ag_line(4, "CHECKPOINT")])
        self.write_handoff()
        self.assertIsNotNone(self.injected(self.run_hook()))
        self.write_transcript([ag_line(4, "CHECKPOINT"), ag_line(9, "CHECKPOINT")])
        self.assertIsNotNone(self.injected(self.run_hook()))   # later checkpoint re-fires

    def test_checkpoint_without_handoff_no_injection(self):
        self.write_transcript([ag_line(4, "CHECKPOINT")])
        self.assertIsNone(self.injected(self.run_hook()))

    def test_malformed_stdin_fails_open(self):
        proc = self.run_hook(stdin_raw="not json")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "{}")


if __name__ == "__main__":
    unittest.main()
