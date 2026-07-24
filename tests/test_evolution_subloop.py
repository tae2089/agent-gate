"""Evolution Main Loop orchestration of one bounded nested Subloop."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts"))

from gate_helpers import init_git_project  # noqa: E402

import evolution_loop  # noqa: E402
import scenario_gate  # noqa: E402
from loop_engine import canonical_json  # noqa: E402
from subloop_contract import PackProfile  # noqa: E402

ASSURANCE_PROFILE = PackProfile(
    name="assurance-loop",
    supported_modes=frozenset({"standalone", "subloop"}),
)


def candidate(**overrides):
    value = {
        "schema_version": 2,
        "kind": "feature",
        "source": "manual",
        "source_ref": "conversation:hierarchical-loop",
        "title": "Build hierarchical Agent Loop execution",
        "problem": "Independent Pack state cannot express Main ownership.",
        "evidence": ["The user approved a Main and Subloop hierarchy."],
        "labels": ["architecture"],
        "request": "Implement the approved hierarchical design.",
        "requirements": ["AC-1", "AC-2"],
        "scope": ["src", "tests"],
        "permissions": [
            "read-repository",
            "modify-worktree",
            "run-local-verification",
            "push",
        ],
    }
    value.update(overrides)
    return value


def subloop_request(**overrides):
    value = {
        "pack": "assurance-loop",
        "objective": "Verify AC-1 and AC-2.",
        "requirements": ["AC-1", "AC-2"],
        "scope": ["src/auth", "tests"],
        "permissions": [
            "read-repository",
            "run-local-verification",
        ],
        "budget": {"iteration_limit": 2},
    }
    value.update(overrides)
    return value


class EvolutionSubloopTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        init_git_project(self.project)
        self.task = self.project / "_workspace" / "evolution-main"
        self.task.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def start(self, subloop_iterations=4):
        result = evolution_loop.start_run(
            self.task,
            candidate(),
            max_iterations=3,
            subloop_iterations=subloop_iterations,
        )
        self.assertTrue(result.allowed, result.errors)
        return result

    def current_source(self):
        fingerprint, errors = scenario_gate.source_fingerprint(self.project)
        self.assertFalse(errors)
        self.assertIsNotNone(fingerprint)
        return fingerprint

    def accepted_result(self, **overrides):
        state = evolution_loop.load_run(self.task)
        active = state.state["active_subloop"]
        invocation_path = self.task / active["path"] / "invocation.json"
        invocation = json.loads(invocation_path.read_text(encoding="utf-8"))
        value = {
            "schema_version": 1,
            "invocation_id": invocation["invocation_id"],
            "invocation_sha256": hashlib.sha256(canonical_json(invocation)).hexdigest(),
            "pack": invocation["pack"],
            "status": "completed",
            "summary": "Assurance completed.",
            "finding_refs": [],
            "changed_paths": [],
            "evidence_refs": ["assurance-report.json"],
            "budget_usage": {"iterations_used": 1},
            "completion_receipt": None,
            "decision": None,
            "source_snapshot_after_sha256": self.current_source(),
        }
        value.update(overrides)
        return value

    def test_start_persists_main_context_and_single_root_pointer(self):
        started = self.start()

        self.assertEqual(started.state["run_id"], "evolution-main")
        self.assertEqual(started.state["subloop_iterations_remaining"], 4)
        self.assertIsNone(started.state["active_subloop"])
        self.assertIsNone(started.state["last_subloop_result_sha256"])
        self.assertEqual(
            (self.project / "_workspace" / ".active-run").read_text(encoding="utf-8"),
            "_workspace/evolution-main\n",
        )
        self.assertFalse((self.project / "_workspace" / ".active-evolution").exists())

    def test_main_builds_nested_invocation_and_rejects_a_second_child(self):
        self.start()

        invoked = evolution_loop.invoke_subloop(
            self.task,
            self.project,
            subloop_request(),
            ASSURANCE_PROFILE,
        )
        duplicate = evolution_loop.invoke_subloop(
            self.task,
            self.project,
            subloop_request(pack="debug-loop"),
            PackProfile(
                name="debug-loop",
                supported_modes=frozenset({"standalone", "subloop"}),
            ),
        )

        self.assertTrue(invoked.allowed, invoked.errors)
        active = invoked.state["active_subloop"]
        self.assertEqual(active["invocation_id"], "subloop-001")
        self.assertEqual(active["pack"], "assurance-loop")
        self.assertTrue(
            (self.task / "subloops" / "subloop-001" / "invocation.json").is_file()
        )
        self.assertFalse(duplicate.allowed)
        self.assertIn("already active", " ".join(duplicate.errors))

    def test_main_rejects_scope_permission_and_budget_expansion(self):
        self.start(subloop_iterations=2)
        cases = (
            subloop_request(scope=["docs"]),
            subloop_request(permissions=["read-repository", "read-external-sources"]),
            subloop_request(budget={"iteration_limit": 3}),
        )

        for value in cases:
            with self.subTest(value=value):
                result = evolution_loop.invoke_subloop(
                    self.task,
                    self.project,
                    value,
                    ASSURANCE_PROFILE,
                )
                self.assertFalse(result.allowed)

    def test_accept_result_debits_budget_clears_child_and_is_immutable(self):
        self.start()
        self.assertTrue(
            evolution_loop.invoke_subloop(
                self.task,
                self.project,
                subloop_request(),
                ASSURANCE_PROFILE,
            ).allowed
        )
        value = self.accepted_result()

        accepted = evolution_loop.accept_subloop_result(
            self.task,
            self.project,
            value,
        )
        repeated = evolution_loop.accept_subloop_result(
            self.task,
            self.project,
            value,
        )

        self.assertTrue(accepted.allowed, accepted.errors)
        self.assertEqual(accepted.state["subloop_iterations_remaining"], 3)
        self.assertIsNone(accepted.state["active_subloop"])
        self.assertEqual(
            accepted.state["last_subloop_result_sha256"],
            hashlib.sha256(canonical_json(value)).hexdigest(),
        )
        self.assertTrue(
            (self.task / "subloops" / "subloop-001" / "result.json").is_file()
        )
        self.assertTrue(repeated.allowed, repeated.errors)
        self.assertEqual(repeated.state, accepted.state)

    def test_stale_or_out_of_scope_result_cannot_be_ingested(self):
        self.start()
        editable = subloop_request(
            permissions=[
                "read-repository",
                "modify-worktree",
                "run-local-verification",
            ]
        )
        self.assertTrue(
            evolution_loop.invoke_subloop(
                self.task,
                self.project,
                editable,
                ASSURANCE_PROFILE,
            ).allowed
        )

        stale = self.accepted_result(source_snapshot_after_sha256="f" * 64)
        outside = self.accepted_result(
            changed_paths=["docs/design.md"],
        )

        stale_result = evolution_loop.accept_subloop_result(
            self.task,
            self.project,
            stale,
        )
        outside_result = evolution_loop.accept_subloop_result(
            self.task,
            self.project,
            outside,
        )

        self.assertFalse(stale_result.allowed)
        self.assertFalse(outside_result.allowed)
        self.assertIsNotNone(evolution_loop.load_run(self.task).state["active_subloop"])

    def test_main_cannot_transition_or_finish_while_child_is_active(self):
        self.start()
        self.assertTrue(
            evolution_loop.invoke_subloop(
                self.task,
                self.project,
                subloop_request(),
                ASSURANCE_PROFILE,
            ).allowed
        )

        transitioned = evolution_loop.transition_run(self.task, "execute")
        terminated = evolution_loop.terminate_run(self.task, "blocked")

        self.assertFalse(transitioned.allowed)
        self.assertFalse(terminated.allowed)
        self.assertIn("Subloop", " ".join(transitioned.errors))
        self.assertIn("Subloop", " ".join(terminated.errors))


if __name__ == "__main__":
    unittest.main()
