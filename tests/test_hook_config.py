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

    def test_shared_plugin_uses_the_minimal_hook_manifest(self):
        manifest = self.load(".codex-plugin/plugin.json")
        self.assertEqual(manifest["hooks"], "./hooks/hooks.json")

    def test_default_manifests_only_wire_design_gate(self):
        optional = (
            "skill_invocation_verifier.py",
            "context_watermark.py",
            "handoff_reinject.py",
            "antigravity_reinject.py",
        )
        for relative in (
            "hooks/hooks.json",
            ".claude/settings.json",
            ".codex/hooks.json",
        ):
            with self.subTest(relative=relative):
                config = self.load(relative)["hooks"]
                self.assertEqual(set(config), {"PreToolUse"})
                self.assertEqual(config["PreToolUse"][0]["matcher"], "Write|Edit|apply_patch")
                commands = [
                    hook.get("command", "") + " " + " ".join(hook.get("args", []))
                    for hook in config["PreToolUse"][0]["hooks"]
                ]
                self.assertEqual(len(commands), 1)
                self.assertIn("design_gate_hook.py", commands[0])
                for name in optional:
                    self.assertNotIn(name, commands[0])

        for relative in ("hooks.json", ".agents/hooks.json"):
            with self.subTest(relative=relative):
                config = self.load(relative)["agent-gate"]
                self.assertEqual(set(config), {"PreToolUse"})
                self.assertEqual(
                    set(config["PreToolUse"][0]["matcher"].split("|")),
                    ANTIGRAVITY_MUTATORS,
                )
                command = config["PreToolUse"][0]["hooks"][0]["command"]
                self.assertIn("design_gate_hook.py", command)
                for name in optional:
                    self.assertNotIn(name, command)

    def test_optional_lifecycle_implementations_remain_available(self):
        for relative in (
            "hooks/skill_invocation_verifier.py",
            "hooks/context_watermark.py",
            "hooks/handoff_reinject.py",
            "hooks/antigravity_reinject.py",
            "tests/test_skill_invocation_verifier.py",
            "tests/test_context_watermark.py",
        ):
            self.assertTrue((ROOT / relative).is_file(), relative)

        rules = self.load(".claude/skill-rules.json")["rules"]
        self.assertEqual(
            [rule["id"] for rule in rules],
            ["artifact-scoring-needs-artifact-judge"],
        )
        self.assertTrue(
            (ROOT / ".claude" / "skills" / "artifact-judge" / "SKILL.md").is_file()
        )

    def test_optional_lifecycle_wiring_is_documented(self):
        text = (
            (ROOT / "README.md").read_text(encoding="utf-8")
            + (ROOT / "PLUGIN.md").read_text(encoding="utf-8")
        ).lower()
        for expected in (
            "opt-in",
            "skill_invocation_verifier.py",
            "context_watermark.py",
            "handoff_reinject.py",
            "antigravity_reinject.py",
            "enable watermark and reinject together",
            "reload",
        ):
            self.assertIn(expected, text)

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
        for relative in (
            "hooks/hooks.json",
            ".claude/settings.json",
            ".codex/hooks.json",
            "hooks.json",
            ".agents/hooks.json",
        ):
            self.assertNotIn(
                "completion_gate_hook.py",
                (ROOT / relative).read_text(encoding="utf-8"),
                relative,
            )
        self.assertFalse((ROOT / "hooks" / "completion_gate_hook.py").exists())

    def test_legacy_readiness_hook_alias_is_removed(self):
        self.assertFalse((ROOT / "hooks" / "readiness_gate_hook.py").exists())


if __name__ == "__main__":
    unittest.main()
