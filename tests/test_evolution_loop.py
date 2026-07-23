"""Contract tests for the autonomous evolutionary loop."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError

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
        "source": "code-analysis",
        "source_ref": "scripts/example.py:12",
        "title": "Observable failure",
        "problem": "The declared branch returns the wrong status.",
        "evidence": ["tests.test_example reproduces the wrong status"],
        "labels": [],
    }
    value.update(overrides)
    return value


class CandidatePolicyTest(unittest.TestCase):
    def test_manual_feature_preserves_verbatim_request(self):
        request = "CSV 내보내기에 실패 행의 이유를 포함해 줘."
        result = evolution_loop.validate_candidate(
            candidate(
                kind="feature",
                source="manual",
                source_ref="manual-request-2026-07-23",
                request=request,
            )
        )

        self.assertTrue(result.allowed, result.errors)
        self.assertEqual(result.candidate["request"], request)

    def test_agent_ready_github_and_jira_features_are_allowed(self):
        for source, source_ref in (
            ("github", "https://github.com/tae2089/agent-gate/issues/17"),
            ("jira", "AG-17"),
        ):
            with self.subTest(source=source):
                result = evolution_loop.validate_candidate(
                    candidate(
                        kind="feature",
                        source=source,
                        source_ref=source_ref,
                        labels=["agent-ready", "enhancement"],
                    )
                )

                self.assertTrue(result.allowed, result.errors)

    def test_unapproved_or_invented_product_features_are_rejected(self):
        cases = (
            candidate(kind="feature", source="code-analysis"),
            candidate(kind="feature", source="github", labels=["enhancement"]),
            candidate(kind="feature", source="jira", labels=[]),
            candidate(kind="feature", source="manual"),
        )
        for value in cases:
            with self.subTest(source=value["source"], labels=value["labels"]):
                result = evolution_loop.validate_candidate(value)

                self.assertFalse(result.allowed)

    def test_external_feature_start_requires_a_matching_live_discovery_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary) / "_workspace" / "sample"
            task.mkdir(parents=True)
            value = candidate(
                kind="feature",
                source="github",
                source_ref="https://github.com/tae2089/agent-gate/issues/17",
                labels=["agent-ready"],
            )

            missing = evolution_loop.start_run(task, value)
            approved = evolution_loop.start_run(
                task,
                value,
                approved_records=[
                    {
                        "source": "github",
                        "source_ref": value["source_ref"],
                        "title": value["title"],
                        "body": value["problem"],
                        "labels": ["agent-ready"],
                        "updated_at": "2026-07-23T01:02:03Z",
                    }
                ],
            )

            self.assertFalse(missing.allowed)
            self.assertIn("live discovery", " ".join(missing.errors))
            self.assertTrue(approved.allowed, approved.errors)

    def test_code_analysis_accepts_only_evidenced_non_feature_work(self):
        for kind in ("bug", "contract-violation", "technical-debt"):
            with self.subTest(kind=kind):
                result = evolution_loop.validate_candidate(candidate(kind=kind))
                self.assertTrue(result.allowed, result.errors)

        unsupported = evolution_loop.validate_candidate(
            candidate(kind="preference-refactor")
        )
        no_evidence = evolution_loop.validate_candidate(candidate(evidence=[]))

        self.assertFalse(unsupported.allowed)
        self.assertFalse(no_evidence.allowed)

    def test_unknown_fields_and_malformed_strings_are_rejected(self):
        with_unknown = candidate(score=0.95)
        malformed = candidate(title=" ", labels=["agent-ready", ""])

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


class GitHubRepositoryContextTest(unittest.TestCase):
    def test_preflight_resolves_canonical_repository_and_accepts_case_insensitive_request(self):
        commands = []

        def run_command(argv, **kwargs):
            commands.append(list(argv))
            if argv[:3] == ["gh", "auth", "status"]:
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(
                argv, 0, '{"nameWithOwner":"tae2089/agent-gate"}', ""
            )

        repository, errors = evolution_loop.resolve_github_repository(
            ROOT,
            requested_repository="TAE2089/AGENT-GATE",
            command_runner=run_command,
        )

        self.assertEqual(repository, "tae2089/agent-gate")
        self.assertEqual(errors, ())
        self.assertEqual(
            commands,
            [
                ["gh", "auth", "status"],
                ["gh", "repo", "view", "--json", "nameWithOwner"],
            ],
        )

    def test_malformed_requested_repository_is_rejected_before_gh(self):
        commands = []

        repository, errors = evolution_loop.resolve_github_repository(
            ROOT,
            requested_repository="https://github.com/tae2089/agent-gate",
            command_runner=lambda argv, **kwargs: commands.append(list(argv)),
        )

        self.assertIsNone(repository)
        self.assertIn("owner/repo", " ".join(errors))
        self.assertEqual(commands, [])

    def test_requested_repository_must_match_project_repository(self):
        def run_command(argv, **kwargs):
            if argv[:3] == ["gh", "auth", "status"]:
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(
                argv, 0, '{"nameWithOwner":"tae2089/agent-gate"}', ""
            )

        repository, errors = evolution_loop.resolve_github_repository(
            ROOT,
            requested_repository="other/project",
            command_runner=run_command,
        )

        self.assertIsNone(repository)
        self.assertIn("does not match", " ".join(errors))

    def test_preflight_failures_are_bounded_and_do_not_expose_stderr(self):
        cases = (
            (
                lambda argv, **kwargs: (_ for _ in ()).throw(
                    FileNotFoundError("missing gh")
                ),
                "could not start",
            ),
            (
                lambda argv, **kwargs: subprocess.CompletedProcess(
                    argv, 1, "", "credential material"
                ),
                "authentication failed",
            ),
            (
                lambda argv, **kwargs: (
                    subprocess.CompletedProcess(argv, 0, "", "")
                    if argv[:3] == ["gh", "auth", "status"]
                    else subprocess.CompletedProcess(
                        argv, 1, "", "private repository detail"
                    )
                ),
                "repository resolution failed",
            ),
            (
                lambda argv, **kwargs: (
                    subprocess.CompletedProcess(argv, 0, "", "")
                    if argv[:3] == ["gh", "auth", "status"]
                    else subprocess.CompletedProcess(argv, 0, "[]", "")
                ),
                "invalid JSON object",
            ),
        )

        for run_command, expected in cases:
            with self.subTest(expected=expected):
                repository, errors = evolution_loop.resolve_github_repository(
                    ROOT, command_runner=run_command
                )

                self.assertIsNone(repository)
                self.assertIn(expected, " ".join(errors))
                self.assertNotIn("credential material", " ".join(errors))
                self.assertNotIn("private repository detail", " ".join(errors))


class DiscoveryTest(unittest.TestCase):
    def test_github_issues_and_failed_ci_are_normalized(self):
        commands = []

        def run_command(argv, **kwargs):
            commands.append(list(argv))
            if argv[:3] == ["gh", "auth", "status"]:
                return subprocess.CompletedProcess(argv, 0, "", "")
            if argv[:3] == ["gh", "repo", "view"]:
                return subprocess.CompletedProcess(
                    argv, 0, '{"nameWithOwner":"tae2089/agent-gate"}', ""
                )
            if argv[1:3] == ["issue", "list"]:
                payload = [
                    {
                        "number": 17,
                        "title": "Export failure reasons",
                        "body": "Requested behavior",
                        "labels": [{"name": "agent-ready"}, {"name": "feature"}],
                        "url": "https://github.com/tae2089/agent-gate/issues/17",
                        "updatedAt": "2026-07-23T01:02:03Z",
                    }
                ]
            else:
                payload = [
                    {
                        "databaseId": 91,
                        "displayTitle": "Unit tests",
                        "conclusion": "failure",
                        "status": "completed",
                        "url": "https://github.com/tae2089/agent-gate/actions/runs/91",
                        "workflowName": "agent-gate-ci",
                        "updatedAt": "2026-07-23T02:03:04Z",
                    }
                ]
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

        result = evolution_loop.discover_evidence(
            ROOT, environment={}, command_runner=run_command
        )

        self.assertEqual(result.github_repository, "tae2089/agent-gate")
        self.assertEqual(result.errors, ("Jira discovery is not configured",))
        self.assertEqual([item["source"] for item in result.records], ["github", "ci"])
        self.assertEqual(result.records[0]["labels"], ["agent-ready", "feature"])
        self.assertEqual(result.records[1]["source_ref"], (
            "https://github.com/tae2089/agent-gate/actions/runs/91"
        ))
        github_reads = [
            command
            for command in commands
            if command[:3] in (["gh", "issue", "list"], ["gh", "run", "list"])
        ]
        self.assertEqual(len(github_reads), 2)
        self.assertTrue(
            all(
                command[command.index("--repo") + 1] == "tae2089/agent-gate"
                for command in github_reads
            )
        )

    def test_jira_search_uses_jql_and_never_returns_credentials(self):
        captured = {}

        def run_command(argv, **kwargs):
            if argv[:3] == ["gh", "auth", "status"]:
                return subprocess.CompletedProcess(argv, 0, "", "")
            if argv[:3] == ["gh", "repo", "view"]:
                return subprocess.CompletedProcess(
                    argv, 0, '{"nameWithOwner":"tae2089/agent-gate"}', ""
                )
            return subprocess.CompletedProcess(argv, 0, "[]", "")

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self):
                return json.dumps(
                    {
                        "isLast": True,
                        "issues": [
                            {
                                "key": "AG-17",
                                "fields": {
                                    "summary": "Preserve failure reasons",
                                    "description": {
                                        "type": "doc",
                                        "content": [
                                            {
                                                "type": "paragraph",
                                                "content": [
                                                    {
                                                        "type": "text",
                                                        "text": "Requested behavior",
                                                    }
                                                ],
                                            }
                                        ],
                                    },
                                    "labels": ["agent-ready", "feature"],
                                    "issuetype": {"name": "Story"},
                                    "status": {"name": "To Do"},
                                },
                            }
                        ],
                    }
                ).encode("utf-8")

        def open_request(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return Response()

        environment = {
            "AGENT_GATE_JIRA_BASE_URL": "https://example.atlassian.net",
            "AGENT_GATE_JIRA_EMAIL": "agent@example.invalid",
            "AGENT_GATE_JIRA_API_TOKEN": "non-secret-test-placeholder",
        }
        result = evolution_loop.discover_evidence(
            ROOT,
            environment=environment,
            command_runner=run_command,
            jira_opener=open_request,
        )

        self.assertEqual(result.errors, ())
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.records[0]["source"], "jira")
        self.assertEqual(result.records[0]["source_ref"], (
            "https://example.atlassian.net/browse/AG-17"
        ))
        self.assertEqual(result.records[0]["body"], "Requested behavior")
        self.assertNotIn(environment["AGENT_GATE_JIRA_API_TOKEN"], repr(result))
        request_body = json.loads(captured["request"].data.decode("utf-8"))
        self.assertIn('labels = "agent-ready"', request_body["jql"])
        self.assertEqual(captured["timeout"], 30)

    def test_source_failures_are_independent_and_bounded(self):
        def run_command(argv, **kwargs):
            if argv[:3] == ["gh", "auth", "status"]:
                return subprocess.CompletedProcess(argv, 0, "", "")
            if argv[:3] == ["gh", "repo", "view"]:
                return subprocess.CompletedProcess(
                    argv, 0, '{"nameWithOwner":"tae2089/agent-gate"}', ""
                )
            return subprocess.CompletedProcess(
                argv, 2, "", "sensitive provider diagnostics"
            )

        def open_request(request, timeout):
            raise HTTPError(request.full_url, 401, "Unauthorized", {}, None)

        environment = {
            "AGENT_GATE_JIRA_BASE_URL": "https://example.atlassian.net",
            "AGENT_GATE_JIRA_EMAIL": "agent@example.invalid",
            "AGENT_GATE_JIRA_API_TOKEN": "non-secret-test-placeholder",
        }
        result = evolution_loop.discover_evidence(
            ROOT,
            environment=environment,
            command_runner=run_command,
            jira_opener=open_request,
        )

        self.assertEqual(result.records, ())
        self.assertEqual(len(result.errors), 3)
        self.assertTrue(all("sensitive provider" not in error for error in result.errors))
        self.assertIn("Jira discovery failed with HTTP 401", result.errors)

    def test_partial_jira_configuration_does_not_make_a_request(self):
        def run_command(argv, **kwargs):
            if argv[:3] == ["gh", "auth", "status"]:
                return subprocess.CompletedProcess(argv, 0, "", "")
            if argv[:3] == ["gh", "repo", "view"]:
                return subprocess.CompletedProcess(
                    argv, 0, '{"nameWithOwner":"tae2089/agent-gate"}', ""
                )
            return subprocess.CompletedProcess(argv, 0, "[]", "")

        def unexpected_request(request, timeout):
            raise AssertionError("partial configuration must not make a request")

        result = evolution_loop.discover_evidence(
            ROOT,
            environment={"AGENT_GATE_JIRA_BASE_URL": "https://example.atlassian.net"},
            command_runner=run_command,
            jira_opener=unexpected_request,
        )

        self.assertEqual(
            result.errors,
            ("Jira discovery configuration is incomplete",),
        )

    def test_failed_github_preflight_skips_github_reads_but_keeps_jira(self):
        commands = []

        def run_command(argv, **kwargs):
            commands.append(list(argv))
            raise FileNotFoundError("missing gh")

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self):
                return json.dumps(
                    {
                        "issues": [
                            {
                                "key": "AG-21",
                                "fields": {
                                    "summary": "Independent Jira work",
                                    "description": "Jira remains available",
                                    "labels": ["agent-ready"],
                                },
                            }
                        ]
                    }
                ).encode("utf-8")

        result = evolution_loop.discover_evidence(
            ROOT,
            environment={
                "AGENT_GATE_JIRA_BASE_URL": "https://example.atlassian.net",
                "AGENT_GATE_JIRA_EMAIL": "agent@example.invalid",
                "AGENT_GATE_JIRA_API_TOKEN": "non-secret-test-placeholder",
            },
            command_runner=run_command,
            jira_opener=lambda request, timeout: Response(),
        )

        self.assertIsNone(result.github_repository)
        self.assertEqual(commands, [["gh", "auth", "status"]])
        self.assertEqual([record["source"] for record in result.records], ["jira"])
        self.assertIn("could not start", " ".join(result.errors))


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


class PublicationTest(unittest.TestCase):
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
        exclude = self.project / ".git" / "info" / "exclude"
        exclude.write_text("_workspace/\n", encoding="utf-8")
        subprocess.run(
            ["git", "branch", "-M", "main"],
            cwd=self.project,
            check=True,
        )
        subprocess.run(
            ["git", "switch", "-qc", "evolution/test"],
            cwd=self.project,
            check=True,
        )
        (self.project / "src" / "app.txt").write_text(
            "implemented\n", encoding="utf-8"
        )
        subprocess.run(
            ["git", "add", "src/app.txt"],
            cwd=self.project,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-qm", "implement candidate"],
            cwd=self.project,
            check=True,
        )
        self.assertTrue(
            evolution_loop.start_run(
                self.task,
                candidate(),
                github_repository="tae2089/agent-gate",
            ).allowed
        )
        for phase in ("execute", "evaluate", "pr-ready"):
            self.assertTrue(evolution_loop.transition_run(self.task, phase).allowed)
        self.assertTrue(
            scenario_gate.run_scenarios(self.task, self.project).result_written
        )
        (self.task / "pr-title.txt").write_text(
            "Fix observable failure\n", encoding="utf-8"
        )
        (self.task / "pr-body.md").write_text(
            "## Summary\n\nFixes the evidenced failure.\n", encoding="utf-8"
        )
        self.commands = []
        self.existing_prs = []
        self.auth_result = subprocess.CompletedProcess([], 0, "", "")
        self.repository_result = subprocess.CompletedProcess(
            [], 0, '{"nameWithOwner":"tae2089/agent-gate"}', ""
        )
        self.create_result = subprocess.CompletedProcess(
            [], 0, "https://github.com/tae2089/agent-gate/pull/42\n", ""
        )

    def tearDown(self):
        self.temp.cleanup()

    def run_command(self, argv, **kwargs):
        self.commands.append(list(argv))
        if argv[:3] == ["gh", "auth", "status"]:
            return subprocess.CompletedProcess(
                argv,
                self.auth_result.returncode,
                self.auth_result.stdout,
                self.auth_result.stderr,
            )
        if argv[:3] == ["gh", "repo", "view"]:
            return subprocess.CompletedProcess(
                argv,
                self.repository_result.returncode,
                self.repository_result.stdout,
                self.repository_result.stderr,
            )
        if argv[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps(self.existing_prs), ""
            )
        if argv[:3] == ["gh", "pr", "create"]:
            return subprocess.CompletedProcess(
                argv,
                self.create_result.returncode,
                self.create_result.stdout,
                self.create_result.stderr,
            )
        if argv[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.run(argv, **kwargs)

    def test_publish_pushes_and_opens_one_pr_without_downstream_actions(self):
        result = evolution_loop.publish_run(
            self.task,
            self.project,
            base_branch="main",
            command_runner=self.run_command,
        )

        self.assertTrue(result.allowed, result.errors)
        self.assertEqual(result.state["status"], "pr-opened")
        self.assertEqual(
            result.state["pr_url"],
            "https://github.com/tae2089/agent-gate/pull/42",
        )
        flattened = [" ".join(command) for command in self.commands]
        self.assertTrue(any(command.startswith("git push ") for command in flattened))
        self.assertTrue(any(command.startswith("gh pr create ") for command in flattened))
        pr_commands = [
            command
            for command in self.commands
            if command[:3] in (["gh", "pr", "list"], ["gh", "pr", "create"])
        ]
        self.assertTrue(
            all(
                command[command.index("--repo") + 1] == "tae2089/agent-gate"
                for command in pr_commands
            )
        )
        self.assertFalse(
            any(
                token in command
                for command in flattened
                for token in (" merge ", " deploy ", " issue close", " issue comment")
            )
        )

    def test_repeated_publish_returns_recorded_pr_without_commands(self):
        first = evolution_loop.publish_run(
            self.task,
            self.project,
            command_runner=self.run_command,
        )
        self.assertTrue(first.allowed, first.errors)
        self.commands.clear()

        repeated = evolution_loop.publish_run(
            self.task,
            self.project,
            command_runner=self.run_command,
        )

        self.assertTrue(repeated.allowed, repeated.errors)
        self.assertEqual(repeated.state["pr_url"], first.state["pr_url"])
        self.assertEqual(self.commands, [])

    def test_existing_exact_pr_is_recorded_without_push_or_create(self):
        self.existing_prs = [
            {
                "url": "https://github.com/tae2089/agent-gate/pull/41",
                "state": "OPEN",
                "headRefName": "evolution/test",
                "baseRefName": "main",
            }
        ]

        result = evolution_loop.publish_run(
            self.task,
            self.project,
            command_runner=self.run_command,
        )

        self.assertTrue(result.allowed, result.errors)
        self.assertEqual(
            result.state["pr_url"],
            "https://github.com/tae2089/agent-gate/pull/41",
        )
        self.assertFalse(any(command[:2] == ["git", "push"] for command in self.commands))
        self.assertFalse(any(command[:3] == ["gh", "pr", "create"] for command in self.commands))

    def test_dirty_worktree_blocks_before_remote_commands(self):
        (self.project / "src" / "app.txt").write_text(
            "uncommitted\n", encoding="utf-8"
        )

        result = evolution_loop.publish_run(
            self.task,
            self.project,
            command_runner=self.run_command,
        )

        self.assertFalse(result.allowed)
        self.assertIn("worktree must be clean", result.errors)
        self.assertFalse(any(command[0] == "gh" for command in self.commands))
        self.assertFalse(any(command[:2] == ["git", "push"] for command in self.commands))

    def test_failed_pr_create_records_publish_blocked(self):
        self.create_result = subprocess.CompletedProcess([], 3, "", "provider detail")

        result = evolution_loop.publish_run(
            self.task,
            self.project,
            command_runner=self.run_command,
        )

        self.assertFalse(result.allowed)
        self.assertEqual(result.state["status"], "publish-blocked")
        self.assertNotIn("provider detail", " ".join(result.errors))

    def test_repository_preflight_failure_blocks_before_pr_or_push(self):
        self.auth_result = subprocess.CompletedProcess(
            [], 2, "", "provider credential detail"
        )

        result = evolution_loop.publish_run(
            self.task,
            self.project,
            command_runner=self.run_command,
        )

        self.assertFalse(result.allowed)
        self.assertEqual(result.state["status"], "publish-blocked")
        self.assertIn("authentication failed", " ".join(result.errors))
        self.assertNotIn("provider credential detail", " ".join(result.errors))
        self.assertFalse(
            any(
                command[:3] in (["gh", "pr", "list"], ["gh", "pr", "create"])
                or command[:2] == ["git", "push"]
                for command in self.commands
            )
        )

    def test_legacy_state_without_repository_resolves_current_project(self):
        state_path = self.task / "evolution-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        del state["github_repository"]
        state_path.write_text(json.dumps(state), encoding="utf-8")

        result = evolution_loop.publish_run(
            self.task,
            self.project,
            command_runner=self.run_command,
        )

        self.assertTrue(result.allowed, result.errors)
        self.assertTrue(
            any(command[:3] == ["gh", "repo", "view"] for command in self.commands)
        )


class CommandLineTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.task = self.project / "_workspace" / "sample"
        self.task.mkdir(parents=True)
        executable_dir = self.project / "bin"
        executable_dir.mkdir()
        fake_gh = executable_dir / "gh"
        fake_gh.write_text(
            f"""#!{sys.executable}
