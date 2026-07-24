---
name: debug-loop
description: Reproduce, diagnose, and optionally repair a concrete failure with evidence-bound root-cause analysis and regression verification. Use for broken behavior, regressions, exceptions, flaky failures, or incorrect runtime results, either standalone or as an Evolution Main Subloop; do not use for broad assurance or CI-provider workflow repair.
---

# Debug Loop

Run `Frame → Reproduce → Diagnose → Fix → Verify` within the declared scope,
permissions, and iteration budget. Preserve the first failing reproduction as
the proof target. Do not guess a root cause from symptoms.

## Boundaries

- Treat the user request or parent invocation as the only authority.
- Do not expand scope, permissions, requirements, or iteration budget.
- Never publish, push, comment, merge, deploy, or call another Subloop.
- Modify the worktree only with `modify-worktree`.
- Return only `completed`, `changes-requested`, `needs-decision`, `blocked`, or
  `budget-exhausted`.

## Resolve execution mode

Resolve `PROJECT_ROOT` as the target Git worktree and `AGENT_LOOP_ROOT` as the
parent of this skill's `skills/` directory.

- **Standalone:** start or resume one direct `_workspace/debug-<slug>` root
  task. It owns `_workspace/.active-run`.
- **Subloop:** use the directory supplied by Evolution Main at
  `_workspace/<main>/subloops/<invocation-id>`. Inherit its requirements,
  scope, source snapshot, permissions, budget, and Completion task. Do not
  create a global pointer or a second Design.

## Standalone workflow

Create `request-input.json`:

```json
{
  "schema_version": 1,
  "source": "manual",
  "source_ref": "conversation:<request-ref>",
  "request": "<verbatim request>",
  "symptom": "<observable failure>",
  "scope": ["src", "tests"],
  "permissions": ["read-repository", "run-local-verification"],
  "evidence": ["reported or observed failure"]
}
```

Add `modify-worktree` only when the user authorized a fix. Start and advance:

```bash
python3 "$AGENT_LOOP_ROOT/scripts/debug_loop.py" start \
  _workspace/debug-<slug> \
  --request _workspace/debug-<slug>/request-input.json \
  --project-root "$PROJECT_ROOT" --max-iterations 3 --json
python3 "$AGENT_LOOP_ROOT/scripts/debug_loop.py" transition \
  _workspace/debug-<slug> reproduce \
  --project-root "$PROJECT_ROOT" --json
python3 "$AGENT_LOOP_ROOT/scripts/debug_loop.py" transition \
  _workspace/debug-<slug> diagnose \
  --project-root "$PROJECT_ROOT" --json
```

Use the smallest deterministic reproduction available. Record a diagnosis only
after the evidence distinguishes the root cause from alternatives:

```json
{
  "schema_version": 1,
  "request_sha256": "<active input hash>",
  "resolution": "fix-required",
  "root_cause": "<causal explanation>",
  "reproduction": ["<failing scenario or command>"],
  "evidence": ["<source, test, or log evidence>"],
  "proposed_fix": "<smallest corrective change>"
}
```

Submit it:

```bash
python3 "$AGENT_LOOP_ROOT/scripts/debug_loop.py" diagnose \
  _workspace/debug-<slug> \
  --diagnosis _workspace/debug-<slug>/diagnosis-input.json \
  --project-root "$PROJECT_ROOT" --json
```

`diagnosed` ends a diagnosis-only run. `fix-required` enters Fix only when edit
authority exists; otherwise it returns a completed diagnosis. Use
`needs-decision` when a user choice changes the fix and `blocked` when required
evidence cannot be obtained.

## Fix and verify

Write the regression test before the production fix. Make the minimum change
that passes it, preserve existing assertions, then run the declared scenarios.
Enter Verify and require the common Completion Gate:

```bash
python3 "$AGENT_LOOP_ROOT/scripts/debug_loop.py" transition \
  _workspace/debug-<slug> verify \
  --project-root "$PROJECT_ROOT" --json
python3 "$AGENT_LOOP_ROOT/scripts/scenario_gate.py" run \
  --project-root "$PROJECT_ROOT" --json
python3 "$AGENT_LOOP_ROOT/scripts/debug_loop.py" complete \
  _workspace/debug-<slug> --project-root "$PROJECT_ROOT" --json
```

If verification fails, return to Fix only while budget remains.

## Subloop workflow

Attach to the immutable parent invocation; the Pack derives its budget from
that artifact and creates no root pointer:

```bash
python3 "$AGENT_LOOP_ROOT/scripts/debug_loop.py" attach-subloop \
  _workspace/<main>/subloops/<invocation-id> \
  --project-root "$PROJECT_ROOT" --json
```

Run the same phases against the inherited scope. When done, prepare the
source-bound common result:

```bash
python3 "$AGENT_LOOP_ROOT/scripts/debug_loop.py" prepare-subloop-result \
  _workspace/<main>/subloops/<invocation-id> \
  --diagnosis _workspace/<main>/subloops/<invocation-id>/diagnosis-input.json \
  --project-root "$PROJECT_ROOT" --json
```

Evolution Main alone accepts `result-input.json`, debits the budget, and
chooses the next Subloop or Main phase. A Subloop may run Completion against
the parent task but must never finish the root task.
