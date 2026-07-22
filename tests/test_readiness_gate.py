"""Contract tests for content-bound task and implementation readiness."""

import importlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from readiness_helpers import (
    CHILD_TASK,
    IMPLEMENTATION,
    assessment_for,
    digest,
    inheritance_for,
    write_artifacts,
    write_ready_artifacts,
)

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "readiness_gate.py"

class ReadinessFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.task_dir = Path(self.temp.name) / "sample-task"
        write_artifacts(self.task_dir)

    def tearDown(self):
        self.temp.cleanup()

    def assessment(self):
        return assessment_for(self.task_dir)

    def write_assessment(self, assessment=None):
        content = assessment if assessment is not None else self.assessment()
        (self.task_dir / "assessment.json").write_text(
            json.dumps(content), encoding="utf-8"
        )
        return content

    def validate(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            gate = importlib.import_module("readiness_gate")
            return gate.validate_task_dir(self.task_dir)
        finally:
            sys.path.pop(0)

    def assert_rejected_with(self, fragment):
        result = self.validate()
        self.assertFalse(result.ready, result)
        self.assertTrue(
            any(fragment in error for error in result.errors),
            f"missing {fragment!r} in {result.errors!r}",
        )
        return result


class TestTaskAssessment(ReadinessFixture):
    def test_valid_content_bound_assessment_passes(self):
        self.write_assessment()
        result = self.validate()
        self.assertTrue(result.ready, result.errors)
        self.assertAlmostEqual(result.task_ambiguity, 0.1)
        self.assertAlmostEqual(result.implementation_readiness, 0.935)
        self.assertEqual(result.ac_coverage, 1.0)

    def test_stale_task_hash_is_rejected(self):
        self.write_assessment()
        with (self.task_dir / "task.md").open("a", encoding="utf-8") as stream:
            stream.write("\n- Clarification: source means a guarded suffix.\n")
        self.assert_rejected_with("task.sha256")

    def test_fabricated_evidence_is_rejected(self):
        assessment = self.assessment()
        assessment["task"]["dimensions"]["outcome_clarity"]["evidence"] = "not in task"
        self.write_assessment(assessment)
        self.assert_rejected_with("outcome_clarity.evidence")

    def test_missing_dimension_is_rejected(self):
        assessment = self.assessment()
        del assessment["task"]["dimensions"]["grounding_clarity"]
        self.write_assessment(assessment)
        self.assert_rejected_with("grounding_clarity")

    def test_non_finite_or_out_of_range_scores_are_rejected(self):
        for value in (-0.1, 1.1, "0.9", True):
            with self.subTest(value=value):
                assessment = self.assessment()
                assessment["task"]["dimensions"]["outcome_clarity"]["score"] = value
                self.write_assessment(assessment)
                self.assert_rejected_with("outcome_clarity.score")

    def test_dimension_floor_blocks_a_good_weighted_average(self):
        assessment = self.assessment()
        assessment["task"]["dimensions"]["outcome_clarity"]["score"] = 0.74
        self.write_assessment(assessment)
        self.assert_rejected_with("outcome_clarity floor")

    def test_ambiguity_threshold_is_recomputed(self):
        assessment = self.assessment()
        scores = {
            "outcome_clarity": 0.75,
            "constraint_clarity": 0.65,
            "acceptance_clarity": 0.70,
            "grounding_clarity": 0.60,
        }
        for key, value in scores.items():
            assessment["task"]["dimensions"][key]["score"] = value
        self.write_assessment(assessment)
        result = self.assert_rejected_with("task ambiguity")
        self.assertAlmostEqual(result.task_ambiguity, 0.31)

    def test_blocking_unknowns_are_rejected(self):
        assessment = self.assessment()
        assessment["task"]["blocking_unknowns"] = ["Which source suffixes are guarded?"]
        self.write_assessment(assessment)
        self.assert_rejected_with("blocking_unknowns")


class TestImplementationAssessment(ReadinessFixture):
    def test_missing_ac_reference_is_named_and_rejected(self):
        path = self.task_dir / "implementation.md"
        path.write_text(IMPLEMENTATION.replace("AC-1 and AC-2", "AC-1"), encoding="utf-8")
        assessment = self.assessment()
        self.write_assessment(assessment)
        result = self.assert_rejected_with("AC-2")
        self.assertEqual(result.ac_coverage, 0.5)

    def test_unresolved_decisions_are_rejected(self):
        assessment = self.assessment()
        assessment["implementation"]["unresolved_decisions"] = ["Exact suffix set"]
        self.write_assessment(assessment)
        self.assert_rejected_with("unresolved_decisions")

    def test_readiness_threshold_is_recomputed(self):
        assessment = self.assessment()
        for dimension in assessment["implementation"]["dimensions"].values():
            dimension["score"] = 0.6
        self.write_assessment(assessment)
        result = self.assert_rejected_with("implementation readiness")
        self.assertAlmostEqual(result.implementation_readiness, 0.74)

    def test_stale_implementation_hash_is_rejected(self):
        self.write_assessment()
        with (self.task_dir / "implementation.md").open("a", encoding="utf-8") as stream:
            stream.write("\n- Edge: reject an unsafe task directory.\n")
        self.assert_rejected_with("implementation.sha256")


class TestReadinessCli(ReadinessFixture):
    def test_json_cli_reports_recomputed_scores(self):
        self.write_assessment()
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--json", str(self.task_dir)],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads(proc.stdout)
        self.assertTrue(result["ready"])
        self.assertAlmostEqual(result["task_ambiguity"], 0.1)
        self.assertAlmostEqual(result["implementation_readiness"], 0.935)

    def test_invalid_assessment_exits_one_with_diagnostics(self):
        self.write_assessment({"schema_version": 1})
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), str(self.task_dir)],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("NOT READY", proc.stdout)

    def test_template_contains_current_hashes_and_zeroed_dimensions(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--template", str(self.task_dir)],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        template = json.loads(proc.stdout)
        self.assertEqual(template["task"]["sha256"], digest(self.task_dir / "task.md"))
        self.assertEqual(
            template["implementation"]["sha256"],
            digest(self.task_dir / "implementation.md"),
        )
        self.assertEqual(
            template["task"]["dimensions"]["outcome_clarity"],
            {"score": 0.0, "evidence": ""},
        )


class InheritedReadinessFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.workspace = self.project / "_workspace"
        self.parent = self.workspace / "parent-task"
        self.child = self.workspace / "child-task"
        write_ready_artifacts(self.parent)
        self.child.mkdir(parents=True)
        (self.child / "task.md").write_text(CHILD_TASK, encoding="utf-8")
        self.write_inheritance()

    def tearDown(self):
        self.temp.cleanup()

    def write_inheritance(self, **overrides):
        manifest = inheritance_for(self.child, **overrides)
        (self.child / "inherited-readiness.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        return manifest

    def validate(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            gate = importlib.import_module("readiness_gate")
            return gate.validate_task_dir(self.child)
        finally:
            sys.path.pop(0)

    def assert_rejected_with(self, fragment):
        result = self.validate()
        self.assertFalse(result.ready, result)
        self.assertTrue(
            any(fragment in error for error in result.errors),
            f"missing {fragment!r} in {result.errors!r}",
        )
        return result


class TestInheritedReadiness(InheritedReadinessFixture):
    def test_valid_child_inherits_ready_parent_metrics(self):
        result = self.validate()
        self.assertTrue(result.ready, result.errors)
        self.assertAlmostEqual(result.task_ambiguity, 0.1)
        self.assertAlmostEqual(result.implementation_readiness, 0.935)
        self.assertEqual(result.ac_coverage, 1.0)

    def test_stale_child_task_hash_is_rejected(self):
        with (self.child / "task.md").open("a", encoding="utf-8") as stream:
            stream.write("\n- changed after inheritance was declared\n")
        self.assert_rejected_with("child_task_sha256")

    def test_parent_must_be_a_safe_direct_full_task(self):
        cases = (
            ({"parent_task": "../parent-task"}, "parent_task"),
            ({"parent_task": "missing-task"}, "parent task"),
        )
        for overrides, message in cases:
            with self.subTest(overrides=overrides):
                self.write_inheritance(**overrides)
                self.assert_rejected_with(message)

        (self.parent / "inherited-readiness.json").write_text("{}", encoding="utf-8")
        self.write_inheritance()
        self.assert_rejected_with("direct Full task")

    def test_stale_parent_readiness_is_rejected(self):
        with (self.parent / "task.md").open("a", encoding="utf-8") as stream:
            stream.write("\n- parent changed after assessment\n")
        self.assert_rejected_with("parent readiness")
        self.assert_rejected_with("task.sha256")

    def test_parent_symlink_is_rejected(self):
        outside = self.project / "outside-parent"
        self.parent.rename(outside)
        self.parent.symlink_to(outside)
        self.assert_rejected_with("parent task")

    def test_flow_and_acceptance_refs_are_closed(self):
        cases = (
            ({"flow_refs": []}, "flow_refs"),
            ({"flow_refs": ["P99"]}, "P99"),
            ({"flow_refs": ["P1", "P1"]}, "duplicate"),
            ({"acceptance_refs": []}, "acceptance_refs"),
            ({"acceptance_refs": ["AC-99"]}, "AC-99"),
            ({"acceptance_refs": ["AC-2"]}, "child task.md"),
        )
        for overrides, message in cases:
            with self.subTest(overrides=overrides):
                self.write_inheritance(**overrides)
                self.assert_rejected_with(message)

    def test_unknown_or_downgrade_fields_are_rejected(self):
        self.write_inheritance(tier="fast")
        self.assert_rejected_with("unknown fields")

    def test_cli_emits_content_bound_inheritance_template(self):
        (self.child / "inherited-readiness.json").unlink()
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--inherit-from",
                str(self.parent),
                str(self.child),
            ],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        template = json.loads(proc.stdout)
        self.assertEqual(template["mode"], "inherit-full")
        self.assertEqual(template["parent_task"], "parent-task")
        self.assertEqual(template["child_task_sha256"], digest(self.child / "task.md"))
        self.assertEqual(template["flow_refs"], [])
        self.assertEqual(template["acceptance_refs"], [])


if __name__ == "__main__":
    unittest.main()
