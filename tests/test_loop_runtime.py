"""Contract tests for managed local Loop Pack runs."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from loop_engine import LoopDefinition  # noqa: E402
from loop_runtime import (  # noqa: E402
    ManagedLoopDefinition,
    load_managed_run,
    resolve_managed_run,
    start_managed_run,
    terminate_managed_run,
    transition_managed_run,
)

LOOP = LoopDefinition(
    name="sample",
    transitions={
        "inspect": frozenset({"verify"}),
        "verify": frozenset({"inspect", "complete"}),
    },
    terminal_statuses=frozenset(
        {"complete", "blocked", "needs-clarification", "budget-exhausted"}
    ),
    iteration_transitions=frozenset({("verify", "inspect")}),
    budget_terminal="budget-exhausted",
)
RUN = ManagedLoopDefinition(
    loop=LOOP,
    input_filename="sample-request.json",
    state_filename="sample-state.json",
    active_pointer_filename=".active-sample",
    input_hash_field="request_sha256",
    initial_status="inspect",
    interrupt_terminals=frozenset({"blocked", "needs-clarification"}),
)


class ManagedLoopRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.workspace = self.project / "_workspace"
        self.task = self.workspace / "sample"
        self.task.mkdir(parents=True)
        self.request = {"schema_version": 1, "request": "Review this change."}

    def tearDown(self):
        self.temp.cleanup()

    def test_start_persists_canonical_input_state_and_active_pointer(self):
        result = start_managed_run(RUN, self.task, self.request, max_iterations=2)

        self.assertTrue(result.allowed, result.errors)
        self.assertEqual(result.state["status"], "inspect")
        self.assertEqual(result.state["iteration"], 1)
        self.assertEqual(result.state["max_iterations"], 2)
        self.assertEqual(len(result.state["request_sha256"]), 64)
        self.assertEqual(
            json.loads(
                (self.task / "sample-request.json").read_text(encoding="utf-8")
            ),
            self.request,
        )
        self.assertEqual(
            (self.workspace / ".active-sample").read_text(encoding="utf-8"),
            "_workspace/sample\n",
        )

    def test_only_one_nonterminal_run_is_active(self):
        self.assertTrue(start_managed_run(RUN, self.task, self.request).allowed)
        other = self.workspace / "other"
        other.mkdir()

        blocked = start_managed_run(RUN, other, self.request)
        self.assertFalse(blocked.allowed)
        self.assertIn("another sample run is active", blocked.errors)

        self.assertTrue(terminate_managed_run(RUN, self.task, "blocked").allowed)
        restarted = start_managed_run(RUN, other, self.request)
        self.assertTrue(restarted.allowed, restarted.errors)

    def test_transition_persists_retry_and_budget_terminal(self):
        self.assertTrue(
            start_managed_run(RUN, self.task, self.request, max_iterations=2).allowed
        )
        self.assertTrue(
            transition_managed_run(RUN, self.task, "verify").allowed
        )
        retry = transition_managed_run(RUN, self.task, "inspect")

        self.assertTrue(retry.allowed, retry.errors)
        self.assertEqual(retry.state["iteration"], 2)
        self.assertTrue(
            transition_managed_run(RUN, self.task, "verify").allowed
        )
        exhausted = transition_managed_run(RUN, self.task, "inspect")

        self.assertTrue(exhausted.allowed, exhausted.errors)
        self.assertEqual(exhausted.state["status"], "budget-exhausted")
        self.assertEqual(exhausted.state["iteration"], 2)

    def test_load_rejects_unknown_fields_and_invalid_hash(self):
        self.assertTrue(start_managed_run(RUN, self.task, self.request).allowed)
        state_path = self.task / "sample-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["provider"] = "github"
        state["request_sha256"] = "invalid"
        state_path.write_text(json.dumps(state), encoding="utf-8")

        loaded = load_managed_run(RUN, self.task)

        self.assertFalse(loaded.allowed)
        self.assertIn("sample state has unknown fields: provider", loaded.errors)
        self.assertIn(
            "sample state request_sha256 must be a lowercase SHA-256",
            loaded.errors,
        )

    def test_resolve_and_terminate_fail_closed(self):
        missing_task, missing_errors = resolve_managed_run(RUN, self.project)
        self.assertIsNone(missing_task)
        self.assertIn("no active sample run", missing_errors)

        self.assertTrue(start_managed_run(RUN, self.task, self.request).allowed)
        active_task, errors = resolve_managed_run(RUN, self.project)
        self.assertFalse(errors)
        self.assertEqual(active_task, self.task.resolve())

        unsupported = terminate_managed_run(RUN, self.task, "complete")
        self.assertFalse(unsupported.allowed)
        self.assertIn("unsupported sample terminal status: complete", unsupported.errors)

        terminated = terminate_managed_run(RUN, self.task, "needs-clarification")
        self.assertTrue(terminated.allowed, terminated.errors)
        repeated = terminate_managed_run(RUN, self.task, "blocked")
        self.assertFalse(repeated.allowed)
        self.assertIn("sample run is already terminal", repeated.errors)


if __name__ == "__main__":
    unittest.main()
