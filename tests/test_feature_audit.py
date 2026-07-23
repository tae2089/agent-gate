"""Contract checks for the dependency-free feature audit preview."""

import re
import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PREVIEW = ROOT / "docs" / "feature-audit.html"

ACTIVE_DECISIONS = {
    "skill-verifier": "simplify",
    "artifact-lint": "required",
    "artifact-judge": "insufficient-evidence",
    "readiness-gate": "required",
    "scenario-gate": "required",
    "context-watermark": "insufficient-evidence",
    "handoff-reinject": "insufficient-evidence",
    "replay-audit": "required",
    "cross-runtime": "insufficient-evidence",
}

DECISION_HISTORY = {
    "prompt-router",
    "scenario-rollout-modes",
    "child-overlay-parent-candidate",
    "runner-review",
    "junit-xml",
    "stage1-autorun",
    "scope-gate",
}


class AuditParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = {}
        self.history = set()
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        classes = set(values.get("class", "").split())
        if tag == "article" and "feature-card" in classes:
            self.active[values.get("data-feature")] = values.get("data-decision")
        if "data-history" in values:
            self.history.add(values["data-history"])
        if tag == "a" and values.get("href"):
            self.hrefs.append(values["href"])


class FeatureAuditTest(unittest.TestCase):
    def setUp(self):
        self.html = PREVIEW.read_text(encoding="utf-8")
        self.parser = AuditParser()
        self.parser.feed(self.html)

    def test_active_inventory_has_explicit_overengineering_decisions(self):
        self.assertEqual(self.parser.active, ACTIVE_DECISIONS)

    def test_removed_and_not_adopted_features_are_history_only(self):
        self.assertEqual(self.parser.history, DECISION_HISTORY)
        self.assertTrue(DECISION_HISTORY.isdisjoint(self.parser.active))

    def test_local_evidence_links_resolve(self):
        missing = []
        for href in self.parser.hrefs:
            if re.match(r"(?:https?:|mailto:|#)", href):
                continue
            target = (PREVIEW.parent / href).resolve()
            if not target.exists():
                missing.append(href)
        self.assertEqual(missing, [])

    def test_filter_script_has_one_closed_entry_point(self):
        self.assertEqual(self.html.count("(() => {"), 1)
        self.assertIn("data-decision", self.html)
        self.assertIn("applyFilters();\n    })();", self.html)

    def test_scenario_metric_is_presented_as_trace_completeness(self):
        self.assertIn("시나리오 추적 완성도", self.html)
        self.assertIn("전체 코드 품질 점수가 아닙니다", self.html)
        self.assertNotIn("Scenario Coverage를 품질 점수로", self.html)


if __name__ == "__main__":
    unittest.main()
