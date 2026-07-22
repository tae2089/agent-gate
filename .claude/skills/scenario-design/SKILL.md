---
name: scenario-design
description: Create or refine observable scenario-contract.json and child scenario-overlay.json artifacts from task acceptance criteria and numbered flow pseudocode. Use before behavioral Full implementation, when a child discovers a missing case, or when classifying a scenario as child-owned versus a parent promotion candidate.
---

# Scenario Design

Create the smallest executable behavior contract that covers the declared flow. Do not design production code or a universal test framework.

## Procedure

1. Resolve the exact task directory. For a parent read only `task.md` Contract/AC and `implementation.md` Pseudocode; for a child also read `inherited-readiness.json`, the parent's scenario titles, and the assigned P/AC scope. Done when every source path is known.
2. Use the `flow-design` skill if available to close missing branch and side-effect failure arms before extracting scenarios. Do not turn an unhandled arm into intended behavior. Done when every scenario points to an observable terminal.
3. Build the smallest arm-covering set. Each scenario has one event, 3-5 Given/When/Then steps total where practical, and asserts public output, persisted state, or emitted events rather than functions, mocks, or call order. Add extra cases only for later failure after an earlier side effect, concurrency, repeated calls, authorization, or data integrity.
4. For a parent write strict `scenario-contract.json`. For a child inherit only real parent IDs and write new cases to `scenario-overlay.json`; set `ownership` to `parent-candidate` when the behavior survives replacing the child implementation, can affect siblings, is visible at the parent boundary, or protects security/data integrity. Otherwise use `child`.
5. Run `python3 scripts/scenario_gate.py review-template <task-dir> --project-root <root>`. Fix schema, AC/P scope, runner, duplicate, and hash errors; do not edit generated hashes. Done when a review template is emitted.
6. Use the `artifact-judge` skill's scenario review procedure. Do not implement protected code until `python3 scripts/scenario_gate.py readiness <task-dir> --project-root <root>` exits 0.

## Boundaries

- Do not create one scenario per P line, function, class, or mock interaction.
- Do not change an expected outcome to match an implementation failure.
- Do not modify or delete inherited parent scenarios from a child.
- Do not auto-promote a parent candidate; record it for independent review.
- Keep successful runner logs out of the model context; inspect only failed scenario evidence.

## Completion Criteria

- Every task AC is covered and every referenced P/AC exists in the assigned scope.
- Every scenario has a configured runner, observable Then, and no implementation identifiers.
- The parent contract or child overlay passes deterministic template generation.
- `scenario-review.json` is independently produced and current.
- Scenario readiness exits 0 before implementation begins.
