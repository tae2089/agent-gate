"""Contract tests for the deterministic research adoption Loop Pack."""

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

import research_adoption_loop  # noqa: E402
import scenario_gate  # noqa: E402


def request(**overrides):
    value = {
        "schema_version": 1,
        "source": "manual",
        "source_ref": "conversation:research-adoption",
        "request": "Research and evaluate adopting deterministic review receipts.",
        "question": "Should this repository adopt deterministic review receipts?",
        "constraints": ["No new dependency", "Keep provider code outside the core"],
        "evidence": ["The user explicitly requested an adoption study."],
    }
    value.update(overrides)
    return value


def checks(**overrides):
    value = {
        "evidence_quality": {
            "passed": True,
            "evidence": ["Two authoritative sources support the mechanism."],
        },
        "repository_fit": {
            "passed": True,
            "evidence": ["The prototype fits the existing Loop Engine seam."],
        },
        "prototype_verified": {
            "passed": True,
            "evidence": ["The declared repository-native checks pass."],
        },
        "cost_acceptable": {
            "passed": True,
            "evidence": ["The change adds no runtime dependency."],
        },
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


class ResearchValidationTest(unittest.TestCase):
    def test_request_requires_manual_authority_and_exact_fields(self):
        valid = research_adoption_loop.validate_request(request())
        external = research_adoption_loop.validate_request(request(source="scheduler"))
        unknown = research_adoption_loop.validate_request(request(provider="web"))
        empty = research_adoption_loop.validate_request(request(constraints=[]))

        self.assertTrue(valid.allowed, valid.errors)
        self.assertFalse(external.allowed)
        self.assertIn("source must be manual", " ".join(external.errors))
        self.assertFalse(unknown.allowed)
        self.assertFalse(empty.allowed)

    def test_decision_policy_validates_verdict_checks_and_source_urls(self):
        base = {
            "schema_version": 1,
            "request_sha256": "a" * 64,
            "prototype_result_sha256": "c" * 64,
            "scenario_result_sha256": "b" * 64,
            "sources": [source()],
        }
        adopt = research_adoption_loop.validate_decision(
            {
                **base,
                "verdict": "adopt",
                "checks": checks(),
                "findings": [],
                "prototype_disposition": "adopted",
            }
        )
        rejected_checks = checks(
            repository_fit={
                "passed": False,
                "evidence": ["The prototype conflicts with the local model."],
            }
        )
        reject = research_adoption_loop.validate_decision(
            {
                **base,
                "verdict": "reject",
                "checks": rejected_checks,
                "findings": ["Repository fit is not acceptable."],
                "prototype_disposition": "removed",
            }
        )
        contradictory = research_adoption_loop.validate_decision(
            {
                **base,
                "verdict": "adopt",
                "checks": rejected_checks,
                "findings": [],
                "prototype_disposition": "adopted",
            }
        )
        credential_url = research_adoption_loop.validate_decision(
            {
                **base,
                "verdict": "adopt",
                "checks": checks(),
                "findings": [],
                "prototype_disposition": "adopted",
                "sources": [source(url="https://user:password@example.org/private")],
            }
        )

        self.assertTrue(adopt.allowed, adopt.errors)
        self.assertTrue(reject.allowed, reject.errors)
        self.assertFalse(contradictory.allowed)
        self.assertFalse(credential_url.allowed)


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

    def start_evaluate(self, max_iterations=2):
        started = research_adoption_loop.start_run(
            self.task,
            request(),
            max_iterations=max_iterations,
        )
        self.assertTrue(started.allowed, started.errors)
        for phase in ("research", "prototype", "evaluate"):
            transitioned = research_adoption_loop.transition_run(
                self.task,
                phase,
            )
            self.assertTrue(transitioned.allowed, transitioned.errors)
        return started

    def decision(
        self,
        verdict,
        decision_checks,
        findings,
        prototype_disposition,
    ):
        state = research_adoption_loop.load_run(self.task)
        content = (self.task / "scenario-result.json").read_bytes()
        prototype_content = (
            self.task
            / "iterations"
            / f"{state.state['iteration']:03d}"
            / "prototype-result.json"
        ).read_bytes()
        return {
            "schema_version": 1,
            "request_sha256": state.state["request_sha256"],
            "prototype_result_sha256": hashlib.sha256(prototype_content).hexdigest(),
            "scenario_result_sha256": hashlib.sha256(content).hexdigest(),
            "verdict": verdict,
            "sources": [source()],
            "checks": decision_checks,
            "findings": findings,
            "prototype_disposition": prototype_disposition,
        }

    def set_scenario_exit(self, code):
        contract = json.loads(self.contract_path.read_text(encoding="utf-8"))
        contract["scenarios"][0]["command"] = [
            sys.executable,
            "-c",
            f"raise SystemExit({code})",
        ]
        self.contract_path.write_text(json.dumps(contract), encoding="utf-8")

    def test_start_persists_frame_state_and_active_pointer(self):
        result = research_adoption_loop.start_run(self.task, request())

        self.assertTrue(result.allowed, result.errors)
        self.assertEqual(result.state["status"], "frame")
        self.assertEqual(len(result.state["request_sha256"]), 64)
        self.assertTrue((self.task / "research-request.json").is_file())
        self.assertEqual(
            (self.task.parent / ".active-research-adoption").read_text(
                encoding="utf-8"
            ),
            "_workspace/research-adoption\n",
        )

    def test_adopt_requires_current_completion_and_archives_decision(self):
        self.start_evaluate()
        self.assertTrue(
            scenario_gate.run_scenarios(self.task, self.project).result_written
        )
        captured = research_adoption_loop.capture_prototype_result(
            self.task,
            self.project,
        )
        self.assertTrue(captured.allowed, captured.errors)

        adopted = research_adoption_loop.submit_decision(
            self.task,
            self.project,
            self.decision("adopt", checks(), [], "adopted"),
        )

        self.assertTrue(adopted.allowed, adopted.errors)
        self.assertEqual(adopted.state["status"], "adopted")
        self.assertTrue((self.task / "iterations" / "001" / "decision.json").is_file())

    def test_reject_requires_failed_check_finding_and_removed_prototype(self):
        self.start_evaluate()
        self.set_scenario_exit(9)
        self.assertTrue(
            scenario_gate.run_scenarios(self.task, self.project).result_written
        )
        captured = research_adoption_loop.capture_prototype_result(
            self.task,
            self.project,
        )
        self.assertTrue(captured.allowed, captured.errors)
        self.set_scenario_exit(0)
        self.assertTrue(
            scenario_gate.run_scenarios(self.task, self.project).result_written
        )
        failed = checks(
            repository_fit={
                "passed": False,
                "evidence": ["The prototype conflicts with the local model."],
            }
        )

        rejected = research_adoption_loop.submit_decision(
            self.task,
            self.project,
            self.decision(
                "reject",
                failed,
                ["Repository fit is not acceptable."],
                "removed",
            ),
        )

        self.assertTrue(rejected.allowed, rejected.errors)
        self.assertEqual(rejected.state["status"], "rejected")
        archived = json.loads(
            (self.task / "iterations" / "001" / "prototype-result.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(archived["results"][0]["status"], "failed")

    def test_iterate_accepts_current_failed_scenario_and_exhausts_budget(self):
        self.start_evaluate(max_iterations=1)
        self.set_scenario_exit(9)
        self.assertTrue(
            scenario_gate.run_scenarios(self.task, self.project).result_written
        )
        captured = research_adoption_loop.capture_prototype_result(
            self.task,
            self.project,
        )
        self.assertTrue(captured.allowed, captured.errors)
        inconclusive = checks(
            prototype_verified={
                "passed": False,
                "evidence": ["The prototype check failed."],
            }
        )

        result = research_adoption_loop.submit_decision(
            self.task,
            self.project,
            self.decision(
                "iterate",
                inconclusive,
                ["Collect a smaller reproducible prototype."],
                "removed",
            ),
        )

        self.assertTrue(result.allowed, result.errors)
        self.assertEqual(result.state["status"], "budget-exhausted")

    def test_stale_decision_cannot_change_state(self):
        self.start_evaluate()
        self.assertTrue(
            scenario_gate.run_scenarios(self.task, self.project).result_written
        )
        captured = research_adoption_loop.capture_prototype_result(
            self.task,
            self.project,
        )
        self.assertTrue(captured.allowed, captured.errors)
        decision = self.decision("adopt", checks(), [], "adopted")
        (self.project / "src" / "app.txt").write_text(
            "changed\n",
            encoding="utf-8",
        )

        result = research_adoption_loop.submit_decision(
            self.task,
            self.project,
            decision,
        )

        self.assertFalse(result.allowed)
        self.assertEqual(result.state["status"], "evaluate")
        self.assertIn("source_fingerprint is stale", " ".join(result.errors))

    def test_decision_requires_a_captured_prototype_result(self):
        self.start_evaluate()
        self.assertTrue(
            scenario_gate.run_scenarios(self.task, self.project).result_written
        )
        state = research_adoption_loop.load_run(self.task)
        current = (self.task / "scenario-result.json").read_bytes()
        decision = {
            "schema_version": 1,
            "request_sha256": state.state["request_sha256"],
            "prototype_result_sha256": "a" * 64,
            "scenario_result_sha256": hashlib.sha256(current).hexdigest(),
            "verdict": "adopt",
            "sources": [source()],
            "checks": checks(),
            "findings": [],
            "prototype_disposition": "adopted",
        }

        result = research_adoption_loop.submit_decision(
            self.task,
            self.project,
            decision,
        )

        self.assertFalse(result.allowed)
        self.assertIn("cannot read captured prototype result", " ".join(result.errors))

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


if __name__ == "__main__":
    unittest.main()
