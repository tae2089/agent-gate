"""Contract tests for hooks/skill_invocation_verifier.py.

Each test drives the verifier as a subprocess, the same way the Claude Code
hook runner does: hook input JSON on stdin, verdict JSON on stdout.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from transcript_helpers import tool_result, tool_use, user_text

VERIFIER = Path(__file__).resolve().parent.parent / "hooks" / "skill_invocation_verifier.py"


GUARDRAILS_RULE = {
    "id": "code-edits-need-guardrails",
    "when": {"tool": "Write|Edit", "input_pattern": r"\.(go|py|ts)"},
    "require": {"skill": "coding-quality-guardrails"},
}
DEBUG_RULE = {
    "id": "debugging-needs-skill",
    "when": {"prompt_pattern": r"(?i)(디버깅|debug|버그)"},
    "require": {"skill": "diagnosing-bugs"},
}
CONTEXT7_RULE = {
    "id": "library-docs-need-context7",
    "when": {"prompt_pattern": r"(?i)library docs"},
    "require": {"tool_pattern": r"^mcp__context7__"},
}


class VerifierHarness(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.transcript = self.dir / "session.jsonl"
        self.rules_path = self.dir / "rules.json"

    def write_transcript(self, entries):
        self.transcript.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")

    def write_rules(self, rules):
        self.rules_path.write_text(json.dumps({"rules": rules}), encoding="utf-8")

    def run_hook(self, hook_input=None, stdin_raw=None, extra_args=None):
        args = [sys.executable, str(VERIFIER), "--rules", str(self.rules_path)]
        if extra_args:
            args += extra_args
        data = stdin_raw if stdin_raw is not None else json.dumps(hook_input)
        return subprocess.run(args, input=data, capture_output=True, text=True, timeout=30)

    def hook_input(self, **over):
        base = {"transcript_path": str(self.transcript), "stop_hook_active": False, "cwd": str(self.dir)}
        base.update(over)
        return base

    def assert_blocked(self, proc, needle):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        verdict = json.loads(proc.stdout)
        self.assertEqual(verdict["decision"], "block")
        self.assertIn(needle, verdict["reason"])

    def assert_passed(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")


class TestVerifier(VerifierHarness):
    def test_t1_prompt_trigger_missing_skill_blocks(self):
        self.write_rules([DEBUG_RULE])
        self.write_transcript([
            user_text("이 버그 좀 디버깅 해줘"),
            tool_use("Read", {"file_path": "/x/main.go"}),
        ])
        self.assert_blocked(self.run_hook(self.hook_input()), "diagnosing-bugs")

    def test_t2_skill_called_earlier_in_session_passes(self):
        self.write_rules([DEBUG_RULE])
        self.write_transcript([
            user_text("버그 잡자"),
            tool_use("Skill", {"skill": "diagnosing-bugs"}),
            tool_result(),
            user_text("계속 디버깅 해줘"),
            tool_use("Read", {"file_path": "/x/main.go"}),
        ])
        self.assert_passed(self.run_hook(self.hook_input()))

    def test_t3_stop_hook_active_with_missing_skill_still_blocks(self):
        self.write_rules([DEBUG_RULE])
        self.write_transcript([user_text("디버깅 해줘")])
        self.assert_blocked(
            self.run_hook(self.hook_input(stop_hook_active=True)),
            "diagnosing-bugs",
        )

    def test_t4_tool_trigger_missing_skill_blocks(self):
        self.write_rules([GUARDRAILS_RULE])
        self.write_transcript([
            user_text("함수 하나 추가해줘"),
            tool_use("Edit", {"file_path": "/x/server.go", "old_string": "a", "new_string": "b"}),
        ])
        self.assert_blocked(self.run_hook(self.hook_input()), "coding-quality-guardrails")

    def test_t5_mcp_tool_requirement(self):
        self.write_rules([CONTEXT7_RULE])
        entries = [user_text("check the library docs for gin")]
        self.write_transcript(entries)
        self.assert_blocked(self.run_hook(self.hook_input()), "library-docs-need-context7")

        entries.append(tool_use("mcp__context7__query-docs", {"query": "gin"}))
        self.write_transcript(entries)
        self.assert_passed(self.run_hook(self.hook_input()))

    def test_t6_fail_open_on_bad_inputs(self):
        # rules file missing
        self.write_transcript([user_text("디버깅 해줘")])
        proc = self.run_hook(self.hook_input())
        self.assert_passed(proc)
        # malformed stdin
        self.write_rules([DEBUG_RULE])
        proc = self.run_hook(stdin_raw="not json {")
        self.assert_passed(proc)
        # transcript missing
        proc = self.run_hook(self.hook_input(transcript_path=str(self.dir / "nope.jsonl")))
        self.assert_passed(proc)

    def test_t7_invalid_regex_rule_skipped_others_evaluated(self):
        bad = {"id": "bad", "when": {"prompt_pattern": "("}, "require": {"skill": "x"}}
        self.write_rules([bad, DEBUG_RULE])
        self.write_transcript([user_text("디버깅 해줘")])
        proc = self.run_hook(self.hook_input())
        self.assert_blocked(proc, "diagnosing-bugs")
        self.assertNotIn("bad", json.loads(proc.stdout)["reason"])

    def test_t8_trigger_scope_is_current_turn_only(self):
        self.write_rules([GUARDRAILS_RULE])
        self.write_transcript([
            user_text("서버 코드 고쳐줘"),
            tool_use("Edit", {"file_path": "/x/server.go"}),  # previous turn
            tool_result(),
            user_text("이제 README만 읽어줘"),  # current turn: no code edit
            tool_use("Read", {"file_path": "/x/README.md"}),
        ])
        self.assert_passed(self.run_hook(self.hook_input()))

    def test_t9_check_mode_reports_and_exits_1(self):
        self.write_rules([DEBUG_RULE])
        self.write_transcript([user_text("디버깅 해줘")])
        proc = self.run_hook(extra_args=["--check", str(self.transcript)], stdin_raw="")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("debugging-needs-skill", proc.stdout)

    def test_t10_no_user_prompt_passes(self):
        self.write_rules([DEBUG_RULE])
        self.write_transcript([tool_result()])
        self.assert_passed(self.run_hook(self.hook_input()))
        self.transcript.write_text("", encoding="utf-8")
        self.assert_passed(self.run_hook(self.hook_input()))

    def test_t11_malformed_rule_shapes_are_skipped(self):
        malformed = [
            None,
            {"id": "bad-when", "when": [], "require": {"skill": "x"}},
            {"id": "bad-pattern", "when": {"prompt_pattern": []},
             "require": {"skill": "x"}},
        ]
        self.write_rules([*malformed, DEBUG_RULE])
        self.write_transcript([user_text("디버깅 해줘")])
        proc = self.run_hook(self.hook_input())
        self.assert_blocked(proc, "diagnosing-bugs")
        self.assertNotIn("bad-", proc.stdout)

    def test_t12_non_object_rule_file_fails_open(self):
        self.rules_path.write_text("[]", encoding="utf-8")
        self.write_transcript([user_text("디버깅 해줘")])
        self.assert_passed(self.run_hook(self.hook_input()))


if __name__ == "__main__":
    unittest.main()
