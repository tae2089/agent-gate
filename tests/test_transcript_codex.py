"""Codex rollout normalization tests for hooks/transcript.py.

Codex CLI writes {payload, timestamp, type}-envelope JSONL; parse_transcript
must normalize those entries so the hooks work unmodified. Payload shapes here
mirror real rollouts measured from ~/.codex/sessions (codex-cli 0.144.5).
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
from transcript import (
    context_tokens,
    context_window,
    last_prompt_index,
    parse_transcript,
    prompt_text,
    successful_tool_results,
    tool_calls,
)
from transcript_helpers import (
    assistant_usage,
    codex_custom_tool_call,
    codex_function_call,
    codex_token_count,
    codex_tool_output,
    codex_user_message,
    tool_use,
    user_text,
)
from test_skill_invocation_verifier import DEBUG_RULE, GUARDRAILS_RULE, VerifierHarness
from test_context_watermark import GOOD_HANDOFF, WatermarkHarness


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


PATCH_CALL = codex_custom_tool_call(
    "apply_patch", "*** Begin Patch\n*** Update File: hooks/foo.py\n+x\n*** End Patch")
SKILL_READ_CALL = codex_custom_tool_call(
    "exec", "cat ~/.agents/skills/coding-quality-guardrails/SKILL.md", call_id="call_s")


class CodexNormalizationTest(unittest.TestCase):
    def test_user_message_is_a_real_prompt(self):
        entries = parse([codex_user_message("디버깅 해줘")])
        self.assertEqual(last_prompt_index(entries), 0)
        self.assertEqual(prompt_text(entries[0]), "디버깅 해줘")

    def test_custom_tool_call_maps_to_tool_call_with_raw_input(self):
        entries = parse([codex_custom_tool_call("exec", "await tools.exec_command({\"cmd\":\"ls\"})",
                                                call_id="call_1")])
        calls = all_tool_calls(entries)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "exec")
        self.assertEqual(calls[0].tool_use_id, "call_1")
        self.assertIn("exec_command", calls[0].input["raw"])

    def test_function_call_json_arguments_become_dict_input(self):
        entries = parse([codex_function_call("wait", '{"cell_id": "21", "yield_time_ms": 30000}')])
        calls = all_tool_calls(entries)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].input, {"cell_id": "21", "yield_time_ms": 30000})

    def test_function_call_bad_arguments_fall_back_to_raw(self):
        entries = parse([codex_function_call("wait", "not-json")])
        self.assertEqual(all_tool_calls(entries)[0].input, {"raw": "not-json"})

    def test_skill_md_read_synthesizes_skill_call(self):
        cmd = ("await tools.exec_command({\"cmd\":\"sed -n '1,240p' "
               "/Users/u/.agents/skills/coding-quality-guardrails/SKILL.md && "
               "cat coding-quality-guardrails/SKILL.md\"})")
        entries = parse([codex_custom_tool_call("exec", cmd)])
        skill_calls = all_tool_calls(entries, "Skill")
        self.assertEqual(len(skill_calls), 1)  # same skill twice → dedup
        self.assertEqual(skill_calls[0].input["skill"], "coding-quality-guardrails")

    def test_skill_mdx_does_not_synthesize_skill_call(self):
        entries = parse([codex_custom_tool_call("exec", "cat docs/some-skill/SKILL.mdx")])
        self.assertEqual(all_tool_calls(entries, "Skill"), [])

    def test_apply_patch_add_and_update_synthesize_write_and_edit(self):
        patch = ("*** Begin Patch\n*** Add File: docs/new.md\n+x\n"
                 "*** Update File: hooks/foo.py\n+y\n*** End Patch")
        entries = parse([codex_custom_tool_call("apply_patch", patch, call_id="call_ap")])
        synth = {(c.name, c.input.get("file_path"))
                 for c in all_tool_calls(entries) if c.name in ("Write", "Edit")}
        self.assertEqual(synth, {("Write", "docs/new.md"), ("Edit", "hooks/foo.py")})

    def test_exec_embedded_patch_path_stops_at_escape(self):
        raw = ('const patch = "*** Begin Patch\\n*** Update File: '
               '/abs/hooks/bar.py\\n+z\\n*** End Patch";')
        entries = parse([codex_custom_tool_call("exec", raw)])
        edits = all_tool_calls(entries, "Edit")
        self.assertEqual([c.input["file_path"] for c in edits], ["/abs/hooks/bar.py"])

    def test_handoff_path_synthesizes_write_call(self):
        patch = "*** Update File: _workspace/task-a/handoff.md\n+content"
        entries = parse([codex_custom_tool_call("apply_patch", patch, call_id="call_hp")])
        writes = all_tool_calls(entries, "Write")
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0].input["file_path"], "_workspace/task-a/handoff.md")
        self.assertEqual(writes[0].tool_use_id, "call_hp")

    def test_non_handoff_basename_does_not_synthesize_write(self):
        entries = parse([codex_custom_tool_call("exec", "cat notes/my-handoff.md")])
        self.assertEqual(all_tool_calls(entries, "Write"), [])

    def test_tool_output_counts_as_successful_result(self):
        entries = parse([codex_custom_tool_call("exec", "x", call_id="call_9"),
                         codex_tool_output(call_id="call_9")])
        self.assertIn("call_9", successful_tool_results(entries))

    def test_token_count_feeds_context_tokens(self):
        entries = parse([codex_token_count(205011, cached=203520)])
        self.assertEqual(context_tokens(entries), 205011)

    def test_token_count_carries_model_context_window(self):
        entries = parse([codex_token_count(1000)])
        self.assertEqual(context_window(entries), 258400)

    def test_claude_usage_has_no_context_window(self):
        self.assertIsNone(context_window(parse([assistant_usage(1000)])))

    def test_claude_entries_pass_through_unchanged(self):
        original = [user_text("hello"), tool_use("Write", {"file_path": "a.py"}, tool_use_id="t1")]
        self.assertEqual(parse(original), original)

    def test_unknown_codex_entries_are_ignored_by_helpers(self):
        entries = parse([
            {"type": "session_meta", "timestamp": "t", "payload": {"id": "s"}},
            {"type": "event_msg", "timestamp": "t", "payload": {"type": "agent_reasoning", "text": "hm"}},
            {"type": "turn_context", "timestamp": "t", "payload": {"cwd": "/x"}},
        ])
        self.assertIsNone(last_prompt_index(entries))
        self.assertEqual(all_tool_calls(entries), [])


class CodexVerifierE2ETest(VerifierHarness):
    """Rules must trigger and satisfy through normalized Codex transcripts."""

    def run_codex(self, rules, entries):
        self.write_rules(rules)
        self.write_transcript(entries)
        return self.run_hook(self.hook_input())

    def test_codex_transcript_violation_blocks(self):
        proc = self.run_codex([DEBUG_RULE], [codex_user_message("디버깅 해줘")])
        self.assert_blocked(proc, "diagnosing-bugs")

    def test_codex_skill_md_read_satisfies_rule(self):
        proc = self.run_codex([DEBUG_RULE], [
            codex_user_message("디버깅 해줘"),
            codex_custom_tool_call("exec", "cat /Users/u/.agents/skills/diagnosing-bugs/SKILL.md"),
        ])
        self.assert_passed(proc)

    def test_codex_code_edit_without_skill_blocks(self):
        proc = self.run_codex([GUARDRAILS_RULE], [codex_user_message("고쳐줘"), PATCH_CALL])
        self.assert_blocked(proc, "coding-quality-guardrails")

    def test_codex_code_edit_with_skill_md_read_passes(self):
        proc = self.run_codex([GUARDRAILS_RULE],
                              [codex_user_message("고쳐줘"), PATCH_CALL, SKILL_READ_CALL])
        self.assert_passed(proc)


class CodexWatermarkE2ETest(WatermarkHarness):
    """Regression: on Codex the handoff is written via apply_patch/exec, not a
    Write tool — the watermark block must still be satisfiable. The harness
    passes --window 200000; Codex transcripts report 258400 and must win."""

    def test_apply_patch_handoff_satisfies_watermark(self):
        (self.dir / "handoff.md").write_text(GOOD_HANDOFF, encoding="utf-8")
        self.write_transcript([
            codex_user_message("계속"),
            codex_custom_tool_call("apply_patch", "*** Update File: handoff.md\n+x", call_id="c1"),
            codex_tool_output(call_id="c1"),
            codex_token_count(240_000),
        ])
        proc = self.run_hook(self.hook_input())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_transcript_window_overrides_flag(self):
        # 190k is 95% of the harness's --window 200000, but only 73.5% of the
        # transcript-reported 258400 window — must not block.
        self.write_transcript([codex_user_message("계속"), codex_token_count(190_000)])
        proc = self.run_hook(self.hook_input())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_block_reason_uses_transcript_window(self):
        self.write_transcript([codex_user_message("계속"), codex_token_count(240_000)])
        proc = self.run_hook(self.hook_input())
        verdict = json.loads(proc.stdout)
        self.assertEqual(verdict["decision"], "block")
        self.assertIn("258400", verdict["reason"])


if __name__ == "__main__":
    unittest.main()
