"""Portability checks for committed Claude Code and Codex hook config."""

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ANTIGRAVITY_MUTATORS = {
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
}


def command_hooks(config):
    for groups in config["hooks"].values():
        for group in groups:
            for hook in group["hooks"]:
                if hook.get("type") == "command":
                    yield hook


class TestHookConfig(unittest.TestCase):
    def load(self, relative):
        return json.loads((ROOT / relative).read_text(encoding="utf-8"))

    def test_claude_hooks_use_project_placeholder_exec_form(self):
        hooks = list(command_hooks(self.load(".claude/settings.json")))
        self.assertTrue(hooks)
        for hook in hooks:
            self.assertEqual(hook["command"], "python3")
            self.assertIsInstance(hook.get("args"), list)
            rendered = [arg.replace("${CLAUDE_PROJECT_DIR}", str(ROOT))
                        for arg in hook["args"]]
            self.assertNotIn("/Users/", " ".join(hook["args"]))
            for argument in rendered:
                if argument.startswith(str(ROOT)):
                    self.assertTrue(Path(argument).exists(), argument)

    def test_codex_hooks_resolve_from_git_root(self):
        hooks = list(command_hooks(self.load(".codex/hooks.json")))
        self.assertTrue(hooks)
        for hook in hooks:
            command = hook["command"]
            self.assertNotIn("/Users/", command)
            self.assertIn("$(git rev-parse --show-toplevel)", command)

    def test_claude_wires_design_precheck_without_post_bind(self):
        config = self.load(".claude/settings.json")
        pre = config["hooks"]["PreToolUse"]
        self.assertEqual(pre[0]["matcher"], "Write|Edit|apply_patch")
        self.assertIn("design_gate_hook.py", pre[0]["hooks"][0]["args"][-1])
        self.assertNotIn("PostToolUse", config["hooks"])

    def test_codex_wires_design_precheck_without_post_bind(self):
        config = self.load(".codex/hooks.json")
        pre = config["hooks"]["PreToolUse"]
        self.assertEqual(pre[0]["matcher"], "Write|Edit|apply_patch")
        self.assertIn("design_gate_hook.py", pre[0]["hooks"][0]["command"])
        self.assertNotIn("PostToolUse", config["hooks"])

    def test_codex_project_wires_watermark_before_manual_and_auto_compaction(self):
        config = self.load(".codex/hooks.json")
        groups = config["hooks"]["PreCompact"]

        self.assertEqual(groups[0]["matcher"], "manual|auto")
        commands = [hook["command"] for hook in groups[0]["hooks"]]
        self.assertTrue(any("context_watermark.py" in command for command in commands))

    def test_claude_project_wires_watermark_before_manual_and_auto_compaction(self):
        config = self.load(".claude/settings.json")
        groups = config["hooks"]["PreCompact"]

        self.assertEqual(groups[0]["matcher"], "manual|auto")
        commands = [
            hook["command"] + " " + " ".join(hook.get("args", []))
            for hook in groups[0]["hooks"]
        ]
        self.assertTrue(any("context_watermark.py" in command for command in commands))

    def test_shared_plugin_packages_precompact_watermark_hook(self):
        manifest = self.load(".codex-plugin/plugin.json")
        self.assertEqual(manifest["hooks"], "./hooks/hooks.json")
        config = self.load("hooks/hooks.json")
        groups = config["hooks"]["PreCompact"]
        self.assertEqual(groups[0]["matcher"], "manual|auto")
        self.assertIn("context_watermark.py", groups[0]["hooks"][0]["command"])

    def test_antigravity_plugin_packages_root_hooks(self):
        hooks_path = ROOT / "hooks.json"
        self.assertTrue(hooks_path.is_file(), "Antigravity plugins require root hooks.json")
        config = self.load("hooks.json")
        self.assertIn("agent-gate", config)

    def test_antigravity_hooks_cover_every_file_mutator(self):
        for relative in ("hooks.json", ".agents/hooks.json"):
            with self.subTest(relative=relative):
                hooks = self.load(relative)["agent-gate"]
                matcher = hooks["PreToolUse"][0]["matcher"]
                self.assertEqual(set(matcher.split("|")), ANTIGRAVITY_MUTATORS)
                self.assertNotIn("PostToolUse", hooks)

    def test_implementation_edits_are_not_gated_by_skill_invocation(self):
        rules = self.load(".claude/skill-rules.json")["rules"]
        matches = [rule for rule in rules
                   if rule.get("require") == {"skill": "flow-design"}
                   and "input_pattern" in rule.get("when", {})]
        self.assertEqual(matches, [])

    def test_no_host_wires_global_completion_stop_hook(self):
        claude_files = ("hooks/hooks.json", ".claude/settings.json")
        for relative in claude_files:
            with self.subTest(relative=relative):
                commands = [
                    hook.get("command", "") + " " + " ".join(hook.get("args", []))
                    for group in self.load(relative)["hooks"]["Stop"]
                    for hook in group.get("hooks", [])
                ]
                self.assertFalse(any("completion_gate_hook.py" in command for command in commands))

        codex = self.load(".codex/hooks.json")["hooks"]["Stop"]
        codex_commands = [
            hook["command"] for group in codex for hook in group.get("hooks", [])
        ]
        self.assertFalse(any("completion_gate_hook.py" in command for command in codex_commands))

        for relative in ("hooks.json", ".agents/hooks.json"):
            commands = [item["command"] for item in self.load(relative)["agent-gate"]["Stop"]]
            self.assertFalse(any("completion_gate_hook.py" in command for command in commands))
        self.assertFalse((ROOT / "hooks" / "completion_gate_hook.py").exists())

    def test_legacy_readiness_hook_alias_is_removed(self):
        self.assertFalse((ROOT / "hooks" / "readiness_gate_hook.py").exists())


if __name__ == "__main__":
    unittest.main()
