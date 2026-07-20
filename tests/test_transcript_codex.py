"""Codex rollout normalization tests for hooks/transcript.py.

Codex CLI writes {payload, timestamp, type}-envelope JSONL; parse_transcript
must normalize those entries so the hooks work unmodified. Payload shapes here
mirror real rollouts measured from ~/.codex/sessions (codex-cli 0.144.5).
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
from context_watermark import context_tokens
from transcript import (
    last_prompt_index,
    parse_transcript,
    prompt_text,
    successful_tool_results,
    tool_calls,
)
from transcript_helpers import (
    codex_custom_tool_call,
    codex_function_call,
    codex_token_count,
    codex_tool_output,
    codex_user_message,
    tool_use,
    user_text,
)

VERIFIER = Path(__file__).resolve().parent.parent / "hooks" / "skill_invocation_verifier.py"
WATERMARK = Path(__file__).resolve().parent.parent / "hooks" / "context_watermark.py"


def parse(entries):
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
        fh.write("\n".join(json.dumps(e) for e in entries))
        path = Path(fh.name)
    try:
        return parse_transcript(path)
    finally:
        path.unlink()


class CodexNormalizationTest(unittest.TestCase):
    def test_user_message_is_a_real_prompt(self):
        entries = parse([codex_user_message("디버깅 해줘")])
        self.assertEqual(last_prompt_index(entries), 0)
        self.assertEqual(prompt_text(entries[0]), "디버깅 해줘")

    def test_custom_tool_call_maps_to_tool_call_with_raw_input(self):
        entries = parse([codex_custom_tool_call("exec", "await tools.exec_command({\"cmd\":\"ls\"})",
                                                call_id="call_1")])
        calls = [c for e in entries for c in tool_calls(e)]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "exec")
        self.assertEqual(calls[0].tool_use_id, "call_1")
        self.assertIn("exec_command", calls[0].input["raw"])

    def test_function_call_json_arguments_become_dict_input(self):
        entries = parse([codex_function_call("wait", '{"cell_id": "21", "yield_time_ms": 30000}')])
        calls = [c for e in entries for c in tool_calls(e)]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].input, {"cell_id": "21", "yield_time_ms": 30000})

    def test_function_call_bad_arguments_fall_back_to_raw(self):
        entries = parse([codex_function_call("wait", "not-json")])
        calls = [c for e in entries for c in tool_calls(e)]
        self.assertEqual(calls[0].input, {"raw": "not-json"})

    def test_skill_md_read_synthesizes_skill_call(self):
        cmd = ("await tools.exec_command({\"cmd\":\"sed -n '1,240p' "
               "/Users/u/.agents/skills/coding-quality-guardrails/SKILL.md && "
               "cat coding-quality-guardrails/SKILL.md\"})")
        entries = parse([codex_custom_tool_call("exec", cmd)])
        calls = [c for e in entries for c in tool_calls(e)]
        skill_calls = [c for c in calls if c.name == "Skill"]
        self.assertEqual(len(skill_calls), 1)  # same skill twice → dedup
        self.assertEqual(skill_calls[0].input["skill"], "coding-quality-guardrails")

    def test_handoff_path_synthesizes_write_call(self):
        patch = "*** Update File: _workspace/task-a/handoff.md\n+content"
        entries = parse([codex_custom_tool_call("apply_patch", patch, call_id="call_hp")])
        writes = [c for e in entries for c in tool_calls(e) if c.name == "Write"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0].input["file_path"], "_workspace/task-a/handoff.md")
        self.assertEqual(writes[0].tool_use_id, "call_hp")

    def test_non_handoff_basename_does_not_synthesize_write(self):
        entries = parse([codex_custom_tool_call("exec", "cat notes/my-handoff.md")])
        writes = [c for e in entries for c in tool_calls(e) if c.name == "Write"]
        self.assertEqual(writes, [])

    def test_apply_patch_add_and_update_synthesize_write_and_edit(self):
        patch = ("*** Begin Patch\n*** Add File: docs/new.md\n+x\n"
                 "*** Update File: hooks/foo.py\n+y\n*** End Patch")
        entries = parse([codex_custom_tool_call("apply_patch", patch, call_id="call_ap")])
        synth = {(c.name, c.input.get("file_path"))
                 for e in entries for c in tool_calls(e) if c.name in ("Write", "Edit")}
        self.assertEqual(synth, {("Write", "docs/new.md"), ("Edit", "hooks/foo.py")})

    def test_exec_embedded_patch_path_stops_at_escape(self):
        raw = ('const patch = "*** Begin Patch\\n*** Update File: '
               '/abs/hooks/bar.py\\n+z\\n*** End Patch";')
        entries = parse([codex_custom_tool_call("exec", raw)])
        edits = [c for e in entries for c in tool_calls(e) if c.name == "Edit"]
        self.assertEqual([c.input["file_path"] for c in edits], ["/abs/hooks/bar.py"])

    def test_skill_mdx_does_not_synthesize_skill_call(self):
        entries = parse([codex_custom_tool_call("exec", "cat docs/some-skill/SKILL.mdx")])
        skills = [c for e in entries for c in tool_calls(e) if c.name == "Skill"]
        self.assertEqual(skills, [])

    def test_tool_output_counts_as_successful_result(self):
        entries = parse([codex_custom_tool_call("exec", "x", call_id="call_9"),
                         codex_tool_output(call_id="call_9")])
        self.assertIn("call_9", successful_tool_results(entries))

    def test_token_count_feeds_context_tokens(self):
        entries = parse([codex_token_count(205011, cached=203520)])
        self.assertEqual(context_tokens(entries), 205011)

    def test_claude_entries_pass_through_unchanged(self):
        original = [user_text("hello"), tool_use("Write", {"file_path": "a.py"}, tool_use_id="t1")]
        entries = parse(original)
        self.assertEqual(entries, original)

    def test_unknown_codex_entries_are_ignored_by_helpers(self):
        entries = parse([
            {"type": "session_meta", "timestamp": "t", "payload": {"id": "s"}},
            {"type": "event_msg", "timestamp": "t", "payload": {"type": "agent_reasoning", "text": "hm"}},
            {"type": "turn_context", "timestamp": "t", "payload": {"cwd": "/x"}},
        ])
        self.assertIsNone(last_prompt_index(entries))
        self.assertEqual([c for e in entries for c in tool_calls(e)], [])


class CodexVerifierE2ETest(unittest.TestCase):
    RULE = {"rules": [{
        "id": "debugging-needs-diagnosing-bugs",
        "when": {"prompt_pattern": "(?i)디버깅"},
        "require": {"skill": "diagnosing-bugs"},
    }]}

    def run_verifier(self, entries):
        d = Path(tempfile.mkdtemp())
        transcript = d / "rollout.jsonl"
        transcript.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")
        rules = d / "rules.json"
        rules.write_text(json.dumps(self.RULE), encoding="utf-8")
        hook_input = {"transcript_path": str(transcript), "stop_hook_active": False, "cwd": str(d)}
        return subprocess.run([sys.executable, str(VERIFIER), "--rules", str(rules)],
                              input=json.dumps(hook_input), capture_output=True, text=True, timeout=30)

    def test_codex_transcript_violation_blocks(self):
        proc = self.run_verifier([codex_user_message("디버깅 해줘")])
        self.assertEqual(proc.returncode, 0)
        verdict = json.loads(proc.stdout)
        self.assertEqual(verdict["decision"], "block")
        self.assertIn("diagnosing-bugs", verdict["reason"])

    def test_codex_skill_md_read_satisfies_rule(self):
        proc = self.run_verifier([
            codex_user_message("디버깅 해줘"),
            codex_custom_tool_call("exec", "await tools.exec_command({\"cmd\":\"cat "
                                           "/Users/u/.agents/skills/diagnosing-bugs/SKILL.md\"})"),
        ])
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")


class CodexToolRuleE2ETest(unittest.TestCase):
    """tool-based rules (Write|Edit) must fire on Codex via apply_patch synthesis."""

    RULE = {"rules": [{
        "id": "code-edits-need-guardrails",
        "when": {"tool": "Write|Edit", "input_pattern": r"\.(go|py|ts)\""},
        "require": {"skill": "coding-quality-guardrails"},
    }]}

    def run_verifier(self, entries):
        d = Path(tempfile.mkdtemp())
        transcript = d / "rollout.jsonl"
        transcript.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")
        rules = d / "rules.json"
        rules.write_text(json.dumps(self.RULE), encoding="utf-8")
        hook_input = {"transcript_path": str(transcript), "stop_hook_active": False, "cwd": str(d)}
        return subprocess.run([sys.executable, str(VERIFIER), "--rules", str(rules)],
                              input=json.dumps(hook_input), capture_output=True, text=True, timeout=30)

    PATCH_CALL = codex_custom_tool_call(
        "apply_patch", "*** Begin Patch\n*** Update File: hooks/foo.py\n+x\n*** End Patch")

    def test_codex_code_edit_without_skill_blocks(self):
        proc = self.run_verifier([codex_user_message("고쳐줘"), self.PATCH_CALL])
        verdict = json.loads(proc.stdout)
        self.assertEqual(verdict["decision"], "block")
        self.assertIn("coding-quality-guardrails", verdict["reason"])

    def test_codex_code_edit_with_skill_md_read_passes(self):
        proc = self.run_verifier([
            codex_user_message("고쳐줘"), self.PATCH_CALL,
            codex_custom_tool_call(
                "exec", "cat ~/.agents/skills/coding-quality-guardrails/SKILL.md",
                call_id="call_s"),
        ])
        self.assertEqual(proc.stdout.strip(), "")


class CodexWatermarkE2ETest(unittest.TestCase):
    """Regression: on Codex the handoff is written via apply_patch/exec, not a
    Write tool — the watermark block must still be satisfiable."""

    def run_watermark(self, entries, cwd):
        transcript = cwd / "rollout.jsonl"
        transcript.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")
        hook_input = {"transcript_path": str(transcript), "cwd": str(cwd),
                      "stop_hook_active": False, "session_id": "codex-s1"}
        return subprocess.run([sys.executable, str(WATERMARK), "--window", "258400"],
                              input=json.dumps(hook_input), capture_output=True, text=True, timeout=30)

    def test_apply_patch_handoff_satisfies_watermark(self):
        from test_context_watermark import GOOD_HANDOFF
        cwd = Path(tempfile.mkdtemp())
        (cwd / "handoff.md").write_text(GOOD_HANDOFF, encoding="utf-8")
        proc = self.run_watermark([
            codex_user_message("계속"),
            codex_custom_tool_call("apply_patch", "*** Update File: handoff.md\n+x", call_id="c1"),
            codex_tool_output(call_id="c1"),
            codex_token_count(240_000),
        ], cwd)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
