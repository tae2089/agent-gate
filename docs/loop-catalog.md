# Loop Pack catalog

A Loop Pack is an iterative work policy with an explicit trigger, state graph,
evidence contract, convergence condition, and bounded terminal policy. A
one-shot validator, hook, or adapter is not a Loop Pack.

## Execution roles

`evolution-loop` is the initial Main Loop. It owns the root request,
requirements, scope, permissions, budgets, phase, one active Subloop, and final
Completion. Specialist Packs declare standalone and Subloop modes. Nested
execution inherits a strict subset of Main authority and returns one of
`completed`, `changes-requested`, `needs-decision`, `blocked`, or
`budget-exhausted`; it cannot dispatch another child or perform external
mutation.

## Implemented

| Pack | Role and modes | Feedback signal | Convergence |
| --- | --- | --- | --- |
| `evolution-loop` | Main; standalone | executable scenarios, accepted Subloop results, and scope evaluation | root Completion is current and the requested deliverable is ready |
| `ci-repair-loop` | specialist; standalone/subloop | repository-native reproductions of requested failing checks | fresh 100% Completion reaches local `checks-green` |
| `assurance-loop` | specialist; standalone/subloop | six source-bound requirements and quality assessments | every assessment passes, or actionable findings return to Main |
| `debug-loop` | specialist; standalone/subloop | reproducible failure and causal evidence | root cause is evidenced and any authorized fix passes regression verification |
| `research-adoption-loop` | specialist; standalone/subloop | eight-axis Requirements Gate, ordinal evidence certainty, repository fit, and a preserved prototype result | a Gate-passed brief records adopt/reject with fresh Completion; only adopt can derive an Evolution candidate |

## Candidates

Candidates do not extend the Loop Engine until they have a concrete user
request and a second demonstrated consumer for any new engine behavior.

| Candidate | Feedback signal | Proposed convergence |
| --- | --- | --- |
| `dependency-upgrade-loop` | compatibility and regression checks | target version is applied without declared regressions |
| `performance-loop` | benchmark and profile evidence | target metric is met within regression limits |
| `security-hardening-loop` | scanner and threat evidence | finding is remediated or a human records the risk decision |
| `documentation-drift-loop` | code, CLI, and example mismatch | documented examples match current observable behavior |

Publication, merge, deploy, issue mutation, and risk acceptance remain explicit
external actions; adding a Loop Pack does not authorize them.
