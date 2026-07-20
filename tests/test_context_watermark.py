"""Contract tests for hooks/context_watermark.py."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

WATERMARK = Path(__file__).resolve().parent.parent / "hooks" / "context_watermark.py"


def assistant_usage(ctx_tokens):
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": ctx_tokens, "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0, "output_tokens": 10},
        },
    }


def write_call(file_path):
    return {
        "type": "assistant",
        "message": {"role": "assistant",
                    "content": [{"type": "tool_use", "name": "Write",
                                 "input": {"file_path": file_path, "content": "x"}}]},
    }


class TestWatermark(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.transcript = self.dir / "session.jsonl"

    def write_transcript(self, entries):
        self.transcript.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")

    def run_hook(self, hook_input=None, stdin_raw=None, extra_args=None):
        args = [sys.executable, str(WATERMARK), "--window", "200000", "--threshold", "0.9"]
        if extra_args:
            args += extra_args
        data = stdin_raw if stdin_raw is not None else json.dumps(hook_input)
        return subprocess.run(args, input=data, capture_output=True, text=True, timeout=30)

    def hook_input(self, **over):
        base = {"transcript_path": str(self.transcript), "stop_hook_active": False, "cwd": str(self.dir)}
        base.update(over)
        return base

    def test_t1_below_threshold_passes(self):
        self.write_transcript([assistant_usage(100_000)])
        proc = self.run_hook(self.hook_input())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_t2_above_threshold_without_handoff_blocks(self):
        self.write_transcript([assistant_usage(185_000)])
        proc = self.run_hook(self.hook_input())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        verdict = json.loads(proc.stdout)
        self.assertEqual(verdict["decision"], "block")
        self.assertIn("handoff", verdict["reason"])
        self.assertIn("92", verdict["reason"])  # 185k/200k = 92.5%

    def test_t3_above_threshold_with_handoff_written_passes(self):
        self.write_transcript([
            write_call("/proj/_workspace/my-task/handoff.md"),
            assistant_usage(185_000),
        ])
        proc = self.run_hook(self.hook_input())
        self.assertEqual(proc.stdout.strip(), "")

    def test_t4_stop_hook_active_passes(self):
        self.write_transcript([assistant_usage(185_000)])
        proc = self.run_hook(self.hook_input(stop_hook_active=True))
        self.assertEqual(proc.stdout.strip(), "")

    def test_t5_no_usage_fail_open(self):
        self.write_transcript([{"type": "user", "message": {"role": "user", "content": "hi"}}])
        proc = self.run_hook(self.hook_input())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_t6_check_mode_reports_percentage(self):
        self.write_transcript([assistant_usage(185_000)])
        proc = self.run_hook(extra_args=["--check", str(self.transcript)], stdin_raw="")
        self.assertIn("92.5%", proc.stdout)


if __name__ == "__main__":
    unittest.main()


class TestWatermarkLintIntegration(TestWatermark):
    def test_t7_empty_shell_handoff_still_blocks_with_lint_reason(self):
        handoff = self.dir / "handoff.md"
        handoff.write_text("# handoff\n\n## 목표\nx\n", encoding="utf-8")  # missing floors
        self.write_transcript([
            write_call(str(handoff)),
            assistant_usage(185_000),
        ])
        proc = self.run_hook(self.hook_input())
        verdict = json.loads(proc.stdout)
        self.assertEqual(verdict["decision"], "block")
        self.assertIn("failed the structural lint", verdict["reason"])

    def test_t8_good_handoff_on_disk_passes(self):
        handoff = self.dir / "handoff.md"
        handoff.write_text(
            "# handoff\n\n## 목표\nagent-gate 작업 마무리\n\n## 완료 작업\n- scripts/artifact_lint.py 구현\n\n"
            "## 결정\n- floor 방식 채택, 평균의 함정 방지\n\n## 검증 상태\n- unittest 통과\n\n"
            "## 다음 단계\n- 커밋하고 README 갱신\n", encoding="utf-8")
        self.write_transcript([
            write_call(str(handoff)),
            assistant_usage(185_000),
        ])
        proc = self.run_hook(self.hook_input())
        self.assertEqual(proc.stdout.strip(), "")
