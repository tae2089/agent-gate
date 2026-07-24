"""Contract tests for managed local Loop Pack runs."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from loop_engine import LoopDefinition  # noqa: E402
from loop_runtime import (  # noqa: E402
    ManagedLoopDefinition,
    attach_managed_subloop,
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
SUBLOOP_RUN = ManagedLoopDefinition(
    loop=LOOP,
    input_filename="invocation.json",
    state_filename="sample-state.json",
    active_pointer_filename=None,
    input_hash_field="request_sha256",
    initial_status="inspect",
    interrupt_terminals=frozenset({"blocked", "needs-clarification"}),
)
OTHER_ROOT_RUN = ManagedLoopDefinition(
    loop=LOOP,
    input_filename="other-request.json",
    state_filename="other-state.json",
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
            json.loads((self.task / "sample-request.json").read_text(encoding="utf-8")),
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

    def test_shared_root_pointer_serializes_different_pack_definitions(self):
        self.assertTrue(start_managed_run(RUN, self.task, self.request).allowed)
        other = self.workspace / "other-pack"
        other.mkdir()

        blocked = start_managed_run(OTHER_ROOT_RUN, other, self.request)
        self.assertFalse(blocked.allowed)

        self.assertTrue(terminate_managed_run(RUN, self.task, "blocked").allowed)
        started = start_managed_run(OTHER_ROOT_RUN, other, self.request)
        self.assertTrue(started.allowed, started.errors)

    def test_transition_persists_retry_and_budget_terminal(self):
        self.assertTrue(
            start_managed_run(RUN, self.task, self.request, max_iterations=2).allowed
        )
        self.assertTrue(transition_managed_run(RUN, self.task, "verify").allowed)
        retry = transition_managed_run(RUN, self.task, "inspect")

        self.assertTrue(retry.allowed, retry.errors)
        self.assertEqual(retry.state["iteration"], 2)
        self.assertTrue(transition_managed_run(RUN, self.task, "verify").allowed)
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

    def test_load_rejects_tampered_canonical_input(self):
        self.assertTrue(start_managed_run(RUN, self.task, self.request).allowed)
        request_path = self.task / "sample-request.json"
        request_path.write_text(
            json.dumps({"schema_version": 1, "request": "Changed."}),
            encoding="utf-8",
        )

        loaded = load_managed_run(RUN, self.task)

        self.assertFalse(loaded.allowed)
        self.assertIn("sample input hash is stale", loaded.errors)

    def test_start_rolls_back_files_when_pointer_write_fails(self):
        pointer = self.workspace / ".active-sample"

        from loop_runtime import atomic_write as real_atomic_write

        def fail_pointer(path, content):
            if Path(path).name == ".active-sample":
                raise OSError("pointer unavailable")
            real_atomic_write(path, content)

        with patch("loop_runtime.atomic_write", side_effect=fail_pointer):
            failed = start_managed_run(RUN, self.task, self.request)

        self.assertFalse(failed.allowed)
        self.assertFalse((self.task / "sample-request.json").exists())
        self.assertFalse((self.task / "sample-state.json").exists())
        self.assertFalse(pointer.exists())
        retried = start_managed_run(RUN, self.task, self.request)
        self.assertTrue(retried.allowed, retried.errors)

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
        self.assertIn(
            "unsupported sample terminal status: complete", unsupported.errors
        )

        terminated = terminate_managed_run(RUN, self.task, "needs-clarification")
        self.assertTrue(terminated.allowed, terminated.errors)
        self.assertFalse((self.workspace / ".active-sample").exists())
        repeated = terminate_managed_run(RUN, self.task, "blocked")
        self.assertFalse(repeated.allowed)
        self.assertIn("sample run is already terminal", repeated.errors)

    def test_normal_terminal_transition_releases_the_root_pointer(self):
        self.assertTrue(start_managed_run(RUN, self.task, self.request).allowed)
        self.assertTrue(transition_managed_run(RUN, self.task, "verify").allowed)

        completed = transition_managed_run(RUN, self.task, "complete")

        self.assertTrue(completed.allowed, completed.errors)
        self.assertFalse((self.workspace / ".active-sample").exists())

    def test_attach_nested_subloop_uses_existing_input_without_global_pointer(self):
        child = self.workspace / "main" / "subloops" / "subloop-001"
        child.mkdir(parents=True)
        invocation = {"schema_version": 1, "mode": "subloop"}
        (child / "invocation.json").write_text(
            json.dumps(invocation),
            encoding="utf-8",
        )

        attached = attach_managed_subloop(
            SUBLOOP_RUN,
            child,
            self.project,
            max_iterations=2,
        )

        self.assertTrue(attached.allowed, attached.errors)
        self.assertEqual(attached.state["status"], "inspect")
        self.assertEqual(attached.state["max_iterations"], 2)
        self.assertFalse(any(self.workspace.glob(".active-*")))
        self.assertEqual(
            json.loads((child / "invocation.json").read_text(encoding="utf-8")),
            invocation,
        )
        self.assertTrue(transition_managed_run(SUBLOOP_RUN, child, "verify").allowed)

    def test_attach_rejects_direct_or_wrong_parent_paths_and_root_definition(self):
        (self.task / "invocation.json").write_text("{}", encoding="utf-8")
        wrong_parent = self.workspace / "main" / "children" / "subloop-001"
        wrong_parent.mkdir(parents=True)
        (wrong_parent / "invocation.json").write_text("{}", encoding="utf-8")

        direct = attach_managed_subloop(
            SUBLOOP_RUN,
            self.task,
            self.project,
        )
        wrong = attach_managed_subloop(
            SUBLOOP_RUN,
            wrong_parent,
            self.project,
        )
        root_definition = attach_managed_subloop(
            RUN,
            wrong_parent,
            self.project,
        )

        self.assertFalse(direct.allowed)
        self.assertFalse(wrong.allowed)
        self.assertFalse(root_definition.allowed)


if __name__ == "__main__":
    unittest.main()
