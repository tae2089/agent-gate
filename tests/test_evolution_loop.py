"""Contract tests for the autonomous evolutionary loop."""

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
import scenario_gate  # noqa: E402


def candidate(**overrides):
    value = {
        "schema_version": 1,
        "kind": "bug",
        "source": "manual",
        "source_ref": "manual-request-2026-07-23",
        "title": "Observable failure",
        "problem": "The declared branch returns the wrong status.",
        "evidence": ["The user reported an observable failure."],
        "labels": [],
        "request": "재현 가능한 오류를 고쳐줘.",
    }
    value.update(overrides)
    return value


class CandidatePolicyTest(unittest.TestCase):
    def test_all_supported_work_kinds_preserve_verbatim_user_request(self):
        request = "CSV 내보내기에 실패 행의 이유를 포함해 줘."
        for kind in ("feature", "bug", "contract-violation", "technical-debt"):
            with self.subTest(kind=kind):
                result = evolution_loop.validate_candidate(
                    candidate(kind=kind, request=request)
                )

                self.assertTrue(result.allowed, result.errors)
                self.assertEqual(result.candidate["request"], request)

    def test_external_sources_and_missing_user_requests_are_rejected(self):
        for source in ("github", "jira", "ci", "repository", "code-analysis"):
            with self.subTest(source=source):
                result = evolution_loop.validate_candidate(
                    candidate(source=source)
                )

                self.assertFalse(result.allowed)
                self.assertIn("source must be manual", " ".join(result.errors))

        missing = candidate()
        del missing["request"]
        empty = candidate(request=" ")

        self.assertFalse(evolution_loop.validate_candidate(missing).allowed)
        self.assertFalse(evolution_loop.validate_candidate(empty).allowed)

    def test_unsupported_kind_or_empty_evidence_is_rejected(self):
        unsupported = evolution_loop.validate_candidate(
            candidate(kind="preference-refactor")
        )
        no_evidence = evolution_loop.validate_candidate(candidate(evidence=[]))

        self.assertFalse(unsupported.allowed)
        self.assertFalse(no_evidence.allowed)

    def test_unknown_fields_and_malformed_strings_are_rejected(self):
        with_unknown = candidate(score=0.95)
        malformed = candidate(title=" ", labels=["context", ""])

        self.assertFalse(evolution_loop.validate_candidate(with_unknown).allowed)
        self.assertFalse(evolution_loop.validate_candidate(malformed).allowed)


class EvolutionStateTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.task = Path(self.temp.name) / "_workspace" / "sample"
        self.task.mkdir(parents=True)
        self.candidate = candidate()

    def tearDown(self):
        self.temp.cleanup()

    def test_start_persists_seed_phase_and_candidate_hash(self):
        result = evolution_loop.start_run(self.task, self.candidate, max_iterations=3)

        self.assertTrue(result.allowed, result.errors)
        state = json.loads(
            (self.task / "evolution-state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(state["status"], "seed")
        self.assertEqual(state["iteration"], 1)
        self.assertEqual(state["max_iterations"], 3)
        self.assertEqual(len(state["candidate_sha256"]), 64)
        self.assertEqual(
            (self.task.parent / ".active-evolution").read_text(encoding="utf-8"),
            "_workspace/sample\n",
        )

    def test_second_run_is_blocked_while_first_is_active(self):
        other = self.task.parent / "other"
        other.mkdir()
        self.assertTrue(
            evolution_loop.start_run(self.task, self.candidate).allowed
        )

        result = evolution_loop.start_run(other, self.candidate)

        self.assertFalse(result.allowed)
        self.assertIn("another evolution run is active", result.errors)
        self.assertFalse((other / "evolution-state.json").exists())

    def test_closed_phase_cycle_reaches_pr_ready(self):
        self.assertTrue(
            evolution_loop.start_run(
                self.task, self.candidate, max_iterations=3
            ).allowed
        )

        for next_phase in ("execute", "evaluate", "interview", "seed"):
            result = evolution_loop.transition_run(self.task, next_phase)
            self.assertTrue(result.allowed, (next_phase, result.errors))
        self.assertEqual(result.state["iteration"], 2)

        for next_phase in ("execute", "evaluate", "pr-ready"):
            result = evolution_loop.transition_run(self.task, next_phase)
            self.assertTrue(result.allowed, (next_phase, result.errors))

        self.assertEqual(result.state["status"], "pr-ready")

    def test_failed_completion_returns_to_execute_in_same_iteration(self):
        self.assertTrue(
            evolution_loop.start_run(
                self.task, self.candidate, max_iterations=3
            ).allowed
        )
        self.assertTrue(evolution_loop.transition_run(self.task, "execute").allowed)
        self.assertTrue(evolution_loop.transition_run(self.task, "evaluate").allowed)

        result = evolution_loop.transition_run(self.task, "execute")

        self.assertTrue(result.allowed, result.errors)
        self.assertEqual(result.state["status"], "execute")
        self.assertEqual(result.state["iteration"], 1)

    def test_invalid_seed_returns_to_interview_and_consumes_iteration(self):
        self.assertTrue(
            evolution_loop.start_run(
                self.task, self.candidate, max_iterations=3
            ).allowed
        )
        self.assertTrue(evolution_loop.transition_run(self.task, "execute").allowed)

        result = evolution_loop.transition_run(self.task, "interview")

        self.assertTrue(result.allowed, result.errors)
        self.assertEqual(result.state["status"], "interview")
        self.assertEqual(result.state["iteration"], 2)

    def test_invalid_transition_does_not_change_state(self):
        self.assertTrue(
            evolution_loop.start_run(
                self.task, self.candidate, max_iterations=3
            ).allowed
        )
        before = (self.task / "evolution-state.json").read_bytes()

        result = evolution_loop.transition_run(self.task, "evaluate")

        self.assertFalse(result.allowed)
        self.assertEqual((self.task / "evolution-state.json").read_bytes(), before)

    def test_retry_at_budget_becomes_budget_exhausted(self):
        self.assertTrue(
            evolution_loop.start_run(
                self.task, self.candidate, max_iterations=1
            ).allowed
        )
        self.assertTrue(evolution_loop.transition_run(self.task, "execute").allowed)
        self.assertTrue(evolution_loop.transition_run(self.task, "evaluate").allowed)

        result = evolution_loop.transition_run(self.task, "interview")

        self.assertTrue(result.allowed, result.errors)
        self.assertEqual(result.state["status"], "budget-exhausted")

    def test_terminal_state_cannot_transition(self):
        self.assertTrue(
            evolution_loop.start_run(
                self.task, self.candidate, max_iterations=1
            ).allowed
        )
        terminal = evolution_loop.terminate_run(self.task, "needs-clarification")
        after = evolution_loop.transition_run(self.task, "seed")

        self.assertTrue(terminal.allowed, terminal.errors)
        self.assertFalse(after.allowed)
        self.assertEqual(after.state["status"], "needs-clarification")

    def test_malformed_persisted_state_is_rejected_without_exception(self):
        self.assertTrue(
            evolution_loop.start_run(self.task, self.candidate).allowed
        )
        path = self.task / "evolution-state.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        state["iteration"] = "one"
        state["candidate_sha256"] = "not-a-hash"
        path.write_text(json.dumps(state), encoding="utf-8")

        result = evolution_loop.transition_run(self.task, "execute")

        self.assertFalse(result.allowed)
        self.assertTrue(
            any("iteration" in error for error in result.errors),
            result.errors,
        )


class EvaluationGateTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.task = self.project / "_workspace" / "sample"
        self.task.mkdir(parents=True)
        (self.task / "task.md").write_text(TASK, encoding="utf-8")
        (self.task / "implementation.md").write_text(
            IMPLEMENTATION, encoding="utf-8"
        )
        (self.task / "scenario-contract.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "scenarios": [
                        {
                            "id": "S-PASS",
                            "title": "Passing scenario",
                            "command": [sys.executable, "-c", "raise SystemExit(0)"],
                            "given": ["a valid implementation"],
                            "when": ["the scenario runs"],
                            "then": ["the process exits successfully"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        init_git_project(self.project)
        self.assertTrue(
            evolution_loop.start_run(
                self.task, candidate(), max_iterations=2
            ).allowed
        )
        self.assertTrue(evolution_loop.transition_run(self.task, "execute").allowed)
        self.assertTrue(evolution_loop.transition_run(self.task, "evaluate").allowed)
        self.assertTrue(
            scenario_gate.run_scenarios(self.task, self.project).result_written
        )

    def tearDown(self):
        self.temp.cleanup()

    def evaluation(self, verdict="pr-ready", **check_overrides):
        checks = {
            name: {
                "passed": True,
                "evidence": [f"{name} verified against the current diff"],
            }
            for name in evolution_loop.EVALUATION_CHECK_NAMES
        }
        checks.update(check_overrides)
        state = json.loads(
            (self.task / "evolution-state.json").read_text(encoding="utf-8")
        )
        result_content = (self.task / "scenario-result.json").read_bytes()
        return {
            "schema_version": 1,
            "verdict": verdict,
            "candidate_sha256": state["candidate_sha256"],
            "scenario_result_sha256": hashlib.sha256(result_content).hexdigest(),
            "checks": checks,
            "findings": [],
        }

    def test_current_complete_minimal_evaluation_becomes_pr_ready(self):
        result = evolution_loop.evaluate_run(
            self.task, self.project, self.evaluation()
        )

        self.assertTrue(result.allowed, result.errors)
        self.assertEqual(result.state["status"], "pr-ready")
        self.assertTrue(
            (self.task / "iterations" / "001" / "evaluation.json").is_file()
        )

    def test_stale_completion_blocks_without_changing_phase(self):
        (self.project / "src" / "app.txt").write_text(
            "changed after scenarios\n", encoding="utf-8"
        )

        result = evolution_loop.evaluate_run(
            self.task, self.project, self.evaluation()
        )

        self.assertFalse(result.allowed)
        self.assertIn("scenario completion is not current and complete", result.errors)
        self.assertEqual(result.state["status"], "evaluate")

    def test_pr_ready_rejects_failed_or_unexplained_simplicity_checks(self):
        failed = {
            "passed": False,
            "evidence": ["a new compatibility layer has no known consumer"],
        }
        unexplained = {"passed": True, "evidence": []}

        for name, value in (
            ("compatibility_has_consumer", failed),
            ("simpler_alternative_considered", unexplained),
        ):
            with self.subTest(name=name):
                result = evolution_loop.evaluate_run(
                    self.task,
                    self.project,
                    self.evaluation(**{name: value}),
                )
                self.assertFalse(result.allowed)
                self.assertEqual(result.state["status"], "evaluate")

    def test_actionable_failed_check_returns_to_interview(self):
        evaluation = self.evaluation(
            verdict="iterate",
            no_speculative_abstraction={
                "passed": False,
                "evidence": ["the generic provider registry has one consumer"],
            },
        )
        evaluation["findings"] = ["Remove the speculative provider registry."]

        result = evolution_loop.evaluate_run(
            self.task, self.project, evaluation
        )

        self.assertTrue(result.allowed, result.errors)
        self.assertEqual(result.state["status"], "interview")
        self.assertEqual(result.state["iteration"], 2)

    def test_ambiguous_product_judgment_terminates_without_pr(self):
        evaluation = self.evaluation(verdict="needs-clarification")
        evaluation["findings"] = ["The issue does not define compatibility behavior."]

        result = evolution_loop.evaluate_run(
            self.task, self.project, evaluation
        )

        self.assertTrue(result.allowed, result.errors)
        self.assertEqual(result.state["status"], "needs-clarification")


class PullRequestReceiptTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.task = self.project / "_workspace" / "sample"
        self.task.mkdir(parents=True)
        (self.task / "task.md").write_text(TASK, encoding="utf-8")
        (self.task / "implementation.md").write_text(
            IMPLEMENTATION, encoding="utf-8"
        )
        (self.task / "scenario-contract.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "scenarios": [
                        {
                            "id": "S-PASS",
                            "title": "Passing scenario",
                            "command": [sys.executable, "-c", "raise SystemExit(0)"],
                            "given": ["a committed implementation"],
                            "when": ["the scenario runs"],
                            "then": ["the process exits successfully"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        init_git_project(self.project)
        (self.project / ".git" / "info" / "exclude").write_text(
            "_workspace/\n", encoding="utf-8"
        )
        self.assertTrue(
            evolution_loop.start_run(self.task, candidate()).allowed
        )
        for phase in ("execute", "evaluate", "pr-ready"):
            self.assertTrue(evolution_loop.transition_run(self.task, phase).allowed)
        self.assertTrue(
            scenario_gate.run_scenarios(self.task, self.project).result_written
        )
        self.receipt_url = "https://code.example/reviews/42"

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, url):
        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "evolution_loop.py"),
                "record-pr",
                str(self.task),
                "--project-root",
                str(self.project),
                "--url",
                url,
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_cli_records_one_current_https_receipt(self):
        result = self.run_cli(self.receipt_url)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = json.loads(result.stdout)["state"]
        self.assertEqual(state["status"], "pr-opened")
        self.assertEqual(state["pr_url"], self.receipt_url)

    def test_same_receipt_is_idempotent_but_a_different_receipt_conflicts(self):
        first = evolution_loop.record_pr(
            self.task, self.project, self.receipt_url
        )
        repeated = evolution_loop.record_pr(
            self.task, self.project, self.receipt_url
        )
        conflicting = evolution_loop.record_pr(
            self.task, self.project, "https://code.example/reviews/43"
        )

        self.assertTrue(first.allowed, first.errors)
        self.assertTrue(repeated.allowed, repeated.errors)
        self.assertFalse(conflicting.allowed)
        self.assertIn("different", " ".join(conflicting.errors))
        self.assertEqual(conflicting.state["pr_url"], self.receipt_url)

    def test_invalid_receipts_leave_pr_ready_state_unchanged(self):
        invalid_urls = (
            "http://code.example/reviews/42",
            "https://code.example",
            "https://user:placeholder@code.example/reviews/42",
            "https://code.example/reviews/42?draft=true",
            " https://code.example/reviews/42",
        )

        for url in invalid_urls:
            with self.subTest(url=url):
                result = evolution_loop.record_pr(
                    self.task, self.project, url
                )

                self.assertFalse(result.allowed)
                self.assertEqual(result.state["status"], "pr-ready")
                self.assertIsNone(result.state["pr_url"])

    def test_wrong_phase_leaves_state_unchanged(self):
        state_path = self.task / "evolution-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["status"] = "execute"
        state_path.write_text(json.dumps(state), encoding="utf-8")

        result = evolution_loop.record_pr(
            self.task, self.project, self.receipt_url
        )

        self.assertFalse(result.allowed)
        self.assertIn("not pr-ready", " ".join(result.errors))
        self.assertEqual(result.state["status"], "execute")
        self.assertIsNone(result.state["pr_url"])

    def test_stale_completion_leaves_pr_ready_state_unchanged(self):
        (self.project / "src" / "app.txt").write_text(
            "changed after evaluation\n", encoding="utf-8"
        )

        result = evolution_loop.record_pr(
            self.task, self.project, self.receipt_url
        )

        self.assertFalse(result.allowed)
        self.assertIn("completion", " ".join(result.errors))
        self.assertEqual(result.state["status"], "pr-ready")
        self.assertIsNone(result.state["pr_url"])


class CommandLineTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.task = self.project / "_workspace" / "sample"
        self.task.mkdir(parents=True)
        self.candidate_path = self.task / "candidate-input.json"
        self.candidate_path.write_text(
            json.dumps(candidate()), encoding="utf-8"
        )

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *arguments):
        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "evolution_loop.py"),
                *arguments,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_start_transition_and_status_use_json_contract(self):
        started = self.run_cli(
            "start",
            str(self.task),
            "--candidate",
            str(self.candidate_path),
            "--project-root",
            str(self.project),
            "--max-iterations",
            "2",
            "--json",
        )
        transitioned = self.run_cli(
            "transition",
            str(self.task),
            "execute",
            "--project-root",
            str(self.project),
            "--json",
        )
        status = self.run_cli(
            "status",
            "--project-root",
            str(self.project),
            "--json",
        )

        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        self.assertEqual(transitioned.returncode, 0, transitioned.stdout + transitioned.stderr)
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        self.assertEqual(
            json.loads(status.stdout)["state"],
            {
                "candidate_sha256": json.loads(started.stdout)["state"][
                    "candidate_sha256"
                ],
                "iteration": 1,
                "max_iterations": 2,
                "pr_url": None,
                "schema_version": 1,
                "status": "execute",
            },
        )

    def test_removed_provider_argument_is_rejected_without_state(self):
        result = self.run_cli(
            "start",
            str(self.task),
            "--candidate",
            str(self.candidate_path),
            "--project-root",
            str(self.project),
            "--github-repo",
            "owner/repository",
            "--json",
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("unrecognized arguments", result.stderr)
        self.assertFalse((self.task / "evolution-state.json").exists())

    def test_task_outside_direct_workspace_is_rejected(self):
        outside = self.project / "outside"
        outside.mkdir()
        candidate_path = outside / "candidate.json"
        candidate_path.write_text(json.dumps(candidate()), encoding="utf-8")

        result = self.run_cli(
            "start",
            str(outside),
            "--candidate",
            str(candidate_path),
            "--project-root",
            str(self.project),
            "--json",
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("direct _workspace task", result.stdout)
        self.assertFalse((outside / "evolution-state.json").exists())

    def test_cli_and_core_expose_no_provider_implementation(self):
        help_result = self.run_cli("--help")
        source = (
            ROOT / "scripts" / "evolution_loop.py"
        ).read_text(encoding="utf-8")

        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("record-pr", help_result.stdout)
        self.assertNotIn("publish", help_result.stdout)
        self.assertNotIn("--github-repo", help_result.stdout)
        for forbidden in (
            "import subprocess",
            "github_repository",
            "resolve_github_repository",
            "def publish_run",
            '"gh"',
            '"git", "push"',
            "AGENT_GATE_JIRA_",
            "def discover_evidence",
        ):
            self.assertNotIn(forbidden, source)


class TargetRepositoryPortabilityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / "product-repository"
        self.project.mkdir()
        init_git_project(self.project)
        (self.project / ".git" / "info" / "exclude").write_text(
            "_workspace/\n", encoding="utf-8"
        )
        self.task = self.project / "_workspace" / "portable-evolution"
        self.task.mkdir(parents=True)
        (self.task / "candidate-input.json").write_text(
            json.dumps(candidate()), encoding="utf-8"
        )
        (self.task / "task.md").write_text(TASK, encoding="utf-8")
        (self.task / "implementation.md").write_text(
            IMPLEMENTATION, encoding="utf-8"
        )
        (self.task / "scenario-contract.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "scenarios": [
                        {
                            "id": "S-REPOSITORY-OWNED-TEST",
                            "title": "The target repository behavior passes",
                            "command": [
                                sys.executable,
                                "tests/verify_portability.py",
                            ],
                            "given": ["the target repository owns its test"],
                            "when": ["the bundled runtime executes the scenario"],
                            "then": ["the target behavior passes"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def run_script(self, name, *arguments):
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / name), *arguments],
            cwd=self.project,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def run_ok(self, name, *arguments):
        result = self.run_script(name, *arguments)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_bundled_runtime_drives_non_agent_gate_repository_to_pr_ready(self):
        task = "_workspace/portable-evolution"
        common = ("--project-root", ".", "--json")
        self.run_ok(
            "evolution_loop.py",
            "start",
            task,
            "--candidate",
            f"{task}/candidate-input.json",
            *common,
        )
        self.run_ok(
            "scenario_gate.py", "design", task, "--activate", *common
        )
        self.run_ok(
            "evolution_loop.py", "transition", task, "execute", *common
        )

        repository_test = self.project / "tests" / "verify_portability.py"
        repository_test.write_text(
            "from pathlib import Path\n"
            "raise SystemExit("
            "0 if Path('src/app.txt').read_text() == 'portable\\n' else 1"
            ")\n",
            encoding="utf-8",
        )
        def run_repository_test():
            return subprocess.run(
                [sys.executable, str(repository_test)],
                cwd=self.project,
                capture_output=True,
                text=True,
                timeout=30,
            )

        red = run_repository_test()
        self.assertNotEqual(red.returncode, 0)
        (self.project / "src" / "app.txt").write_text(
            "portable\n", encoding="utf-8"
        )
        green = run_repository_test()
        self.assertEqual(green.returncode, 0, green.stdout + green.stderr)
        for command in (
            ["git", "add", "src/app.txt", "tests/verify_portability.py"],
            ["git", "commit", "-qm", "Implement portable behavior"],
        ):
            subprocess.run(
                command,
                cwd=self.project,
                check=True,
                capture_output=True,
                text=True,
            )

        self.run_ok(
            "evolution_loop.py", "transition", task, "evaluate", *common
        )
        self.run_ok("scenario_gate.py", "run", task, *common)
        state = json.loads(
            (self.task / "evolution-state.json").read_text(encoding="utf-8")
        )
        scenario_content = (self.task / "scenario-result.json").read_bytes()
        evaluation = {
            "schema_version": 1,
            "verdict": "pr-ready",
            "candidate_sha256": state["candidate_sha256"],
            "scenario_result_sha256": hashlib.sha256(
                scenario_content
            ).hexdigest(),
            "checks": {
                name: {
                    "passed": True,
                    "evidence": [f"{name} verified in the target diff"],
                }
                for name in evolution_loop.EVALUATION_CHECK_NAMES
            },
            "findings": [],
        }
        (self.task / "evaluation-input.json").write_text(
            json.dumps(evaluation), encoding="utf-8"
        )
        evaluated = self.run_ok(
            "evolution_loop.py",
            "evaluate",
            task,
            "--evaluation",
            f"{task}/evaluation-input.json",
            *common,
        )

        self.assertEqual(
            json.loads(evaluated.stdout)["state"]["status"], "pr-ready"
        )
        self.assertFalse(
            (self.project / "scripts" / "evolution_loop.py").exists()
        )
        self.assertTrue(
            (self.task / "iterations" / "001" / "evaluation.json").is_file()
        )


class SkillPackagingTest(unittest.TestCase):
    def test_one_canonical_skill_is_shared_by_all_three_hosts(self):
        canonical = ROOT / "skills" / "evolution-loop"
        claude_entry = ROOT / ".claude" / "skills" / "evolution-loop"
        codex_entry = ROOT / ".agents" / "skills" / "evolution-loop"

        self.assertTrue((canonical / "SKILL.md").is_file())
        self.assertTrue(claude_entry.is_symlink())
        self.assertTrue(codex_entry.is_symlink())
        self.assertEqual(claude_entry.resolve(strict=True), canonical.resolve(strict=True))
        self.assertEqual(codex_entry.resolve(strict=True), canonical.resolve(strict=True))

    def test_skill_uses_shared_artifacts_and_no_host_command_forks(self):
        content = (
            ROOT / "skills" / "evolution-loop" / "SKILL.md"
        ).read_text(encoding="utf-8")

        for required in (
            "Interview",
            "Seed",
            "Execute",
            "Evaluate",
            "candidate.json",
            "evolution-state.json",
            "evaluation.json",
            "scenario_gate.py",
            "evolution_loop.py",
            "verbatim user request",
            "needs-clarification",
            "pr-opened",
        ):
            self.assertIn(required, content)
        for forbidden in ("codex exec", "claude -p", "agy -p", "git merge"):
            self.assertNotIn(forbidden, content)

    def test_antigravity_plugin_has_root_skills_component(self):
        self.assertTrue((ROOT / "skills" / "evolution-loop" / "SKILL.md").is_file())
        manifest = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "agent-loop")

    def test_host_smoke_stays_inside_clone_and_never_bypasses_denied_writes(self):
        content = (
            ROOT / "skills" / "evolution-loop" / "SKILL.md"
        ).read_text(encoding="utf-8")
        normalized = " ".join(content.split())

        self.assertIn("working directory inside the disposable clone", normalized)
        self.assertIn("Never retry a denied write", normalized)
        self.assertIn("terminate `blocked`", normalized)

    def test_skill_and_docs_make_user_request_the_only_entrypoint(self):
        skill = (
            ROOT / "skills" / "evolution-loop" / "SKILL.md"
        ).read_text(encoding="utf-8")
        docs = (ROOT / "README.md").read_text(encoding="utf-8")
        plugin_docs = (ROOT / "PLUGIN.md").read_text(encoding="utf-8")

        self.assertIn("sole trigger", skill.lower())
        self.assertIn("유일한 진입점", docs)
        self.assertIn("sole trigger", plugin_docs.lower())
        for content in (skill, docs, plugin_docs):
            normalized = " ".join(content.split()).lower()
            self.assertIn("mcp", normalized)
            self.assertIn("skill", normalized)
            self.assertNotIn("agent-ready", normalized)
            self.assertNotIn("evolution_loop.py discover", normalized)
        self.assertNotIn("codex exec", skill)
        self.assertNotIn("claude -p", skill)
        self.assertNotIn("agy -p", skill)

    def test_skill_and_docs_delegate_remote_publication_to_the_host(self):
        skill = (
            ROOT / "skills" / "evolution-loop" / "SKILL.md"
        ).read_text(encoding="utf-8")
        docs = (ROOT / "README.md").read_text(encoding="utf-8")
        plugin_docs = (ROOT / "PLUGIN.md").read_text(encoding="utf-8")

        self.assertIn("GitHub MCP", skill)
        for content in (skill, docs, plugin_docs):
            normalized = " ".join(content.split())
            self.assertIn("record-pr", normalized)
            self.assertNotIn("--github-repo", normalized)
            self.assertNotIn("github_repository", normalized)
            self.assertNotIn("evolution_loop.py publish", normalized)
            self.assertNotIn("publish-blocked", normalized)
            self.assertNotIn("publish-uncertain", normalized)

    def test_skill_targets_current_repository_through_plugin_runtime(self):
        skill = (
            ROOT / "skills" / "evolution-loop" / "SKILL.md"
        ).read_text(encoding="utf-8")
        docs = (ROOT / "README.md").read_text(encoding="utf-8")
        plugin_docs = (ROOT / "PLUGIN.md").read_text(encoding="utf-8")

        for content in (skill, docs, plugin_docs):
            normalized = " ".join(content.split())
            self.assertIn("target repository", normalized.lower())
            self.assertIn("AGENT_LOOP_ROOT", normalized)
            self.assertIn("PROJECT_ROOT", normalized)
        self.assertIn(
            "$AGENT_LOOP_ROOT/scripts/evolution_loop.py", skill
        )
        self.assertIn("$AGENT_LOOP_ROOT/scripts/scenario_gate.py", skill)
        self.assertNotIn("python3 scripts/evolution_loop.py", skill)
        self.assertNotIn("python3 scripts/scenario_gate.py", skill)
        self.assertNotIn("Operate only in the `agent-loop` repository", skill)
        self.assertNotIn(
            "self-evolution of the `agent-loop` repository", skill
        )

    def test_execute_uses_repository_native_verification(self):
        skill = (
            ROOT / "skills" / "evolution-loop" / "SKILL.md"
        ).read_text(encoding="utf-8")
        execute = skill.split("## Execute", 1)[1].split("## Evaluate", 1)[0]

        self.assertIn("repository-native", execute)
        self.assertIn("direct argv", execute)
        self.assertIn("CI", execute)
        self.assertNotIn("replay audit", execute)
        self.assertNotIn("plugin validator", execute)

    def test_public_metadata_describes_the_optional_evolution_loop(self):
        paths = (
            ".claude-plugin/plugin.json",
            ".codex-plugin/plugin.json",
            ".gemini-extension.json",
            "plugin.json",
            ".claude-plugin/marketplace.json",
        )
        for relative in paths:
            with self.subTest(path=relative):
                content = (ROOT / relative).read_text(encoding="utf-8").lower()
                self.assertIn("evolution", content)

    def test_versioned_manifests_share_the_minor_feature_version(self):
        for relative in (
            ".claude-plugin/plugin.json",
            ".codex-plugin/plugin.json",
            ".gemini-extension.json",
        ):
            with self.subTest(path=relative):
                manifest = json.loads((ROOT / relative).read_text(encoding="utf-8"))
                self.assertEqual(manifest["version"], "0.4.0")

    def test_user_docs_define_evidence_terminals_and_no_merge_boundary(self):
        content = (
            (ROOT / "README.md").read_text(encoding="utf-8")
            + (ROOT / "PLUGIN.md").read_text(encoding="utf-8")
        ).lower()

        for required in (
            "interview",
            "seed",
            "execute",
            "evaluate",
            "user request",
            "mcp",
            "needs-clarification",
            "budget-exhausted",
            "pr-opened",
            "does not merge",
        ):
            self.assertIn(required, content)


if __name__ == "__main__":
    unittest.main()
