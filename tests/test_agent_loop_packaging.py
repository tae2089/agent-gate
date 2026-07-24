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
                self.assertEqual(self.load(relative)["version"], "0.5.0")

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

    def test_subloop_skills_have_one_canonical_cross_host_source(self):
        for name in (
            "assurance-loop",
            "debug-loop",
            "research-adoption-loop",
        ):
            with self.subTest(skill=name):
                canonical = ROOT / "skills" / name
                claude_entry = ROOT / ".claude" / "skills" / name
                codex_entry = ROOT / ".agents" / "skills" / name

                self.assertTrue((canonical / "SKILL.md").is_file())
                self.assertTrue((canonical / "agents" / "openai.yaml").is_file())
                self.assertTrue(claude_entry.is_symlink())
                self.assertTrue(codex_entry.is_symlink())
                self.assertEqual(
                    claude_entry.resolve(strict=True),
                    canonical.resolve(strict=True),
                )
                self.assertEqual(
                    codex_entry.resolve(strict=True),
                    canonical.resolve(strict=True),
                )

    def test_loop_skills_resolve_the_agent_loop_runtime(self):
        for relative in (
            "skills/evolution-loop/SKILL.md",
            "skills/ci-repair-loop/SKILL.md",
            "skills/assurance-loop/SKILL.md",
            "skills/debug-loop/SKILL.md",
            "skills/research-adoption-loop/SKILL.md",
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

    def test_subloop_skills_declare_hierarchical_boundaries(self):
        for name in (
            "assurance-loop",
            "debug-loop",
            "ci-repair-loop",
            "research-adoption-loop",
        ):
            with self.subTest(skill=name):
                content = (ROOT / "skills" / name / "SKILL.md").read_text(
                    encoding="utf-8"
                )
                normalized = " ".join(content.split()).lower()

                self.assertIn("standalone", normalized)
                self.assertIn("subloop", normalized)
                self.assertIn("evolution main", normalized)
                self.assertIn("_workspace/.active-run", normalized)
                self.assertIn("result-input.json", normalized)
                for status in (
                    "completed",
                    "changes-requested",
                    "needs-decision",
                    "blocked",
                    "budget-exhausted",
                ):
                    self.assertIn(status, normalized)
                for forbidden in ("push", "merge", "deploy"):
                    self.assertIn(forbidden, normalized)

                metadata = (
                    ROOT / "skills" / name / "agents" / "openai.yaml"
                ).read_text(encoding="utf-8")
                self.assertIn(f"${name}", metadata)

    def test_research_adoption_skill_uses_requirements_gate_without_scores(self):
        skill = ROOT / "skills" / "research-adoption-loop"
        content = (skill / "SKILL.md").read_text(encoding="utf-8")
        schemas = (skill / "references" / "artifact-schemas.md").read_text(
            encoding="utf-8"
        )
        normalized = " ".join((content + schemas).split())

        for required in (
            "Requirements Gate",
            "requirements-assessment.json",
            "adoption-brief.json",
            "Evidence Grade",
            "high",
            "moderate",
            "low",
            "very-low",
            "repository_fit",
            "prototype_result",
            "evolution-candidate.json",
            "first_completion_success",
        ):
            self.assertIn(required, normalized)
        for criterion in (
            "clarity",
            "completeness",
            "consistency",
            "necessity",
            "traceability",
            "feasibility",
            "verifiability",
            "atomicity",
        ):
            self.assertIn(f'"{criterion}"', schemas)
        self.assertIn("Do not calculate", content)
        self.assertNotIn('"total_score"', schemas)
        self.assertNotIn('"weight"', schemas)

    def test_new_loop_scripts_are_pack_owned_entry_points(self):
        self.assertTrue((ROOT / "scripts" / "assurance_loop.py").is_file())
        self.assertTrue((ROOT / "scripts" / "debug_loop.py").is_file())
        self.assertTrue(
            (ROOT / "scripts" / "research_adoption_loop.py").is_file()
        )

    def test_docs_define_main_subloops_gates_and_five_implemented_packs(self):
        content = (
            (ROOT / "README.md").read_text(encoding="utf-8")
            + (ROOT / "PLUGIN.md").read_text(encoding="utf-8")
        )

        for required in (
            "Agent Loop",
            "Loop Engine",
            "Loop Pack",
            "Gate",
            "Main Loop",
            "Subloop",
            "evolution-loop",
            "ci-repair-loop",
            "assurance-loop",
            "debug-loop",
            "research-adoption-loop",
        ):
            self.assertIn(required, content)

    def test_evolution_skill_owns_subloop_dispatch_and_final_completion(self):
        content = (ROOT / "skills" / "evolution-loop" / "SKILL.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("Main Loop", content)
        self.assertIn("invoke-subloop", content)
        self.assertIn("accept-subloop", content)
        self.assertIn("_workspace/.active-run", content)
        self.assertIn("only Main", content)


if __name__ == "__main__":
    unittest.main()
