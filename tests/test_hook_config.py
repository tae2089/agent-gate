"""Portability checks for committed Claude Code and Codex hook config."""

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


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


if __name__ == "__main__":
    unittest.main()
