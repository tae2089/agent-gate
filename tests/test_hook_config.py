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

    def test_claude_wires_readiness_precheck_and_post_bind(self):
        config = self.load(".claude/settings.json")
        pre = config["hooks"]["PreToolUse"]
        post = config["hooks"]["PostToolUse"]
        self.assertEqual(pre[0]["matcher"], "Write|Edit|apply_patch")
        self.assertEqual(post[0]["matcher"], "Write|Edit|apply_patch")
        self.assertEqual(pre[0]["hooks"][0]["args"][-2:], ["--mode", "pre"])
        self.assertEqual(post[0]["hooks"][0]["args"][-2:], ["--mode", "bind"])

    def test_codex_wires_readiness_precheck_and_post_bind(self):
        config = self.load(".codex/hooks.json")
        pre = config["hooks"]["PreToolUse"]
        post = config["hooks"]["PostToolUse"]
        self.assertEqual(pre[0]["matcher"], "Write|Edit|apply_patch")
        self.assertEqual(post[0]["matcher"], "Write|Edit|apply_patch")
        self.assertIn("readiness_gate_hook.py", pre[0]["hooks"][0]["command"])
        self.assertIn("--mode pre", pre[0]["hooks"][0]["command"])
        self.assertIn("--mode bind", post[0]["hooks"][0]["command"])

    def test_antigravity_plugin_packages_root_hooks(self):
        hooks_path = ROOT / "hooks.json"
        self.assertTrue(hooks_path.is_file(), "Antigravity plugins require root hooks.json")
        config = self.load("hooks.json")
        self.assertIn("agent-gate", config)

    def test_antigravity_hooks_cover_every_file_mutator(self):
        for relative in ("hooks.json", ".agents/hooks.json"):
            with self.subTest(relative=relative):
                hooks = self.load(relative)["agent-gate"]
                for event in ("PreToolUse", "PostToolUse"):
                    matcher = hooks[event][0]["matcher"]
                    self.assertEqual(set(matcher.split("|")), ANTIGRAVITY_MUTATORS)

    def test_implementation_edits_route_through_flow_design(self):
        rules = self.load(".claude/skill-rules.json")["rules"]
        matches = [rule for rule in rules
                   if rule.get("require") == {"skill": "flow-design"}
                   and "input_pattern" in rule.get("when", {})]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["when"].get("tool"), "Write|Edit")
        self.assertRegex('"/project/_workspace/change/implementation.md"',
                         matches[0]["when"]["input_pattern"])

    def test_all_hosts_wire_scenario_completion_stop_hook(self):
        claude_files = ("hooks/hooks.json", ".claude/settings.json")
        for relative in claude_files:
            with self.subTest(relative=relative):
                commands = [
                    hook.get("command", "") + " " + " ".join(hook.get("args", []))
                    for group in self.load(relative)["hooks"]["Stop"]
                    for hook in group.get("hooks", [])
                ]
                self.assertTrue(any("scenario_gate_hook.py" in command for command in commands))

        codex = self.load(".codex/hooks.json")["hooks"]["Stop"]
        codex_commands = [
            hook["command"] for group in codex for hook in group.get("hooks", [])
        ]
        self.assertTrue(any("scenario_gate_hook.py" in command for command in codex_commands))

        for relative in ("hooks.json", ".agents/hooks.json"):
            commands = [item["command"] for item in self.load(relative)["agent-gate"]["Stop"]]
            self.assertTrue(any("scenario_gate_hook.py" in command for command in commands))


if __name__ == "__main__":
    unittest.main()
