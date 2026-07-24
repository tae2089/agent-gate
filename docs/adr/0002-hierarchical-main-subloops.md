# ADR 0002: Use one Main Loop with bounded specialist Subloops

## Status

Accepted

## Context

Independent Loop Pack state machines allow several root runs to compete for
the same user goal, scope, authority, budget, and Completion decision. The
former review-loop also described a finding iteration mechanism without
owning the broader requirements and regression assurance expected from review.

Design and Completion are reusable one-shot validators, not iterative work
policies. A general workflow DSL would hide the Pack-specific evidence and
termination rules before the hierarchy has demonstrated a need for such an
abstraction.

## Decision

Use `evolution-loop` as the initial Main Loop. Main exclusively owns the user
goal, root requirements, scope, permissions, budget ledger, phase, one active
Subloop, result ingestion, and final Completion.

Support four specialist Packs in both standalone and Subloop modes:

- `assurance-loop`;
- `debug-loop`;
- `research-adoption-loop`;
- `ci-repair-loop`.

Each nested invocation binds the parent state hash, source snapshot,
requirements, scope, permissions, budget, and parent Completion task. A child
cannot expand those values, call another child, or receive push, publish,
merge, or deploy authority. It returns exactly one common status:
`completed`, `changes-requested`, `needs-decision`, `blocked`, or
`budget-exhausted`. Main alone validates that result and decides the next
action.

Replace review-loop with assurance-loop without a compatibility alias.
Assurance evaluates requirements conformance, missing or excessive behavior,
failure and compatibility cases, module quality, unnecessary complexity, and
test regression protection.

Use one root execution pointer, `_workspace/.active-run`. Store children only
under `_workspace/<main>/subloops/<invocation-id>/`; nested executions do not
own a global pointer or a second Design.

Keep Design Gate and Completion Gate as validators. A code-changing Subloop may
validate Completion against the parent task, but only Main may finish it.

## Alternatives considered

- Independent root Pack machines: leaves goal, authority, and Completion
  ownership ambiguous.
- Let Subloops dispatch other Subloops: creates recursive budget and authority
  propagation before a demonstrated use case exists.
- Preserve review-loop as an alias: retains terminology and state that no
  current consumer requires.
- Introduce a workflow DSL, registry, or scheduler: generalizes Pack internals
  before the small parent-child contract proves insufficient.

## Consequences

- Root ownership and external-action authority have one deterministic owner.
- Specialist phase graphs remain explicit and independently testable.
- Standalone Packs remain useful for an existing diff, failure, CI run, or
  adoption question.
- Existing pack-specific active pointers are replaced without compatibility
  branches.
- Main is responsible for interpreting child statuses and allocating any
  remaining budget.
