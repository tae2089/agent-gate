"""Contract tests for the requirements-gated research adoption Loop Pack."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts"))

from gate_helpers import IMPLEMENTATION, TASK, init_git_project  # noqa: E402

import evolution_loop  # noqa: E402
import research_adoption_loop  # noqa: E402
import scenario_gate  # noqa: E402
from subloop_contract import validate_result  # noqa: E402

REQUIREMENT_NAMES = (
    "atomicity",
    "clarity",
    "completeness",
    "consistency",
    "feasibility",
    "necessity",
    "traceability",
    "verifiability",
)


def request(**overrides):
    value = {
        "schema_version": 2,
        "source": "manual",
        "source_ref": "conversation:research-adoption",
        "request": "Evaluate adopting deterministic review receipts.",
        "question": "Should this repository adopt deterministic review receipts?",
        "requirements": [
            "The receipt binds a decision to immutable local evidence.",
            "The mechanism adds no runtime dependency.",
        ],
        "constraints": ["Keep provider code outside the core."],
        "success_criteria": [
            "A repository-native scenario verifies receipt freshness."
        ],
        "evidence": ["The user explicitly requested an adoption study."],
    }
    value.update(overrides)
    return value


def requirements_assessment(request_sha256, failed=None, **overrides):
    criteria = {name: {"status": "pass", "findings": []} for name in REQUIREMENT_NAMES}
    if failed is not None:
        criteria[failed] = {
            "status": "fail",
            "findings": [f"{failed} needs clarification."],
        }
    value = {
        "schema_version": 1,
        "request_sha256": request_sha256,
        "criteria": criteria,
    }
    value.update(overrides)
    return value


def source(**overrides):
    value = {
        "url": "https://example.org/specification",
        "title": "Authoritative specification",
        "claims": ["The mechanism binds a receipt to immutable content."],
    }
    value.update(overrides)
    return value


def evidence_grade(request_sha256, grade="moderate", **overrides):
    value = {
        "schema_version": 1,
        "request_sha256": request_sha256,
        "grade": grade,
        "sources": [source()],
        "rationale": ["One primary specification and one local observation agree."],
        "limitations": ["No long-running production data exists yet."],
    }
    value.update(overrides)
    return value


def axis(status="pass", findings=None):
    return {
        "status": status,
        "evidence": ["The bounded repository check provides direct evidence."],
        "findings": (
            []
            if findings is None and status == "pass"
            else findings or ["The axis does not meet the adoption threshold."]
        ),
    }


def candidate_summary(**overrides):
    value = {
        "kind": "feature",
        "title": "Adopt deterministic review receipts",
        "problem": "Review decisions are not bound to immutable local evidence.",
        "evidence": ["The verified prototype produced a current receipt."],
        "labels": ["research-adoption"],
    }
    value.update(overrides)
    return value


def adoption_brief_value(**overrides):
    value = {
        "schema_version": 1,
        "request_sha256": "a" * 64,
        "requirements_assessment_sha256": "b" * 64,
        "evidence_grade_sha256": "c" * 64,
        "prototype_result_sha256": "d" * 64,
        "scenario_result_sha256": "e" * 64,
        "verdict": "adopt",
        "evidence_certainty": {
            "grade": "moderate",
            "rationale": ["The evidence is direct but operationally limited."],
        },
        "repository_fit": axis(),
        "prototype_result": axis(),
        "findings": [],
        "prototype_disposition": "adopted",
        "evolution_candidate": candidate_summary(),
    }
    value.update(overrides)
    return value


class ResearchValidationTest(unittest.TestCase):
    def test_request_requires_engineered_requirements_and_exact_fields(self):
        valid = research_adoption_loop.validate_request(request())
        external = research_adoption_loop.validate_request(request(source="scheduler"))
        missing_requirements = research_adoption_loop.validate_request(
            request(requirements=[])
        )
        missing_success = research_adoption_loop.validate_request(
            request(success_criteria=[])
        )
        legacy = research_adoption_loop.validate_request(
            {key: value for key, value in request().items() if key != "requirements"}
        )

        self.assertTrue(valid.allowed, valid.errors)
        self.assertFalse(external.allowed)
        self.assertFalse(missing_requirements.allowed)
        self.assertFalse(missing_success.allowed)
        self.assertFalse(legacy.allowed)

    def test_requirements_assessment_is_eight_binary_axes_without_score(self):
        valid = research_adoption_loop.validate_requirements_assessment(
            requirements_assessment("a" * 64)
        )
        failed = research_adoption_loop.validate_requirements_assessment(
            requirements_assessment("a" * 64, failed="clarity")
        )
        scored = research_adoption_loop.validate_requirements_assessment(
            requirements_assessment("a" * 64, total_score=87)
        )
        weighted = requirements_assessment("a" * 64)
        weighted["criteria"]["clarity"]["weight"] = 0.2
        weighted_result = research_adoption_loop.validate_requirements_assessment(
            weighted
        )
        contradictory = requirements_assessment("a" * 64, failed="clarity")
        contradictory["criteria"]["clarity"]["findings"] = []

        self.assertTrue(valid.allowed, valid.errors)
        self.assertTrue(failed.allowed, failed.errors)
        self.assertFalse(scored.allowed)
        self.assertFalse(weighted_result.allowed)
        self.assertFalse(
            research_adoption_loop.validate_requirements_assessment(
                contradictory
            ).allowed
        )
        self.assertNotIn("score", json.dumps(valid.value))
        self.assertNotIn("weight", json.dumps(valid.value))

    def test_evidence_grade_uses_certainty_labels_not_requirement_quality(self):
        for grade in ("high", "moderate", "low", "very-low"):
            with self.subTest(grade=grade):
                result = research_adoption_loop.validate_evidence_grade(
                    evidence_grade("a" * 64, grade=grade)
                )
                self.assertTrue(result.allowed, result.errors)

        unsupported = research_adoption_loop.validate_evidence_grade(
            evidence_grade("a" * 64, grade="certain")
        )
        scored = research_adoption_loop.validate_evidence_grade(
            evidence_grade("a" * 64, quality_score=90)
        )
        credential = research_adoption_loop.validate_evidence_grade(
            evidence_grade(
                "a" * 64,
                sources=[source(url="https://user:password@example.org/private")],
            )
        )

        self.assertFalse(unsupported.allowed)
        self.assertFalse(scored.allowed)
        self.assertFalse(credential.allowed)

    def test_adoption_brief_keeps_three_decision_axes_separate(self):
        adopt = research_adoption_loop.validate_adoption_brief(adoption_brief_value())
        reject = research_adoption_loop.validate_adoption_brief(
            adoption_brief_value(
                verdict="reject",
                evidence_certainty={
                    "grade": "low",
                    "rationale": ["Only indirect evidence is currently available."],
                },
                findings=["Evidence is insufficient for adoption."],
                prototype_disposition="removed",
                evolution_candidate=None,
            )
        )
        contradictory = research_adoption_loop.validate_adoption_brief(
            adoption_brief_value(repository_fit=axis("fail"))
        )
        scored = research_adoption_loop.validate_adoption_brief(
            adoption_brief_value(quality_score=92)
        )
        combined = research_adoption_loop.validate_adoption_brief(
            adoption_brief_value(checks={"quality": {"passed": True}})
        )

        self.assertTrue(adopt.allowed, adopt.errors)
        self.assertTrue(reject.allowed, reject.errors)
        self.assertFalse(contradictory.allowed)
        self.assertFalse(scored.allowed)
        self.assertFalse(combined.allowed)


class ResearchAdoptionRunTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        init_git_project(self.project)
        self.task = self.project / "_workspace" / "research-adoption"
        self.task.mkdir(parents=True)
        (self.task / "task.md").write_text(TASK, encoding="utf-8")
        (self.task / "implementation.md").write_text(
            IMPLEMENTATION,
            encoding="utf-8",
        )
        self.contract_path = self.task / "scenario-contract.json"
        self.contract_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "scenarios": [
                        {
                            "id": "S-RESEARCH-PROTOTYPE",
                            "title": "Adoption prototype passes",
                            "command": [
                                sys.executable,
                                "-c",
                                "raise SystemExit(0)",
                            ],
                            "given": ["the local adoption prototype"],
                            "when": ["the repository check runs"],
                            "then": ["the process exits successfully"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def request_hash(self):
        return research_adoption_loop.load_run(self.task).state["request_sha256"]

    def start_gate(self):
        started = research_adoption_loop.start_run(self.task, request())
        self.assertTrue(started.allowed, started.errors)
        gate = research_adoption_loop.transition_run(
            self.task,
            "requirements-gate",
        )
        self.assertTrue(gate.allowed, gate.errors)
        return started

    def start_research(self):
        started = self.start_gate()
        assessed = research_adoption_loop.assess_requirements(
            self.task,
            requirements_assessment(self.request_hash()),
        )
        self.assertTrue(assessed.allowed, assessed.errors)
        self.assertEqual(assessed.state["status"], "research")
        return started

    def start_prototype(self, grade="moderate"):
        started = self.start_research()
        evidence_phase = research_adoption_loop.transition_run(
            self.task,
            "evidence-grade",
        )
        self.assertTrue(evidence_phase.allowed, evidence_phase.errors)
        graded = research_adoption_loop.submit_evidence_grade(
            self.task,
            evidence_grade(self.request_hash(), grade=grade),
        )
        self.assertTrue(graded.allowed, graded.errors)
        self.assertEqual(graded.state["status"], "prototype")
        return started

    def start_verification(self, grade="moderate"):
        started = self.start_prototype(grade)
        verification = research_adoption_loop.transition_run(
            self.task,
            "verification",
        )
        self.assertTrue(verification.allowed, verification.errors)
        return started

    def set_scenario_exit(self, code):
        contract = json.loads(self.contract_path.read_text(encoding="utf-8"))
        contract["scenarios"][0]["command"] = [
            sys.executable,
            "-c",
            f"raise SystemExit({code})",
        ]
        self.contract_path.write_text(json.dumps(contract), encoding="utf-8")

    def run_and_capture(self):
        run = scenario_gate.run_scenarios(self.task, self.project)
        self.assertTrue(run.result_written, run.errors)
        captured = research_adoption_loop.capture_prototype_result(
            self.task,
            self.project,
        )
        self.assertTrue(captured.allowed, captured.errors)

    def brief(
        self,
        verdict="adopt",
        grade="moderate",
        repository_fit=None,
        prototype_result=None,
    ):
        state = research_adoption_loop.load_run(self.task)
        assessment_content = (self.task / "requirements-assessment.json").read_bytes()
        grade_content = (self.task / "evidence-grade.json").read_bytes()
        prototype_content = (
            self.task / "iterations" / "001" / "prototype-result.json"
        ).read_bytes()
        current_content = (self.task / "scenario-result.json").read_bytes()
        return adoption_brief_value(
            request_sha256=state.state["request_sha256"],
            requirements_assessment_sha256=hashlib.sha256(
                assessment_content
            ).hexdigest(),
            evidence_grade_sha256=hashlib.sha256(grade_content).hexdigest(),
            prototype_result_sha256=hashlib.sha256(prototype_content).hexdigest(),
            scenario_result_sha256=hashlib.sha256(current_content).hexdigest(),
            verdict=verdict,
            evidence_certainty={
                "grade": grade,
                "rationale": [
                    "One primary specification and one local observation agree."
                ],
            },
            repository_fit=repository_fit or axis(),
            prototype_result=prototype_result or axis(),
            findings=(
                []
                if verdict == "adopt"
                else ["The current evidence does not justify adoption."]
            ),
            prototype_disposition=("adopted" if verdict == "adopt" else "removed"),
            evolution_candidate=(candidate_summary() if verdict == "adopt" else None),
        )

    def test_state_graph_matches_the_replacement_policy(self):
        self.assertEqual(
            research_adoption_loop.PHASE_TRANSITIONS,
            {
                "frame": frozenset({"requirements-gate"}),
                "requirements-gate": frozenset({"research"}),
                "research": frozenset({"evidence-grade"}),
                "evidence-grade": frozenset({"prototype"}),
                "prototype": frozenset({"verification"}),
                "verification": frozenset({"adopted", "rejected"}),
            },
        )

    def test_start_persists_frame_with_one_non_scored_pass(self):
        result = research_adoption_loop.start_run(self.task, request())

        self.assertTrue(
            research_adoption_loop.PACK_PROFILE.supports("standalone")
        )
        self.assertTrue(research_adoption_loop.PACK_PROFILE.supports("subloop"))
        self.assertTrue(result.allowed, result.errors)
        self.assertEqual(result.state["status"], "frame")
        self.assertEqual(result.state["max_iterations"], 1)
        self.assertTrue((self.task / "research-request.json").is_file())
        self.assertEqual(
            (self.task.parent / ".active-run").read_text(encoding="utf-8"),
            "_workspace/research-adoption\n",
        )
        self.assertFalse(
            (self.task.parent / ".active-research-adoption").exists()
        )

    def test_failed_requirements_gate_stops_before_research(self):
        self.start_gate()
        result = research_adoption_loop.assess_requirements(
            self.task,
            requirements_assessment(
                self.request_hash(),
                failed="clarity",
            ),
        )

        self.assertTrue(result.allowed, result.errors)
        self.assertEqual(result.state["status"], "needs-clarification")
        self.assertTrue((self.task / "requirements-assessment.json").is_file())
        blocked = research_adoption_loop.transition_run(self.task, "research")
        self.assertFalse(blocked.allowed)

    def test_passing_requirements_gate_enters_research(self):
        self.start_gate()

        result = research_adoption_loop.assess_requirements(
            self.task,
            requirements_assessment(self.request_hash()),
        )

        self.assertTrue(result.allowed, result.errors)
        self.assertEqual(result.state["status"], "research")

    def test_evidence_grade_is_persisted_before_prototype(self):
        self.start_research()
        self.assertTrue(
            research_adoption_loop.transition_run(
                self.task,
                "evidence-grade",
            ).allowed
        )

        result = research_adoption_loop.submit_evidence_grade(
            self.task,
            evidence_grade(self.request_hash(), grade="very-low"),
        )

        self.assertTrue(result.allowed, result.errors)
        self.assertEqual(result.state["status"], "prototype")
        stored = json.loads(
            (self.task / "evidence-grade.json").read_text(encoding="utf-8")
        )
        self.assertEqual(stored["grade"], "very-low")

    def test_adopt_requires_verification_and_writes_adoption_brief(self):
        self.start_verification()
        self.run_and_capture()

        result = research_adoption_loop.submit_adoption_brief(
            self.task,
            self.project,
            self.brief(),
        )

        self.assertTrue(result.allowed, result.errors)
        self.assertEqual(result.state["status"], "adopted")
        self.assertTrue((self.task / "adoption-brief.json").is_file())
        self.assertFalse((self.task / "iterations" / "001" / "decision.json").exists())

    def test_reject_preserves_failed_prototype_and_clean_completion(self):
        self.start_verification(grade="low")
        self.set_scenario_exit(9)
        self.run_and_capture()
        self.set_scenario_exit(0)
        run = scenario_gate.run_scenarios(self.task, self.project)
        self.assertTrue(run.result_written, run.errors)

        result = research_adoption_loop.submit_adoption_brief(
            self.task,
            self.project,
            self.brief(
                verdict="reject",
                grade="low",
                prototype_result=axis("fail"),
            ),
        )

        self.assertTrue(result.allowed, result.errors)
        self.assertEqual(result.state["status"], "rejected")
        archived = json.loads(
            (self.task / "iterations" / "001" / "prototype-result.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(archived["results"][0]["status"], "failed")

    def test_stale_brief_cannot_change_verification_state(self):
        self.start_verification()
        self.run_and_capture()
        brief = self.brief()
        (self.project / "src" / "app.txt").write_text(
            "changed\n",
            encoding="utf-8",
        )

        result = research_adoption_loop.submit_adoption_brief(
            self.task,
            self.project,
            brief,
        )

        self.assertFalse(result.allowed)
        self.assertEqual(result.state["status"], "verification")
        self.assertIn("source_fingerprint is stale", " ".join(result.errors))

    def test_only_adopted_current_brief_can_export_evolution_candidate(self):
        self.start_verification()
        self.run_and_capture()
        adopted = research_adoption_loop.submit_adoption_brief(
            self.task,
            self.project,
            self.brief(),
        )
        self.assertTrue(adopted.allowed, adopted.errors)

        handoff = research_adoption_loop.export_evolution_candidate(
            self.task,
            self.project,
        )

        self.assertTrue(handoff.allowed, handoff.errors)
        candidate = json.loads(
            (self.task / "evolution-candidate.json").read_text(encoding="utf-8")
        )
        validation = evolution_loop.validate_candidate(candidate)
        self.assertTrue(validation.allowed, validation.errors)
        self.assertEqual(candidate["request"], request()["request"])

        assessment_path = self.task / "requirements-assessment.json"
        assessment_path.write_text(
            assessment_path.read_text(encoding="utf-8") + " ",
            encoding="utf-8",
        )
        stale = research_adoption_loop.export_evolution_candidate(
            self.task,
            self.project,
        )
        self.assertFalse(stale.allowed)
        self.assertIn("requirements_assessment_sha256 is stale", stale.errors)

    def test_rejected_brief_cannot_export_evolution_candidate(self):
        self.start_verification(grade="low")
        self.run_and_capture()
        rejected = research_adoption_loop.submit_adoption_brief(
            self.task,
            self.project,
            self.brief(verdict="reject", grade="low"),
        )
        self.assertTrue(rejected.allowed, rejected.errors)

        handoff = research_adoption_loop.export_evolution_candidate(
            self.task,
            self.project,
        )

        self.assertFalse(handoff.allowed)
        self.assertIn("only an adopted brief", " ".join(handoff.errors))

    def test_cli_start_and_status_use_the_direct_workspace_task(self):
        input_path = self.task / "request-input.json"
        input_path.write_text(json.dumps(request()), encoding="utf-8")
        common = ["--project-root", str(self.project), "--json"]
        started = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "research_adoption_loop.py"),
                "start",
                "_workspace/research-adoption",
                "--request",
                str(input_path),
                *common,
            ],
            cwd=self.project,
            text=True,
            capture_output=True,
            check=False,
        )
        status = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "research_adoption_loop.py"),
                "status",
                *common,
            ],
            cwd=self.project,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(started.returncode, 0, started.stderr)
        self.assertEqual(status.returncode, 0, status.stderr)
        payload = json.loads(status.stdout)
        self.assertEqual(payload["state"]["status"], "frame")
        self.assertEqual(payload["task"], str(self.task.resolve()))

    def test_failed_requirements_gate_maps_to_needs_decision_subloop_result(self):
        invocation = {
            "schema_version": 1,
            "invocation_id": "subloop-001",
            "pack": "research-adoption-loop",
            "mode": "subloop",
            "parent": {
                "run_id": "evolution-001",
                "task_ref": "_workspace/research-adoption",
                "state_sha256": "a" * 64,
            },
            "objective": "Determine whether to adopt the candidate.",
            "requirements": ["AC-RESEARCH-1"],
            "scope": ["."],
            "source_snapshot": {
                "ref": "subloops/subloop-001/source-snapshot.json",
                "sha256": "b" * 64,
            },
            "permissions": [
                "read-repository",
                "read-external-sources",
                "run-local-verification",
            ],
            "budget": {"iteration_limit": 1},
            "completion_task_ref": "_workspace/research-adoption",
        }
        artifact = requirements_assessment(
            "d" * 64,
            failed="clarity",
        )
        result = research_adoption_loop.build_subloop_result(
            invocation,
            artifact,
            source_snapshot_after_sha256="b" * 64,
        )

        validated = validate_result(
            result,
            invocation,
            current_source_snapshot_sha256="b" * 64,
        )

        self.assertEqual(result["status"], "needs-decision")
        self.assertTrue(validated.allowed, validated.errors)


if __name__ == "__main__":
    unittest.main()