import json
import os
import sys

if sys.argv[1:3] == ["auth", "status"]:
    if os.environ.get("AGENT_GATE_TEST_GH_AUTH_FAIL"):
        print("provider credential detail", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(0)
if sys.argv[1:3] == ["repo", "view"]:
    print(json.dumps({{"nameWithOwner": "tae2089/agent-gate"}}))
    raise SystemExit(0)
if sys.argv[1:3] in (["issue", "list"], ["run", "list"]):
    print("[]")
    raise SystemExit(0)
raise SystemExit(0)
""",
            encoding="utf-8",
        )
        fake_gh.chmod(0o755)
        self.environment = dict(os.environ)
        self.environment["PATH"] = (
            str(executable_dir)
            + os.pathsep
            + self.environment.get("PATH", "")
        )
        self.candidate_path = self.task / "candidate-input.json"
        self.candidate_path.write_text(
            json.dumps(candidate()), encoding="utf-8"
        )

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *arguments, environment=None):
        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "evolution_loop.py"),
                *arguments,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env=self.environment if environment is None else environment,
        )

    def test_start_transition_and_status_use_json_contract(self):
        started = self.run_cli(
            "start",
            str(self.task),
            "--candidate",
            str(self.candidate_path),
            "--project-root",
            str(self.project),
            "--github-repo",
            "tae2089/agent-gate",
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
        self.assertEqual(json.loads(status.stdout)["state"]["status"], "execute")
        self.assertEqual(
            json.loads(status.stdout)["state"]["github_repository"],
            "tae2089/agent-gate",
        )

    def test_start_preflight_failure_writes_no_state_or_provider_diagnostics(self):
        environment = dict(self.environment)
        environment["AGENT_GATE_TEST_GH_AUTH_FAIL"] = "1"

        result = self.run_cli(
            "start",
            str(self.task),
            "--candidate",
            str(self.candidate_path),
            "--project-root",
            str(self.project),
            "--json",
            environment=environment,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("authentication failed", result.stdout)
        self.assertNotIn("provider credential detail", result.stdout)
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
            "agent-ready",
            "needs-clarification",
            "pr-opened",
        ):
            self.assertIn(required, content)
        for forbidden in ("codex exec", "claude -p", "agy -p", "git merge"):
            self.assertNotIn(forbidden, content)

    def test_antigravity_plugin_has_root_skills_component(self):
        self.assertTrue((ROOT / "skills" / "evolution-loop" / "SKILL.md").is_file())
        manifest = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "agent-gate")

    def test_host_smoke_stays_inside_clone_and_never_bypasses_denied_writes(self):
        content = (
            ROOT / "skills" / "evolution-loop" / "SKILL.md"
        ).read_text(encoding="utf-8")
        normalized = " ".join(content.split())

        self.assertIn("working directory inside the disposable clone", normalized)
        self.assertIn("Never retry a denied write", normalized)
        self.assertIn("terminate `blocked`", normalized)

    def test_skill_and_docs_bind_one_preflighted_github_repository(self):
        skill = (
            ROOT / "skills" / "evolution-loop" / "SKILL.md"
        ).read_text(encoding="utf-8")
        docs = (ROOT / "README.md").read_text(encoding="utf-8")

        for content in (skill, docs):
            normalized = " ".join(content.split())
            self.assertIn("--github-repo <owner/repo>", normalized)
            self.assertIn("github_repository", normalized)
            self.assertIn("before Seed", normalized)
        self.assertNotIn("codex exec", skill)
        self.assertNotIn("claude -p", skill)
        self.assertNotIn("agy -p", skill)

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
                self.assertEqual(manifest["version"], "0.2.0")

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
            "agent-ready",
            "needs-clarification",
            "budget-exhausted",
            "pr-opened",
            "does not merge",
        ):
            self.assertIn(required, content)


if __name__ == "__main__":
    unittest.main()
