---
name: completion-check
description: Verify an active agent-gate task before reporting implementation work complete after changing project files. Use immediately before the final completion report for implementation, fixes, or refactors that made project edits. Do not use for general conversation, explanation, status-only, planning-only, review-only, or turns with no project edits.
---

# Completion Check

Verify executable evidence at the implementation-completion boundary without
adding a lifecycle hook.

## Procedure

1. Resolve the project root containing `scripts/scenario_gate.py`.
2. Check `_workspace/.active-task`. If it is absent, stop without creating or
   activating a task.
3. Run:

   ```bash
   python3 scripts/scenario_gate.py run --project-root . --json
   ```

4. If the run fails, report the failing scenario and continue implementation.
   Do not weaken the expected result.
5. Run:

   ```bash
   python3 scripts/scenario_gate.py completion --project-root . --finish --json
   ```

6. Report the implementation complete only when completion is current, exactly
   100%, and `--finish` succeeds. Otherwise fix the reported failure and rerun
   the procedure.

## Boundaries

- Treat this skill as prompt guidance, not deterministic enforcement.
- Never add or require a Stop, commit, push, PostToolUse, or verifier hook.
- Never create an active task, scenario contract, runner, or completion policy.
- Leave general conversation and read-only work untouched.
