# ADR 0001: Model the product as Agent Loop with reusable gates

## Status

Accepted

## Context

The product began as `agent-gate`, centered on structural Design and executable
Completion gates. A proven `evolution-loop` later added a bounded
`Interview → Seed → Execute → Evaluate` lifecycle, while context preservation
and artifact validation added supporting capabilities.

The product now needs multiple feedback loops without hiding their different
inputs, convergence rules, or risks behind a universal workflow abstraction.
The existing evolution state machine also contains transition and iteration
mechanics that a second concrete loop can reuse.

Canonical terms:

- **Agent Loop**: the product and plugin.
- **Loop Engine**: deterministic transition and iteration mechanics.
- **Loop Pack**: one concrete lifecycle and its artifacts.
- **Gate**: a reusable evidence-based transition guard.
- **Lifecycle support**: hooks such as context watermark and reinjection that
  are not themselves Loop Packs.

## Decision

Rename tracked public product identifiers to `agent-loop` and make the existing
gates primitives within that platform.

Extract a small typed Loop Engine from demonstrated evolution mechanics. Keep
pack-owned schemas, evaluation policy, CLI commands, and external adapters out
of the engine. Add `ci-repair-loop` as the second consumer: it iterates through
Inspect, Repair, and Verify and reaches `checks-green` only through current
100% Completion evidence.

Do not rename the Gate modules or scenario artifacts; their concepts remain
valid. Do not rename the remote repository or local checkout as part of this
code change.

## Alternatives Considered

- Keep `agent-gate`: preserves installation identity but understates the
  product after multiple executable feedback loops.
- Rename only: creates a broader label without a reusable engine or second
  consumer.
- Introduce a JSON/YAML workflow DSL, registry, scheduler, or provider
  interface: maximizes configurability before any current consumer requires it.
- Turn every validator and lifecycle hook into a Loop Pack: blurs the
  distinction between iterative work, transition guards, and support services.

## Consequences

- Evolution and CI repair share deterministic transition and budget behavior
  while retaining explicit domain contracts.
- Design and Completion remain independently reusable Gates.
- Plugin names, Antigravity install paths, hook namespaces, diagnostics, docs,
  and skill runtime variables change to `agent-loop`/`AGENT_LOOP_ROOT`.
- Existing installations must reinstall or update their plugin reference; no
  compatibility alias is added because no demonstrated consumer requires two
  simultaneously published identities.
- Future packs must demonstrate a real feedback signal, convergence condition,
  iteration budget, and terminal policy before extending the engine.
