"""Antigravity transcript normalization tests for hooks/transcript.py.

Antigravity writes transcript_full.jsonl lines shaped as
{step_index, source, type, ...}; fixtures mirror shapes measured from a real
~/.gemini/antigravity-cli/.../transcript_full.jsonl session. Antigravity has
no token/usage field in the transcript, so the watermark is unsupported there
(documented); only the verifier path is exercised here.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
from transcript import last_prompt_index, parse_transcript, prompt_text, tool_calls
from transcript_helpers import tool_use, user_text
from test_skill_invocation_verifier import DEBUG_RULE, GUARDRAILS_RULE, VerifierHarness

SKILL_PATH = "/proj/.agents/skills/diagnosing-bugs/SKILL.md"


def ag_user(text):
    return {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE",
            "content": f"<USER_REQUEST>\n{text}\n</USER_REQUEST>\n<ADDITIONAL_METADATA>x</ADDITIONAL_METADATA>"}


def ag_planner(tool_calls_list):
    return {"step_index": 2, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
            "tool_calls": tool_calls_list}


def ag_view_skill(path=SKILL_PATH):
    return {"name": "view_file", "args": {"AbsolutePath": path, "IsSkillFile": True,
                                          "toolAction": "Reading skill file"}}


def parse(entries):
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
        fh.write("\n".join(json.dumps(e) for e in entries))
        path = Path(fh.name)
    try:
        return parse_transcript(path)
    finally:
        path.unlink()


def all_tool_calls(entries, name=None):
    calls = [c for e in entries for c in tool_calls(e)]
    return calls if name is None else [c for c in calls if c.name == name]


class AntigravityNormalizationTest(unittest.TestCase):
    def test_user_input_becomes_prompt_without_wrapper(self):
        entries = parse([ag_user("디버깅 해줘")])
        self.assertEqual(last_prompt_index(entries), 0)
        self.assertEqual(prompt_text(entries[0]), "디버깅 해줘")

    def test_write_to_file_becomes_write_with_file_path(self):
        entries = parse([ag_planner([{"name": "write_to_file",
                                      "args": {"TargetFile": "/proj/NOTES.md", "CodeContent": "x"}}])])
        writes = all_tool_calls(entries, "Write")
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0].input["file_path"], "/proj/NOTES.md")

    def test_run_command_becomes_bash(self):
        entries = parse([ag_planner([{"name": "run_command",
                                      "args": {"CommandLine": "ls", "Cwd": "/proj"}}])])
        self.assertEqual(all_tool_calls(entries, "Bash")[0].input["command"], "ls")

    def test_skill_file_read_synthesizes_skill_call(self):
        entries = parse([ag_planner([ag_view_skill()])])
        self.assertEqual(all_tool_calls(entries, "Read")[0].input["file_path"], SKILL_PATH)
        skills = all_tool_calls(entries, "Skill")
        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0].input["skill"], "diagnosing-bugs")

    def test_plain_view_file_is_not_a_skill(self):
        entries = parse([ag_planner([{"name": "view_file",
                                      "args": {"AbsolutePath": "/proj/README.md", "IsSkillFile": False}}])])
        self.assertEqual(all_tool_calls(entries, "Skill"), [])

    def test_claude_entries_pass_through_unchanged(self):
        original = [user_text("hi"), tool_use("Write", {"file_path": "a.py"}, tool_use_id="t1")]
        self.assertEqual(parse(original), original)

    def test_system_entries_are_ignored(self):
        entries = parse([{"step_index": 1, "source": "SYSTEM", "type": "CHECKPOINT", "content": "x"},
                         {"step_index": 3, "source": "SYSTEM", "type": "CONVERSATION_HISTORY"}])
        self.assertIsNone(last_prompt_index(entries))
        self.assertEqual(all_tool_calls(entries), [])


class AntigravityVerifierE2ETest(VerifierHarness):
    def run_ag(self, rules, entries):
        self.write_rules(rules)
        self.write_transcript(entries)
        return self.run_hook(self.hook_input())

    def test_missing_skill_blocks(self):
        proc = self.run_ag([DEBUG_RULE], [ag_user("디버깅 해줘")])
        self.assert_blocked(proc, "diagnosing-bugs")

    def test_skill_file_read_satisfies(self):
        proc = self.run_ag([DEBUG_RULE], [ag_user("디버깅 해줘"), ag_planner([ag_view_skill()])])
        self.assert_passed(proc)

    def test_write_to_file_triggers_code_edit_rule(self):
        proc = self.run_ag([GUARDRAILS_RULE], [
            ag_user("고쳐줘"),
            ag_planner([{"name": "write_to_file", "args": {"TargetFile": "/proj/app.py", "CodeContent": "x"}}]),
        ])
        self.assert_blocked(proc, "coding-quality-guardrails")


if __name__ == "__main__":
    unittest.main()
