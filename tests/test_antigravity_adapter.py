"""Contract tests for hooks/antigravity_adapter.py.

The adapter is a boundary shim: it translates Antigravity's command-hook JSON
(camelCase stdin, decision:"deny"/"continue" stdout) to and from the Claude
hook contract our hooks already speak, so the hooks run unmodified. The output
contract is pinned to antigravity.google/docs/hooks.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ADAPTER = Path(__file__).resolve().parent.parent / "hooks" / "antigravity_adapter.py"

# Fake underlying hooks driven as `python3 -c ...` so tests don't depend on the
# real hooks. BLOCK echoes a Claude-contract block; PASS stays silent; DUMP
# writes its received stdin to a path so we can assert the translation.
BLOCK = "import sys,json; print(json.dumps({'decision':'block','reason':'nope'}))"
PRETOOL_BLOCK = (
    "import json; print(json.dumps({'hookSpecificOutput': {"
    "'hookEventName': 'PreToolUse', 'permissionDecision': 'deny', "
    "'permissionDecisionReason': 'not ready'}}))"
)
PASS = "import sys; sys.stdin.read()"
DUMP_TMPL = "import sys; open({!r},'w').write(sys.stdin.read())"


def run(event, under_cmd, stdin_obj):
    args = [sys.executable, str(ADAPTER), "--event", event, "--", *under_cmd]
    return subprocess.run(args, input=json.dumps(stdin_obj), capture_output=True,
                          text=True, timeout=30)


class TranslateStdinTest(unittest.TestCase):
    def test_camelcase_fields_become_claude_schema(self):
        with tempfile.TemporaryDirectory() as d:
            dump = Path(d) / "seen.json"
            proc = run("pretooluse",
                       [sys.executable, "-c", DUMP_TMPL.format(str(dump))],
                       {"transcriptPath": "/t/x.jsonl", "workspacePaths": ["/proj"],
                        "conversationId": "c1",
                        "toolCall": {"name": "Write", "args": {"file_path": "a.py"}}})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            seen = json.loads(dump.read_text())
        self.assertEqual(seen["transcript_path"], "/t/x.jsonl")
        self.assertEqual(seen["cwd"], "/proj")
        self.assertEqual(seen["session_id"], "c1")
        self.assertEqual(seen["tool_name"], "Write")
        self.assertEqual(seen["tool_input"], {"file_path": "a.py"})


class PreToolUseOutputTest(unittest.TestCase):
    def test_block_becomes_deny(self):
        proc = run("pretooluse", [sys.executable, "-c", BLOCK], {"workspacePaths": ["/p"]})
        verdict = json.loads(proc.stdout)
        self.assertEqual(verdict["decision"], "deny")
        self.assertEqual(verdict["reason"], "nope")

    def test_no_block_becomes_allow(self):
        proc = run("pretooluse", [sys.executable, "-c", PASS], {"workspacePaths": ["/p"]})
        self.assertEqual(json.loads(proc.stdout)["decision"], "allow")

    def test_claude_pretool_deny_becomes_antigravity_deny(self):
        proc = run(
            "pretooluse",
            [sys.executable, "-c", PRETOOL_BLOCK],
            {"workspacePaths": ["/p"]},
        )
        verdict = json.loads(proc.stdout)
        self.assertEqual(verdict["decision"], "deny")
        self.assertEqual(verdict["reason"], "not ready")


class StopOutputTest(unittest.TestCase):
    def test_block_becomes_continue(self):
        proc = run("stop", [sys.executable, "-c", BLOCK], {"transcriptPath": "/t.jsonl"})
        verdict = json.loads(proc.stdout)
        self.assertEqual(verdict["decision"], "continue")
        self.assertEqual(verdict["reason"], "nope")

    def test_no_block_emits_no_decision(self):
        proc = run("stop", [sys.executable, "-c", PASS], {"transcriptPath": "/t.jsonl"})
        out = proc.stdout.strip()
        self.assertTrue(out in ("", "{}") or json.loads(out).get("decision") != "continue")


class PostToolUseOutputTest(unittest.TestCase):
    def test_posttooluse_always_empty_and_runs_hook(self):
        # PostToolUse cannot block (docs), but the hook still runs (e.g. bind).
        with tempfile.TemporaryDirectory() as d:
            dump = Path(d) / "seen.json"
            proc = run("posttooluse",
                       [sys.executable, "-c", DUMP_TMPL.format(str(dump))],
                       {"conversationId": "c1", "workspacePaths": ["/p"],
                        "toolCall": {"name": "Write", "args": {"file_path": "a.py"}}})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "{}")
            self.assertEqual(json.loads(dump.read_text())["session_id"], "c1")

    def test_posttooluse_ignores_underlying_block(self):
        proc = run("posttooluse", [sys.executable, "-c", BLOCK], {"workspacePaths": ["/p"]})
        self.assertEqual(proc.stdout.strip(), "{}")


class FailOpenTest(unittest.TestCase):
    def test_malformed_stdin_fails_open_pre(self):
        args = [sys.executable, str(ADAPTER), "--event", "pretooluse", "--",
                sys.executable, "-c", PASS]
        proc = subprocess.run(args, input="not json", capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0)
        self.assertNotEqual(json.loads(proc.stdout).get("decision"), "deny")

    def test_underlying_crash_fails_open_pre(self):
        proc = run("pretooluse", [sys.executable, "-c", "import sys; sys.exit(3)"],
                   {"workspacePaths": ["/p"]})
        self.assertEqual(proc.returncode, 0)
        self.assertNotEqual(json.loads(proc.stdout).get("decision"), "deny")


if __name__ == "__main__":
    unittest.main()
