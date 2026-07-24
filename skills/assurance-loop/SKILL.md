---
name: assurance-loop
description: Assess an explicitly requested implementation, diff, branch, or pull request for requirements conformance, missing or excessive behavior, failure and compatibility risks, module quality, overengineering, and regression protection. Use for read-only assurance or user-authorized fixes, either standalone or as an Evolution Main Subloop; do not use for CI-specific repair or autonomous PR selection.
---

# Assurance Loop

Assess all six categories before returning a result:

1. requirements conformance;
2. missing or overimplemented requirements;
3. failure, boundary, and compatibility cases;
4. code quality and module responsibility;
5. unnecessary abstraction and complexity;
6. test quality and regression prevention.

The deterministic runtime validates request authority, immutable receipts,
assessment shape, retry budget, and terminal status. Semantic findings remain
the host's responsibility.

## Boundaries

- Treat the verbatim user request or parent invocation as the only authority.
- Review only the declared source snapshot and scope.
- Do not invent findings, weaken tests, or expand into unrelated cleanup.
- Never publish, push, comment, approve, merge, deploy, or invoke another
  Subloop.
- In read-only mode, return `changes-requested` for actionable findings.
- Modify the worktree only when `modify-worktree` is explicitly present.
- Use `completed`, `changes-requested`, `needs-decision`, `blocked`, or
  `budget-exhausted` as terminal result language.

## Resolve execution mode

Resolve `PROJECT_ROOT` as the target Git worktree and `AGENT_LOOP_ROOT` as the
parent of this skill's `skills/` directory.

- **Standalone:** resume the root task from `_workspace/.active-run`, or start
  one direct `_workspace/assurance-<slug>` task from the explicit user request.
- **Subloop:** use only the invocation directory supplied by Evolution Main,
  nested at `_workspace/<main>/subloops/<invocation-id>`. Inherit its
  requirements, scope, source snapshot, permissions, budget, and parent
  Completion task. Do not create a global pointer or another Design.

## Standalone start

Create `request-input.json`:

```json
{
  "schema_version": 2,
  "source": "manual",
  "source_ref": "conversation:<request-ref>",
  "request": "<verbatim user request>",
  "target": "base=<oid>; head=<oid>; worktree=<include|exclude>; untracked=<include|exclude>",
  "requirements": ["AC-1"],
  "scope": ["src", "tests"],
  "permissions": ["read-repository", "run-local-verification"],
  "evidence": ["target resolution evidence"]
}
```

Add `modify-worktree` only when the user requested fixes. Start, create the
root Design artifacts, activate Design, and enter Assess:

```bash
python3 "$AGENT_LOOP_ROOT/scripts/assurance_loop.py" start \
  _workspace/assurance-<slug> \
  --request _workspace/assurance-<slug>/request-input.json \
  --project-root "$PROJECT_ROOT" --max-iterations 3 --json
python3 "$AGENT_LOOP_ROOT/scripts/scenario_gate.py" design \
  _workspace/assurance-<slug> \
  --project-root "$PROJECT_ROOT" --activate --json
python3 "$AGENT_LOOP_ROOT/scripts/assurance_loop.py" transition \
  _workspace/assurance-<slug> assess \
  --project-root "$PROJECT_ROOT" --json
```

Pin mutable PR or branch names to immutable OIDs before start. Declare only
safe repository-native scenario argv.

## Assess

Inspect the full requested boundary and trace each requirement to source and
tests. Use unique `A-NNN` findings with `P0` through `P3` severity. Every
finding must name its assessment category, requirement references, observable
evidence, and smallest corrective action.

Run the scenarios, copy the canonical request hash and exact scenario-result
hash, then write `assessment-input.json`:

```json
{
  "schema_version": 2,
  "request_sha256": "<request hash>",
  "scenario_result_sha256": "<scenario result hash>",
  "assessments": {
    "requirements_conformance": {
      "status": "fail",
      "findings": [{
        "id": "A-001",
        "category": "requirements_conformance",
        "severity": "P1",
        "title": "Requirement gap",
        "requirement_refs": ["AC-1"],
        "evidence": ["src/example.py:42 contradicts AC-1"],
        "action": "Implement AC-1 and add its regression test"
      }]
    },
    "missing_or_overimplemented_requirements": {"status": "pass", "findings": []},
    "failure_boundary_compatibility": {"status": "pass", "findings": []},
    "code_quality_module_responsibility": {"status": "pass", "findings": []},
    "abstraction_complexity": {"status": "pass", "findings": []},
    "test_quality_regression_prevention": {"status": "pass", "findings": []}
  }
}
```

Submit:

```bash
python3 "$AGENT_LOOP_ROOT/scripts/assurance_loop.py" submit \
  _workspace/assurance-<slug> \
  --report _workspace/assurance-<slug>/assessment-input.json \
  --project-root "$PROJECT_ROOT" --json
```

- Any failed category with no edit permission returns `changes-requested`.
- Any failed category with edit permission enters Address.
- All categories passing require current 100% Completion and return
  `completed`.

## Address and verify

Address only recorded findings. Preserve failing reproductions and existing
assertions. Enter Verify, run the root scenarios, and return to a complete
six-category reassessment:

```bash
python3 "$AGENT_LOOP_ROOT/scripts/assurance_loop.py" transition \
  _workspace/assurance-<slug> verify \
  --project-root "$PROJECT_ROOT" --json
python3 "$AGENT_LOOP_ROOT/scripts/scenario_gate.py" run \
  --project-root "$PROJECT_ROOT" --json
python3 "$AGENT_LOOP_ROOT/scripts/assurance_loop.py" verify \
  _workspace/assurance-<slug> \
  --project-root "$PROJECT_ROOT" --json
```

Stop at the assigned iteration budget. Main or the standalone owner alone
decides whether to allocate more work.

## Subloop result

Do not activate Design or create a root pointer. Write the six-category
assessment object, then let the Pack prepare a source-bound common result:

```bash
python3 "$AGENT_LOOP_ROOT/scripts/assurance_loop.py" prepare-subloop-result \
  _workspace/<main>/subloops/<invocation-id> \
  --assessment _workspace/<main>/subloops/<invocation-id>/assessment-input.json \
  --project-root "$PROJECT_ROOT" --json
```

The command writes `assurance-report.json` and `result-input.json`. A passing
assessment maps to `completed`; actionable findings map to
`changes-requested`. Evolution Main validates and accepts the result, debits
budget, and decides the next phase.

If the Subloop needs a requirement or authority decision, do not guess or edit
the invocation. Return a parent-consumable `needs-decision` result. Use
`blocked` for unavailable evidence or infrastructure.

## Completion

Subloop verification may run Completion against the parent task without
`--finish`. Only Evolution Main may finish the root Completion. Standalone
Assurance may finish its own root task after a passing assessment.
