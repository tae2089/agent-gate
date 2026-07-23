"""Cross-runtime packaging checks for project-local agent skills."""

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class TestSkillPackaging(unittest.TestCase):
    def test_prompt_router_is_not_packaged_or_wired(self):
        self.assertFalse((ROOT / "hooks" / "prompt_router.py").exists())
        self.assertFalse((ROOT / "tests" / "test_prompt_router.py").exists())
        for relative in ("hooks/hooks.json", ".claude/settings.json", ".codex/hooks.json"):
            config = (ROOT / relative).read_text(encoding="utf-8")
            self.assertNotIn("UserPromptSubmit", config, relative)
            self.assertNotIn("prompt_router", config, relative)

    def test_default_rules_only_enforce_requested_artifact_scoring(self):
        data = json.loads((ROOT / ".claude" / "skill-rules.json").read_text(encoding="utf-8"))
        self.assertEqual(
            [rule["id"] for rule in data["rules"]],
            ["artifact-scoring-needs-artifact-judge"],
        )

    def test_codex_artifact_judge_is_symlinked_to_claude_canonical_directory(self):
        canonical = ROOT / ".claude" / "skills" / "artifact-judge"
        codex_entry = ROOT / ".agents" / "skills" / "artifact-judge"

        self.assertTrue(codex_entry.is_symlink(), codex_entry)
        self.assertEqual(codex_entry.resolve(strict=True), canonical.resolve(strict=True))
        self.assertTrue((codex_entry / "SKILL.md").is_file())

    def test_artifact_judge_is_optional_and_not_a_design_gate(self):
        skill = (ROOT / ".claude" / "skills" / "artifact-judge" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("assessment.json", skill)
        self.assertNotIn("inherited-readiness.json", skill)
        self.assertNotIn("readiness_gate.py", skill)

    def test_scenario_design_is_shared_and_has_bounded_completion_criteria(self):
        canonical = ROOT / ".claude" / "skills" / "scenario-design"
        codex_entry = ROOT / ".agents" / "skills" / "scenario-design"
        self.assertTrue(codex_entry.is_symlink(), codex_entry)
        self.assertEqual(codex_entry.resolve(strict=True), canonical.resolve(strict=True))
        skill = (canonical / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("scenario-contract.json", skill)
        self.assertIn("plain observable expectations", skill)
        self.assertIn('"command"', skill)
        self.assertIn("--activate", skill)
        self.assertIn("Completion Criteria", skill)

    def test_scenario_completion_does_not_route_through_artifact_judge(self):
        judge = (ROOT / ".claude" / "skills" / "artifact-judge" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        scenario = (ROOT / ".claude" / "skills" / "scenario-design" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("scenario-evidence.json", judge)
        self.assertNotIn("evidence-template", judge)
        self.assertNotIn("Scenario Evidence Procedure", judge)
        self.assertNotIn("artifact-judge", scenario)
        self.assertFalse((ROOT / "docs" / "scenario-assessment.md").exists())

        for relative in ("README.md", "PLUGIN.md"):
            content = (ROOT / relative).read_text(encoding="utf-8")
            self.assertNotIn("scenario-evidence", content, relative)
            self.assertNotIn("evidence-template", content, relative)

    def test_completion_check_is_shared_with_codex(self):
        canonical = ROOT / ".claude" / "skills" / "completion-check"
        codex_entry = ROOT / ".agents" / "skills" / "completion-check"

        self.assertTrue((canonical / "SKILL.md").is_file())
        self.assertTrue((canonical / "agents" / "openai.yaml").is_file())
        self.assertTrue(codex_entry.is_symlink(), codex_entry)
        self.assertEqual(codex_entry.resolve(strict=True), canonical.resolve(strict=True))

    def test_completion_check_trigger_is_bounded(self):
        skill = (
            ROOT / ".claude" / "skills" / "completion-check" / "SKILL.md"
        ).read_text(encoding="utf-8")
        frontmatter = skill.split("---", 2)[1]

        self.assertIn("before reporting implementation work complete", frontmatter)
        self.assertIn("project files", frontmatter)
        for excluded in (
            "general conversation",
            "explanation",
            "status-only",
            "planning-only",
            "review-only",
            "no project edits",
        ):
            self.assertIn(excluded, frontmatter)

    def test_completion_check_is_not_hook_enforced(self):
        skill = (
            ROOT / ".claude" / "skills" / "completion-check" / "SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn("scenario_gate.py run --project-root . --json", skill)
        self.assertIn(
            "scenario_gate.py completion --project-root . --finish --json",
            skill,
        )

        rules = (
            ROOT / ".claude" / "skill-rules.json"
        ).read_text(encoding="utf-8")
        self.assertNotIn("completion-check", rules)
        for relative in (
            "hooks/hooks.json",
            ".claude/settings.json",
            ".codex/hooks.json",
            ".agents/hooks.json",
            "hooks.json",
        ):
            config = (ROOT / relative).read_text(encoding="utf-8")
            self.assertNotIn("completion-check", config, relative)


if __name__ == "__main__":
    unittest.main()
