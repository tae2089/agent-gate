"""Tests for scripts/replay_audit.py — the CI regression harness that replays
a corpus of fixture transcripts against the verifier and checks each verdict.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPLAY = Path(__file__).resolve().parent.parent / "scripts" / "replay_audit.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "replay"


def run(manifest):
    return subprocess.run([sys.executable, str(REPLAY), str(manifest)],
                          capture_output=True, text=True, timeout=30)


class ReplayAuditTest(unittest.TestCase):
    def test_matching_corpus_exits_zero(self):
        proc = run(FIXTURES / "manifest.json")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("2 passed", proc.stdout)

    def test_regressed_expectation_exits_one(self):
        # Flip one expectation so the recorded verdict no longer matches.
        manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
        manifest["cases"][1]["expect_block"] = ["debugging-needs-diagnosing-bugs"]
        with tempfile.TemporaryDirectory() as d:
            tampered = Path(d) / "manifest.json"
            # rules/transcript paths resolve relative to the manifest dir, so
            # point them back at the fixtures dir via absolute paths.
            for case in manifest["cases"]:
                case["transcript"] = str(FIXTURES / case["transcript"])
                case["rules"] = str(FIXTURES / case["rules"])
            tampered.write_text(json.dumps(manifest), encoding="utf-8")
            proc = run(tampered)
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("artifact-scoring-with-judge-passes", proc.stdout)


if __name__ == "__main__":
    unittest.main()
