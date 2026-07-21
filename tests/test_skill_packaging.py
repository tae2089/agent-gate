"""Cross-runtime packaging checks for project-local agent skills."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class TestSkillPackaging(unittest.TestCase):
    def test_codex_artifact_judge_is_symlinked_to_claude_canonical_directory(self):
        canonical = ROOT / ".claude" / "skills" / "artifact-judge"
        codex_entry = ROOT / ".agents" / "skills" / "artifact-judge"

        self.assertTrue(codex_entry.is_symlink(), codex_entry)
        self.assertEqual(codex_entry.resolve(strict=True), canonical.resolve(strict=True))
        self.assertTrue((codex_entry / "SKILL.md").is_file())


if __name__ == "__main__":
    unittest.main()
