---
name: scenario-design
description: Create or refine a task's executable scenario-contract.json from its contract and numbered flow, with plain observable expectations and direct argv checks.
---

# Scenario Design

Create the smallest executable behavior contract that covers the declared
flow. Do not design production code or a universal test framework.

## Procedure

1. Resolve one direct `_workspace/<task>` containing `task.md` and
   `implementation.md`. Read the Contract/AC and numbered Pseudocode.
2. Use `flow-design` to close missing branch and side-effect failure arms.
   Never turn an unhandled arm into intended behavior.
3. Extract the smallest arm-covering scenario set. Each scenario has one
   event, 3–5 Given/When/Then steps where practical, and asserts a public
   output, persisted state, or emitted event.
4. Write every Then as plain observable expectations. Give each scenario one
   direct non-shell `"command"` argv array.
5. Write the strict `scenario-contract.json` and run:

   ```bash
   python3 scripts/scenario_gate.py design _workspace/<task> \
     --project-root . --activate
   ```

6. After implementation, run `scenario_gate.py run --project-root .`, then
   `scenario_gate.py completion --project-root . --finish`. Fix failed or stale
   checks until completion is current, reports 100%, and clears the active task.

## Contract shape

```json
{
  "schema_version": 1,
  "scenarios": [
    {
      "id": "S-EXAMPLE",
      "title": "Observable example",
      "command": ["python3", "-m", "unittest", "tests.test_example"],
      "given": ["an observable initial state"],
      "when": ["a public action occurs"],
      "then": ["the public result is visible"]
    }
  ]
}
```

## Boundaries

- Do not create one scenario per P line, function, class, or mock interaction.
- Do not change expected outcomes to match implementation failures.
- Commands are argv arrays; never add shell parsing.
- Completion is explicit CLI/CI work; never wire the worktree-level active task
  to a global Stop hook.
- Successful runner logs stay out of model context.
- Scenario review is authoring help. Fresh executable results own completion.

## Completion Criteria

- The task, numbered flow, and at least one scenario are structurally valid.
- Every scenario has non-empty Given, When, Then, and `"command"` arrays.
- Design validation succeeds with `--activate`.
- Current scenario checks pass and `completion --finish` reports 100%.
