"""Contract tests for the deterministic Assurance Loop Pack."""

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

import assurance_loop  # noqa: E402
import scenario_gate  # noqa: E402
from subloop_contract import validate_result  # noqa: E402

CATEGORIES = (
    "abstraction_complexity",
    "code_quality_module_responsibility",
    "failure_boundary_compatibility",
    "missing_or_overimplemented_requirements",
    "requirements_conformance",
    "test_quality_regression_prevention",
)


def request(**overrides):
    value = {
        "schema_version": 2,
        "source": "manual",
        "source_ref": "conversation:assurance-current-change",
        "request": "Assess the current change and address findings if authorized.",
        "target": "HEAD compared with main",
        "requirements": ["AC-1", "AC-2"],
        "scope": ["src", "tests"],
        "permissions": [
            "read-repository",
            "modify-worktree",
            "run-local-verification",
        ],
        "evidence": ["The user requested implementation assurance."],
    }
    value.update(overrides)
    return value


def finding(category="requirements_conformance", **overrides):
    value = {
        "id": "A-001",
        "category": category,
        "severity": "P1",
        "title": "Observable requirement gap",
        "requirement_refs": ["AC-1"],
        "evidence": ["src/app.txt does not implement AC-1."],
        "action": "Implement AC-1 and add a regression test.",
    }
    value.update(overrides)
    return value


def assessments(failed=None, **finding_overrides):
    value = {category: {"status": "pass", "findings": []} for category in CATEGORIES}
    if failed is not None:
        details = {"category": failed, **finding_overrides}
        value[failed] = {
            "status": "fail",
            "findings": [finding(**details)],
        }
    return value


class AssuranceValidationTest(unittest.TestCase):
    def test_profile_declares_standalone_and_subloop_modes(self):
        self.assertTrue(assurance_loop.PACK_PROFILE.supports("standalone"))
        self.assertTrue(assurance_loop.PACK_PROFILE.supports("subloop"))

    def test_request_requires_manual_authority_and_exact_root_context(self):
        valid = assurance_loop.validate_request(request())
        external = assurance_loop.validate_request(request(source="github"))
        unknown = assurance_loop.validate_request(request(provider="github"))
        unsupported_permission = assurance_loop.validate_request(
            request(permissions=["read-repository", "push"])
        )

        self.assertTrue(valid.allowed, valid.errors)
        self.assertFalse(external.allowed)
        self.assertIn("source must be manual", " ".join(external.errors))
        self.assertFalse(unknown.allowed)
        self.assertFalse(unsupported_permission.allowed)

    def test_report_requires_all_six_independent_assessment_categories(self):
        base = {
            "schema_version": 2,
            "request_sha256": "a" * 64,
            "scenario_result_sha256": "b" * 64,
        }
        passing = assurance_loop.validate_report({**base, "assessments": assessments()})
        failing = assurance_loop.validate_report(
            {
                **base,
                "assessments": assessments("missing_or_overimplemented_requirements"),
            }
        )
        missing = assessments()
        del missing["test_quality_regression_prevention"]
        missing_result = assurance_loop.validate_report(
            {**base, "assessments": missing}
        )
        contradictory = assessments()
        contradictory["requirements_conformance"] = {
            "status": "pass",
            "findings": [finding()],
        }
        contradictory_result = assurance_loop.validate_report(
            {**base, "assessments": contradictory}
        )

        self.assertTrue(passing.allowed, passing.errors)
        self.assertTrue(failing.allowed, failing.errors)
        self.assertFalse(missing_result.allowed)
        self.assertFalse(contradictory_result.allowed)

    def test_findings_are_category_requirement_and_source_bound(self):
        base = {
            "schema_version": 2,
            "request_sha256": "a" * 64,
            "scenario_result_sha256": "b" * 64,
        }
        wrong_category = assurance_loop.validate_report(
            {
                **base,
                "assessments": assessments(
                    "requirements_conformance",
                    category="abstraction_complexity",
                ),
            }
        )
        no_requirement = assurance_loop.validate_report(
            {
                **base,
                "assessments": assessments(
                    "requirements_conformance",
                    requirement_refs=[],
                ),
            }
        )

        self.assertFalse(wrong_category.allowed)
        self.assertFalse(no_requirement.allowed)


class AssuranceRunTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        init_git_project(self.project)
        self.task = self.project / "_workspace" / "assurance"
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
                            "id": "S-ASSURANCE-UNIT",
                            "title": "Assured repository checks pass",
                            "command": [
                                sys.executable,
                                "-c",
                                "raise SystemExit(0)",
                            ],
                            "given": ["the assured change"],
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

    def start_assurance(self, *, editable=True, max_iterations=2):
        permissions = (
            request()["permissions"]
            if editable
            else ["read-repository", "run-local-verification"]
        )
        result = assurance_loop.start_run(
            self.task,
            request(permissions=permissions),
            max_iterations=max_iterations,
        )
        self.assertTrue(result.allowed, result.errors)
        self.assertTrue(assurance_loop.transition_run(self.task, "assess").allowed)
        return result

    def report(self, failed=None, **finding_overrides):
        state = assurance_loop.load_run(self.task)
        content = (self.task / "scenario-result.json").read_bytes()
        return {
            "schema_version": 2,
            "request_sha256": state.state["request_sha256"],
            "scenario_result_sha256": hashlib.sha256(content).hexdigest(),
            "assessments": assessments(failed, **finding_overrides),
        }

    def test_start_uses_root_pointer_and_assurance_artifacts(self):
        result = assurance_loop.start_run(self.task, request())

        self.assertTrue(result.allowed, result.errors)
        self.assertEqual(result.state["status"], "inspect")
        self.assertTrue((self.task / "assurance-request.json").is_file())
        self.assertEqual(
            (self.task.parent / ".active-run").read_text(encoding="utf-8"),
            "_workspace/assurance\n",
        )
        self.assertFalse((self.task.parent / ".active-assurance").exists())

    def test_read_only_finding_returns_changes_requested_without_address(self):
        self.start_assurance(editable=False)
        self.assertTrue(
            scenario_gate.run_scenarios(self.task, self.project).result_written
        )

        submitted = assurance_loop.submit_assessment(
            self.task,
            self.project,
            self.report("requirements_conformance"),
        )

        self.assertTrue(submitted.allowed, submitted.errors)
        self.assertEqual(submitted.state["status"], "changes-requested")
        self.assertFalse((self.task / "iterations" / "001" / "review.json").exists())
        self.assertTrue((self.task / "iterations" / "001" / "assurance.json").is_file())

    def test_editable_finding_addresses_verifies_and_reassesses(self):
        self.start_assurance(editable=True, max_iterations=2)
        self.assertTrue(
            scenario_gate.run_scenarios(self.task, self.project).result_written
        )
        actionable = assurance_loop.submit_assessment(
            self.task,
            self.project,
            self.report("failure_boundary_compatibility"),
        )
        self.assertEqual(actionable.state["status"], "address")
        self.assertTrue(assurance_loop.transition_run(self.task, "verify").allowed)
        self.assertTrue(
            scenario_gate.run_scenarios(self.task, self.project).result_written
        )

        reassess = assurance_loop.verify_run(self.task, self.project)

        self.assertTrue(reassess.allowed, reassess.errors)
        self.assertEqual(reassess.state["status"], "assess")
        self.assertEqual(reassess.state["iteration"], 2)

    def test_passing_assessment_requires_current_completion(self):
        self.start_assurance()
        missing = assurance_loop.submit_assessment(
            self.task,
            self.project,
            {
                "schema_version": 2,
                "request_sha256": assurance_loop.load_run(self.task).state[
                    "request_sha256"
                ],
                "scenario_result_sha256": "a" * 64,
                "assessments": assessments(),
            },
        )
        self.assertFalse(missing.allowed)
        self.assertEqual(missing.state["status"], "assess")

        self.assertTrue(
            scenario_gate.run_scenarios(self.task, self.project).result_written
        )
        completed = assurance_loop.submit_assessment(
            self.task,
            self.project,
            self.report(),
        )

        self.assertTrue(completed.allowed, completed.errors)
        self.assertEqual(completed.state["status"], "completed")

    def test_cli_start_and_status_use_the_root_workspace_task(self):
        input_path = self.task / "request-input.json"
        input_path.write_text(json.dumps(request()), encoding="utf-8")
        common = ["--project-root", str(self.project), "--json"]
        started = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "assurance_loop.py"),
                "start",
                "_workspace/assurance",
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
                str(ROOT / "scripts" / "assurance_loop.py"),
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
        self.assertEqual(json.loads(status.stdout)["state"]["status"], "inspect")


class AssuranceSubloopTest(unittest.TestCase):
    def test_assessment_maps_to_common_parent_result_without_dispatch(self):
        invocation = {
            "schema_version": 1,
            "invocation_id": "subloop-001",
            "pack": "assurance-loop",
            "mode": "subloop",
            "parent": {
                "run_id": "evolution-001",
                "task_ref": "_workspace/evolution-example",
                "state_sha256": "a" * 64,
            },
            "objective": "Assess AC-1.",
            "requirements": ["AC-1"],
            "scope": ["src"],
            "source_snapshot": {
                "ref": "subloops/subloop-001/source-snapshot.json",
                "sha256": "b" * 64,
            },
            "permissions": ["read-repository"],
            "budget": {"iteration_limit": 1},
            "completion_task_ref": "_workspace/evolution-example",
        }
        source_after = "b" * 64

        result = assurance_loop.build_subloop_result(
            invocation,
            assessments("abstraction_complexity"),
            source_snapshot_after_sha256=source_after,
        )
        validated = validate_result(
            result,
            invocation,
            current_source_snapshot_sha256=source_after,
        )

        self.assertEqual(result["status"], "changes-requested")
        self.assertTrue(validated.allowed, validated.errors)
        self.assertNotIn("invoke-subloop", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
