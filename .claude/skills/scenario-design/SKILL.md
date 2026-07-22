---
name: scenario-design
description: Create or refine a Full parent's observable scenario-contract.json from task acceptance criteria and numbered flow pseudocode, with atomic observation IDs and exclusive executable checks.
---

# Scenario Design

Create the smallest executable behavior contract that covers the declared flow. Do not design production code, child-local completion boundaries, or a universal test framework.

## Procedure

1. Resolve the direct Full task and `.agent-gate/scenario-gate.json`. If the active task is inherited, use its `parent_task`; the child never owns a scenario contract. Read only the Full task's `task.md` Contract/AC and `implementation.md` Pseudocode. Done when every exact source path is known.
2. Use the `flow-design` skill if available to close missing branch and side-effect failure arms before extracting scenarios. Do not turn an unhandled arm into intended behavior. Done when every scenario points to an observable terminal.
3. Build the smallest arm-covering scenario set. Each scenario has one event, 3-5 Given/When/Then steps total where practical, and asserts public output, persisted state, or emitted events rather than functions, mocks, or call order.
4. Split every Then into atomic `{id, expectation}` observations. Use stable unique `O-*` IDs. Each scenario names one configured runner, and no runner name may be shared by another scenario.
5. Write the strict Full-parent `scenario-contract.json`. When a child discovers a missing behavior, update the parent contract before further implementation; never create `scenario-overlay.json`, ownership, or parent-candidate state.
6. Run `python3 scripts/scenario_gate.py readiness <full-task-dir> --project-root <root>`. Fix schema, AC/P reference, duplicate observation, or exclusive-runner errors. Done when the command exits 0.
7. After implementation, use the `artifact-judge` skill's Scenario Evidence Procedure. Scenario readiness authorizes implementation; only current evidence plus execution can authorize completion.

## Boundaries

- Do not create one scenario per P line, function, class, or mock interaction.
- Do not change an expected outcome to match an implementation failure.
- Do not create child-local scenario artifacts or downgrade a Full completion boundary after decomposition.
- Do not ask an LLM to judge runner commands. Repository runner configuration is the declared trust boundary.
- Keep successful runner logs out of model context; inspect only failed execution evidence.

## Contract Shape

```json
{
  "schema_version": 1,
  "scenarios": [
    {
      "id": "S-EXAMPLE",
      "title": "Observable example",
      "covers": {"acceptance": ["AC-1"], "flow": ["P1"]},
      "runner": "example-check",
      "given": ["an observable initial state"],
      "when": ["a public action occurs"],
      "then": [
        {"id": "O-EXAMPLE-RESULT", "expectation": "the public result is visible"}
      ]
    }
  ]
}
```

## Completion Criteria

- Every Full-task AC is covered and every referenced P/AC exists.
- Every scenario has observable outcomes with unique atomic observation IDs.
- Every scenario names an exclusive configured runner.
- No risk, level, ownership, parent-candidate, overlay, or runner-review metadata is introduced.
- Scenario readiness exits 0 before protected implementation begins.
