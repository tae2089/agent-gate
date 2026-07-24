"""Contract tests for the bounded Debug Loop Pack."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts"))

from gate_helpers import IMPLEMENTATION, TASK, init_git_project  # noqa: E402

import debug_loop  # noqa: E402
import scenario_gate  # noqa: E402
from subloop_contract import validate_result  # noqa: E402


def request(**overrides):
    value = {
        "schema_version": 1,
        "source": "manual",
        "source_ref": "conversation:debug-failure",
        "request": "Diagnose and fix the reported session failure.",
        "symptom": "A valid session is rejected after refresh.",
        "scope": ["src", "tests"],
        "permissions": [
            "read-repository",
            "modify-worktree",
            "run-local-verification",
        ],
        "evidence": ["The user supplied a reproducible failure."],
    }
    value.update(overrides)
    return value


def diagnosis(request_sha, resolution="fix-required", **overrides):
    value = {
        "schema_version": 1,
        "request_sha256": request_sha,
        "resolution": resolution,
        "root_cause": "Refresh validation reads the stale session generation.",
        "reproduction": ["S-DEBUG-REPRO fails before the correction."],
        "evidence": ["src/session.py:42 reads the previous generation."],
        "proposed_fix": "Read and validate the current generation.",
    }
    value.update(overrides)
    return value


class DebugLoopTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        init_git_project(self.project)
        self.task = self.project / "_workspace" / "debug"
        self.task.mkdir(parents=True)
        (self.task / "task.md").write_text(TASK, encoding="utf-8")
        (self.task / "implementation.md").write_text(
            IMPLEMENTATION,
            encoding="utf-8",
        )
        (self.task / "scenario-contract.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "scenarios": [
                        {
                            "id": "S-DEBUG-REPRO",
                            "title": "Reported failure stays fixed",
                            "command": [
                                sys.executable,
                                "-c",
                                "raise SystemExit(0)",
                            ],
                            "given": ["the reported failure"],
                            "when": ["the reproduction runs"],
                            "then": ["the process exits successfully"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def start_to_diagnose(self, *, editable=True):
        permissions = (
            request()["permissions"]
            if editable
            else ["read-repository", "run-local-verification"]
        )
        started = debug_loop.start_run(
            self.task,
            request(permissions=permissions),
            max_iterations=2,
        )
        self.assertTrue(started.allowed, started.errors)
        self.assertTrue(
            debug_loop.transition_run(self.task, "reproduce").allowed
        )
        self.assertTrue(
            debug_loop.transition_run(self.task, "diagnose").allowed
        )
        return started

    def test_profile_and_request_modes_are_explicit(self):
        self.assertTrue(debug_loop.PACK_PROFILE.supports("standalone"))
        self.assertTrue(debug_loop.PACK_PROFILE.supports("subloop"))
        self.assertTrue(debug_loop.validate_request(request()).allowed)
        self.assertFalse(
            debug_loop.validate_request(
                request(permissions=["read-repository", "push"])
            ).allowed
        )

    def test_diagnosis_without_edit_authority_completes_read_only(self):
        self.start_to_diagnose(editable=False)
        state = debug_loop.load_run(self.task).state

        result = debug_loop.submit_diagnosis(
            self.task,
            diagnosis(state["request_sha256"]),
        )

        self.assertTrue(result.allowed, result.errors)
        self.assertEqual(result.state["status"], "completed")

    def test_editable_diagnosis_enters_fix_and_completion_finishes_verify(self):
        self.start_to_diagnose(editable=True)
        state = debug_loop.load_run(self.task).state
        diagnosed = debug_loop.submit_diagnosis(
            self.task,
            diagnosis(state["request_sha256"]),
        )
        self.assertEqual(diagnosed.state["status"], "fix")
        self.assertTrue(debug_loop.transition_run(self.task, "verify").allowed)
        self.assertTrue(
            scenario_gate.run_scenarios(self.task, self.project).result_written
        )

        completed = debug_loop.complete_run(self.task, self.project)

        self.assertTrue(completed.allowed, completed.errors)
        self.assertEqual(completed.state["status"], "completed")

    def test_needs_decision_is_not_guessed(self):
        self.start_to_diagnose()
        state = debug_loop.load_run(self.task).state

        result = debug_loop.submit_diagnosis(
            self.task,
            diagnosis(
                state["request_sha256"],
                resolution="needs-decision",
            ),
        )

        self.assertTrue(result.allowed, result.errors)
        self.assertEqual(result.state["status"], "needs-decision")

    def test_subloop_diagnosis_maps_to_common_result(self):
        invocation = {
            "schema_version": 1,
            "invocation_id": "subloop-001",
            "pack": "debug-loop",
            "mode": "subloop",
            "parent": {
                "run_id": "evolution-001",
                "task_ref": "_workspace/evolution",
                "state_sha256": "a" * 64,
            },
            "objective": "Diagnose AC-1 failure.",
            "requirements": ["AC-1"],
            "scope": ["src"],
            "source_snapshot": {
                "ref": "subloops/subloop-001/source-snapshot.json",
                "sha256": "b" * 64,
            },
            "permissions": ["read-repository"],
            "budget": {"iteration_limit": 1},
            "completion_task_ref": "_workspace/evolution",
        }
        source_after = "c" * 64
        result = debug_loop.build_subloop_result(
            invocation,
            diagnosis("d" * 64, resolution="diagnosed"),
            source_snapshot_after_sha256=source_after,
        )

        validated = validate_result(
            result,
            invocation,
            current_source_snapshot_sha256=source_after,
        )

        self.assertEqual(result["status"], "completed")
        self.assertTrue(validated.allowed, validated.errors)

    def test_subloop_attaches_to_parent_budget_without_global_pointer(self):
        child = self.task / "subloops" / "subloop-001"
        child.mkdir(parents=True)
        invocation = {
            "schema_version": 1,
            "invocation_id": "subloop-001",
            "pack": "debug-loop",
            "mode": "subloop",
            "parent": {
                "run_id": "evolution-001",
                "task_ref": "_workspace/debug",
                "state_sha256": "a" * 64,
            },
            "objective": "Diagnose AC-1 failure.",
            "requirements": ["AC-1"],
            "scope": ["src"],
            "source_snapshot": {
                "ref": "subloops/subloop-001/source-snapshot.json",
                "sha256": "b" * 64,
            },
            "permissions": ["read-repository"],
            "budget": {"iteration_limit": 2},
            "completion_task_ref": "_workspace/debug",
        }
        (child / "invocation.json").write_text(
            json.dumps(invocation),
            encoding="utf-8",
        )

        attached = debug_loop.attach_subloop(child, self.project)

        self.assertTrue(attached.allowed, attached.errors)
        self.assertEqual(attached.state["max_iterations"], 2)
        self.assertFalse((self.project / "_workspace" / ".active-run").exists())


if __name__ == "__main__":
    unittest.main()
