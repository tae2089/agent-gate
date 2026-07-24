# Loop Pack catalog

A Loop Pack is an iterative work policy with an explicit trigger, state graph,
evidence contract, convergence condition, and bounded terminal policy. A
one-shot validator, hook, or adapter is not a Loop Pack.

## Implemented

| Pack | Feedback signal | Convergence |
| --- | --- | --- |
| `evolution-loop` | executable scenarios plus scope evaluation | verified PR is ready or opened |
| `ci-repair-loop` | repository-native reproductions of requested failing checks | fresh 100% Completion reaches `checks-green` |
| `review-loop` | current source-bound actionable diff findings | no actionable findings remain and Completion is fresh 100% |
| `research-adoption-loop` | cited source evidence plus a preserved, content-bound local prototype result | adopt or reject decision is recorded with fresh 100% Completion |

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
