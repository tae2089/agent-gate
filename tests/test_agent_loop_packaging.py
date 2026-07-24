"""Public identity and cross-host packaging contracts for Agent Loop."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class AgentLoopIdentityTest(unittest.TestCase):
    def load(self, relative):
        return json.loads((ROOT / relative).read_text(encoding="utf-8"))

    def test_public_manifests_use_the_agent_loop_identity(self):
        for relative in (
            ".claude-plugin/plugin.json",
            ".codex-plugin/plugin.json",
            ".gemini-extension.json",
            "plugin.json",
        ):
            with self.subTest(path=relative):
                manifest = self.load(relative)
                self.assertEqual(manifest["name"], "agent-loop")
                self.assertIn("loop", manifest["description"].lower())

        marketplace = self.load(".claude-plugin/marketplace.json")
        self.assertEqual(marketplace["name"], "agent-loop")
        self.assertEqual(marketplace["owner"]["name"], "agent-loop")
        self.assertEqual(marketplace["plugins"][0]["name"], "agent-loop")

    def test_versioned_manifests_publish_the_multi_loop_minor(self):
        for relative in (
            ".claude-plugin/plugin.json",
            ".codex-plugin/plugin.json",
            ".gemini-extension.json",
        ):
            with self.subTest(path=relative):
                self.assertEqual(self.load(relative)["version"], "0.3.0")

    def test_antigravity_hook_namespaces_and_paths_use_agent_loop(self):
        for relative in ("hooks.json", ".agents/hooks.json"):
            with self.subTest(path=relative):
                config = self.load(relative)
                self.assertEqual(set(config), {"agent-loop"})
                command = config["agent-loop"]["PreToolUse"][0]["hooks"][0][
                    "command"
                ]
                if relative == "hooks.json":
                    self.assertIn("/plugins/agent-loop", command)
                    self.assertNotIn("/plugins/agent-gate", command)

    def test_runtime_diagnostics_use_agent_loop_prefix(self):
        for relative in (
            "hooks/design_gate_hook.py",
            "hooks/skill_invocation_verifier.py",
            "hooks/context_watermark.py",
            "hooks/handoff_reinject.py",
            "hooks/antigravity_reinject.py",
            "hooks/transcript.py",
        ):
            with self.subTest(path=relative):
                content = (ROOT / relative).read_text(encoding="utf-8")
                self.assertNotIn("[agent-gate]", content)

    def test_ci_workflow_uses_agent_loop_filename_and_name(self):
        workflow = ROOT / ".github" / "workflows" / "agent-loop-ci.yml"

        self.assertTrue(workflow.is_file())
        self.assertFalse(
            (ROOT / ".github" / "workflows" / "agent-gate-ci.yml").exists()
        )
        self.assertIn("name: agent-loop-ci", workflow.read_text(encoding="utf-8"))


class LoopPackPackagingTest(unittest.TestCase):
    def test_ci_repair_skill_has_one_canonical_cross_host_source(self):
        canonical = ROOT / "skills" / "ci-repair-loop"
        claude_entry = ROOT / ".claude" / "skills" / "ci-repair-loop"
        codex_entry = ROOT / ".agents" / "skills" / "ci-repair-loop"

        self.assertTrue((canonical / "SKILL.md").is_file())
        self.assertTrue(claude_entry.is_symlink())
        self.assertTrue(codex_entry.is_symlink())
        self.assertEqual(claude_entry.resolve(strict=True), canonical.resolve(strict=True))
        self.assertEqual(codex_entry.resolve(strict=True), canonical.resolve(strict=True))

    def test_loop_skills_resolve_the_agent_loop_runtime(self):
        for relative in (
            "skills/evolution-loop/SKILL.md",
            "skills/ci-repair-loop/SKILL.md",
        ):
            with self.subTest(path=relative):
                content = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("AGENT_LOOP_ROOT", content)
                self.assertNotIn("AGENT_GATE_ROOT", content)

    def test_ci_repair_skill_closes_design_and_bounds_commands(self):
        content = (
            ROOT / "skills" / "ci-repair-loop" / "SKILL.md"
        ).read_text(encoding="utf-8")
        normalized = " ".join(content.split())

        self.assertIn("parent of the `skills/` directory", normalized)
        self.assertIn("ci_repair_loop.py\" terminate", normalized)
        self.assertIn("scenario_gate.py\" completion", normalized)
        self.assertIn("--finish", normalized)
        self.assertIn("production or shared remote state", normalized)
        self.assertIn("production credentials", normalized)
        self.assertIn('"schema_version": 1', content)
        self.assertIn('"id": "S-', content)
        self.assertIn("AC-1", content)
        self.assertIn("Approach", content)
        self.assertIn("Assumptions", content)
        self.assertIn("P1", content)
        self.assertIn("P2", content)
        self.assertIn("```text", content)
        self.assertIn("```mermaid", content)
        self.assertIn("[time] decision:", content)
        self.assertIn("retry the `complete` command", content)
        self.assertIn("reactivate the Design", content)
        self.assertLess(
            content.index('scenario_gate.py" completion'),
            content.index('ci_repair_loop.py" complete'),
        )

    def test_docs_define_the_product_domain_and_both_implemented_packs(self):
        content = (
            (ROOT / "README.md").read_text(encoding="utf-8")
            + (ROOT / "PLUGIN.md").read_text(encoding="utf-8")
        )

        for required in (
            "Agent Loop",
            "Loop Engine",
            "Loop Pack",
            "Gate",
            "evolution-loop",
            "ci-repair-loop",
        ):
            self.assertIn(required, content)


if __name__ == "__main__":
    unittest.main()
