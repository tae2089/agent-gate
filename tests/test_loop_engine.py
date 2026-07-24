"""Contract tests for the deterministic Agent Loop transition kernel."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from loop_engine import LoopDefinition, transition  # noqa: E402


DEFINITION = LoopDefinition(
    name="sample",
    transitions={
        "inspect": frozenset({"repair"}),
        "repair": frozenset({"verify"}),
        "verify": frozenset({"repair", "complete"}),
    },
    terminal_statuses=frozenset({"complete", "blocked", "budget-exhausted"}),
    iteration_transitions=frozenset({("verify", "repair")}),
    budget_terminal="budget-exhausted",
)


def state(**overrides):
    value = {
        "schema_version": 1,
        "status": "inspect",
        "iteration": 1,
        "max_iterations": 2,
        "input_sha256": "a" * 64,
    }
    value.update(overrides)
    return value


class LoopTransitionTest(unittest.TestCase):
    def test_declared_transition_preserves_pack_owned_state(self):
        current = state()

        result = transition(DEFINITION, current, "repair")

        self.assertTrue(result.allowed, result.errors)
        self.assertEqual(result.state["status"], "repair")
        self.assertEqual(result.state["iteration"], 1)
        self.assertEqual(result.state["input_sha256"], "a" * 64)
        self.assertEqual(current["status"], "inspect")

    def test_iteration_edge_increments_only_after_a_retry(self):
        result = transition(
            DEFINITION,
            state(status="verify"),
            "repair",
        )

        self.assertTrue(result.allowed, result.errors)
        self.assertEqual(result.state["status"], "repair")
        self.assertEqual(result.state["iteration"], 2)

    def test_exhausted_retry_reaches_the_declared_budget_terminal(self):
        result = transition(
            DEFINITION,
            state(status="verify", iteration=2),
            "repair",
        )

        self.assertTrue(result.allowed, result.errors)
        self.assertEqual(result.state["status"], "budget-exhausted")
        self.assertEqual(result.state["iteration"], 2)

    def test_invalid_and_terminal_transitions_do_not_mutate_state(self):
        invalid_state = state()
        terminal_state = state(status="complete")

        invalid = transition(DEFINITION, invalid_state, "verify")
        terminal = transition(DEFINITION, terminal_state, "repair")

        self.assertFalse(invalid.allowed)
        self.assertIn("sample transition inspect -> verify is not allowed", invalid.errors)
        self.assertEqual(invalid.state, invalid_state)
        self.assertFalse(terminal.allowed)
        self.assertIn("terminal sample state cannot transition", terminal.errors)
        self.assertEqual(terminal.state, terminal_state)

    def test_invalid_iteration_contract_is_rejected_without_mutation(self):
        invalid_state = state(iteration=0, max_iterations=True)

        result = transition(DEFINITION, invalid_state, "repair")

        self.assertFalse(result.allowed)
        self.assertIn("iteration must be a positive integer", result.errors)
        self.assertIn("max_iterations must be an integer from 1 through 10", result.errors)
        self.assertEqual(result.state, invalid_state)


if __name__ == "__main__":
    unittest.main()
