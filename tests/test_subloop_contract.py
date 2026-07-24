"""Contracts shared by Main Loop and bounded Subloop executions."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import subloop_contract  # noqa: E402


def parent_context(**overrides):
    value = {
        "run_id": "evolution-001",
        "task_ref": "_workspace/evolution-example",
        "state_sha256": "a" * 64,
        "scope": ["src", "tests"],
        "permissions": [
            "read-repository",
            "modify-worktree",
            "run-local-verification",
        ],
        "remaining_iterations": 4,
        "source_snapshot_sha256": "b" * 64,
    }
    value.update(overrides)
    return value


def invocation(**overrides):
    value = {
        "schema_version": 1,
        "invocation_id": "subloop-001",
        "pack": "assurance-loop",
        "mode": "subloop",
        "parent": {
            "run_id": "evolution-001",
            "task_ref": "_workspace/evolution-example",
            "state_sha256": "a" * 64,
        },
        "objective": "Verify AC-1 against the current implementation.",
        "requirements": ["AC-1"],
        "scope": ["src/auth", "tests"],
        "source_snapshot": {
            "ref": "source-snapshot.json",
            "sha256": "b" * 64,
        },
        "permissions": [
            "read-repository",
            "run-local-verification",
        ],
        "budget": {"iteration_limit": 2},
        "completion_task_ref": "_workspace/evolution-example",
    }
    value.update(overrides)
    return value


def invocation_sha(value):
    content = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()
    return hashlib.sha256(content).hexdigest()


def result(value, **overrides):
    output = {
        "schema_version": 1,
        "invocation_id": value["invocation_id"],
        "invocation_sha256": invocation_sha(value),
        "pack": value["pack"],
        "status": "completed",
        "summary": "The assigned assurance objective is complete.",
        "finding_refs": [],
        "changed_paths": [],
        "evidence_refs": ["assurance-report.json"],
        "budget_usage": {"iterations_used": 1},
        "completion_receipt": {
            "task_ref": "_workspace/evolution-example",
            "scenario_result_sha256": "c" * 64,
        },
        "decision": None,
        "source_snapshot_after_sha256": "d" * 64,
    }
    output.update(overrides)
    return output


class PackProfileTest(unittest.TestCase):
    def test_profile_declares_supported_execution_modes_only(self):
        profile = subloop_contract.PackProfile(
            name="assurance-loop",
            supported_modes=frozenset({"standalone", "subloop"}),
        )

        self.assertTrue(profile.supports("standalone"))
        self.assertTrue(profile.supports("subloop"))
        self.assertFalse(profile.supports("main"))

        with self.assertRaises(ValueError):
            subloop_contract.PackProfile(
                name="bad-loop",
                supported_modes=frozenset({"subloop", "nested-subloop"}),
            )


class InvocationValidationTest(unittest.TestCase):
    def setUp(self):
        self.profile = subloop_contract.PackProfile(
            name="assurance-loop",
            supported_modes=frozenset({"standalone", "subloop"}),
        )

    def test_valid_invocation_is_normalized_and_hash_bound(self):
        value = invocation()

        validated = subloop_contract.validate_invocation(
            value,
            self.profile,
            parent_context(),
        )

        self.assertTrue(validated.allowed, validated.errors)
        self.assertEqual(validated.value, value)
        self.assertEqual(validated.sha256, invocation_sha(value))

    def test_parent_binding_and_source_snapshot_must_be_current(self):
        stale_parent = invocation()
        stale_parent["parent"]["state_sha256"] = "e" * 64
        stale_source = invocation(
            source_snapshot={
                "ref": "source-snapshot.json",
                "sha256": "f" * 64,
            }
        )

        parent_result = subloop_contract.validate_invocation(
            stale_parent,
            self.profile,
            parent_context(),
        )
        source_result = subloop_contract.validate_invocation(
            stale_source,
            self.profile,
            parent_context(),
        )

        self.assertFalse(parent_result.allowed)
        self.assertIn("parent state", " ".join(parent_result.errors))
        self.assertFalse(source_result.allowed)
        self.assertIn("source snapshot", " ".join(source_result.errors))

    def test_scope_permissions_and_budget_cannot_expand_parent(self):
        broader_scope = invocation(scope=["docs"])
        broader_permissions = invocation(
            permissions=["read-repository", "read-external-sources"]
        )
        too_much_budget = invocation(budget={"iteration_limit": 5})

        scope_result = subloop_contract.validate_invocation(
            broader_scope,
            self.profile,
            parent_context(),
        )
        permission_result = subloop_contract.validate_invocation(
            broader_permissions,
            self.profile,
            parent_context(),
        )
        budget_result = subloop_contract.validate_invocation(
            too_much_budget,
            self.profile,
            parent_context(),
        )

        self.assertFalse(scope_result.allowed)
        self.assertIn("scope", " ".join(scope_result.errors))
        self.assertFalse(permission_result.allowed)
        self.assertIn("permissions", " ".join(permission_result.errors))
        self.assertFalse(budget_result.allowed)
        self.assertIn("remaining", " ".join(budget_result.errors))

    def test_forbidden_external_capabilities_and_nested_dispatch_are_rejected(self):
        for capability in ("push", "publish", "merge", "deploy", "invoke-subloop"):
            with self.subTest(capability=capability):
                value = invocation(
                    permissions=["read-repository", capability],
                )
                validated = subloop_contract.validate_invocation(
                    value,
                    self.profile,
                    parent_context(
                        permissions=["read-repository", capability],
                    ),
                )
                self.assertFalse(validated.allowed)
                self.assertIn("unsupported permission", " ".join(validated.errors))

    def test_exact_schema_and_mode_support_are_enforced(self):
        unknown = invocation(workflow={"steps": []})
        unsupported_profile = subloop_contract.PackProfile(
            name="assurance-loop",
            supported_modes=frozenset({"standalone"}),
        )

        unknown_result = subloop_contract.validate_invocation(
            unknown,
            self.profile,
            parent_context(),
        )
        mode_result = subloop_contract.validate_invocation(
            invocation(),
            unsupported_profile,
            parent_context(),
        )

        self.assertFalse(unknown_result.allowed)
        self.assertIn("unknown fields", " ".join(unknown_result.errors))
        self.assertFalse(mode_result.allowed)
        self.assertIn("does not support", " ".join(mode_result.errors))


class ResultValidationTest(unittest.TestCase):
    def setUp(self):
        self.invocation = invocation()

    def validate(self, value, current_source="d" * 64):
        return subloop_contract.validate_result(
            value,
            self.invocation,
            current_source_snapshot_sha256=current_source,
        )

    def test_all_five_terminal_statuses_have_explicit_semantics(self):
        cases = (
            result(self.invocation),
            result(
                self.invocation,
                status="changes-requested",
                finding_refs=["assurance-report.json#A-001"],
                completion_receipt=None,
            ),
            result(
                self.invocation,
                status="needs-decision",
                decision={
                    "question": "Should compatibility be retained?",
                    "options": ["retain", "remove"],
                },
                completion_receipt=None,
            ),
            result(
                self.invocation,
                status="blocked",
                evidence_refs=["blocker.json"],
                completion_receipt=None,
            ),
            result(
                self.invocation,
                status="budget-exhausted",
                budget_usage={"iterations_used": 2},
                evidence_refs=["budget.json"],
                completion_receipt=None,
            ),
        )

        for value in cases:
            with self.subTest(status=value["status"]):
                validated = self.validate(value)
                self.assertTrue(validated.allowed, validated.errors)

    def test_result_is_bound_to_invocation_pack_and_current_source(self):
        stale_invocation = result(self.invocation, invocation_sha256="e" * 64)
        wrong_pack = result(self.invocation, pack="debug-loop")
        stale_source = result(
            self.invocation,
            source_snapshot_after_sha256="f" * 64,
        )

        self.assertFalse(self.validate(stale_invocation).allowed)
        self.assertFalse(self.validate(wrong_pack).allowed)
        self.assertFalse(self.validate(stale_source).allowed)

    def test_changed_paths_require_permission_and_stay_inside_scope(self):
        read_only = copy.deepcopy(self.invocation)
        read_only["permissions"] = ["read-repository", "run-local-verification"]
        unauthorized = result(
            read_only,
            changed_paths=["src/auth/session.py"],
            completion_receipt=None,
        )

        editable = copy.deepcopy(self.invocation)
        editable["permissions"].append("modify-worktree")
        outside = result(
            editable,
            changed_paths=["docs/design.md"],
            completion_receipt=None,
        )
        inside = result(
            editable,
            changed_paths=["src/auth/session.py", "tests/test_auth.py"],
        )

        self.invocation = read_only
        self.assertFalse(self.validate(unauthorized).allowed)
        self.invocation = editable
        self.assertFalse(self.validate(outside).allowed)
        self.assertTrue(self.validate(inside).allowed)

    def test_status_specific_requirements_and_budget_are_enforced(self):
        missing_findings = result(
            self.invocation,
            status="changes-requested",
            completion_receipt=None,
        )
        missing_decision = result(
            self.invocation,
            status="needs-decision",
            completion_receipt=None,
        )
        premature_exhaustion = result(
            self.invocation,
            status="budget-exhausted",
            completion_receipt=None,
        )
        over_budget = result(
            self.invocation,
            budget_usage={"iterations_used": 3},
        )

        for value in (
            missing_findings,
            missing_decision,
            premature_exhaustion,
            over_budget,
        ):
            with self.subTest(status=value["status"]):
                self.assertFalse(self.validate(value).allowed)


if __name__ == "__main__":
    unittest.main()
