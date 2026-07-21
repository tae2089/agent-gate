"""Tests for hooks/prompt_router.py — a UserPromptSubmit hook that proactively
hints which skill a prompt should use, so the model complies on the first turn
instead of hitting the Stop-gate block. This is an optimization layer; the
deterministic verifier remains the guarantee.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROUTER = Path(__file__).resolve().parent.parent / "hooks" / "prompt_router.py"
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_skill_invocation_verifier import CONTEXT7_RULE, DEBUG_RULE, GUARDRAILS_RULE  # noqa: E402


class RouterHarness(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.rules_path = self.dir / "rules.json"

    def write_rules(self, rules):
        self.rules_path.write_text(json.dumps({"rules": rules}), encoding="utf-8")

    def run_hook(self, prompt, stdin_raw=None):
        hook_input = {"prompt": prompt, "cwd": str(self.dir),
                      "transcript_path": str(self.dir / "t.jsonl"), "session_id": "s1"}
        data = stdin_raw if stdin_raw is not None else json.dumps(hook_input)
        return subprocess.run([sys.executable, str(ROUTER), "--rules", str(self.rules_path)],
                              input=data, capture_output=True, text=True, timeout=30)


class PromptRouterTest(RouterHarness):
    def test_prompt_match_hints_the_skill(self):
        self.write_rules([DEBUG_RULE])
        proc = self.run_hook("이 버그 디버깅 해줘")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("diagnosing-bugs", proc.stdout)

    def test_no_match_injects_nothing(self):
        self.write_rules([DEBUG_RULE])
        proc = self.run_hook("just say hello")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_tool_only_rule_is_not_hinted(self):
        # GUARDRAILS_RULE triggers on a Write/Edit tool call, unknowable at
        # prompt-submit time — must not be hinted.
        self.write_rules([GUARDRAILS_RULE])
        proc = self.run_hook("edit main.py please")
        self.assertEqual(proc.stdout.strip(), "")

    def test_non_skill_require_is_not_hinted(self):
        # CONTEXT7_RULE requires a tool_pattern, not a skill; skill-routing only.
        self.write_rules([CONTEXT7_RULE])
        proc = self.run_hook("show me the library docs")
        self.assertEqual(proc.stdout.strip(), "")

    def test_multiple_matches_all_listed(self):
        other = {"id": "flow", "when": {"prompt_pattern": "다이어그램"},
                 "require": {"skill": "flow-design"}}
        self.write_rules([DEBUG_RULE, other])
        proc = self.run_hook("버그 잡고 다이어그램도 그려줘")
        self.assertIn("diagnosing-bugs", proc.stdout)
        self.assertIn("flow-design", proc.stdout)

    def test_malformed_stdin_fails_open(self):
        self.write_rules([DEBUG_RULE])
        proc = self.run_hook("x", stdin_raw="not json")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
