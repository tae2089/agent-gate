# agent-gate as a plugin (Claude Code · Codex CLI · Antigravity)

One repo, three host manifests. The hook scripts under `hooks/` and the rules
in `.claude/skill-rules.json` are shared; each host reads its own manifest.

| Host | Manifest | Hooks file | Script root |
|------|----------|-----------|-------------|
| Claude Code | `.claude-plugin/plugin.json` (+ `.claude-plugin/marketplace.json`) | `hooks/hooks.json` | `${CLAUDE_PLUGIN_ROOT}` |
| Codex CLI | `.codex-plugin/plugin.json` | `hooks/hooks.json` + `hooks/codex-hooks.json` | `${CLAUDE_PLUGIN_ROOT}` (Codex compat shim) |
| Antigravity | `plugin.json` (root) | `hooks.json` (root) | `$HOME/.gemini/antigravity-cli/plugins/agent-gate` |

Requires `python3` on PATH. Hooks are stdlib-only.

## Claude Code

```
/plugin marketplace add <owner>/agent-gate      # or: /plugin marketplace add /path/to/agent-gate
/plugin install agent-gate@agent-gate
```
Verify: `claude plugin validate .` (from the repo). Hooks resolve their scripts
through `${CLAUDE_PLUGIN_ROOT}`.

## Codex CLI

Enable hooks once in `~/.codex/config.toml`:
```toml
[features]
hooks = true
```
Then:
```
codex plugin marketplace add <owner>/agent-gate
codex plugin add agent-gate
/reload-plugins
```
Codex exports `CLAUDE_PLUGIN_ROOT` for Claude-plugin compatibility. Shared
lifecycle hooks stay in `hooks/hooks.json`; the supplemental
`hooks/codex-hooks.json` adds the `PreCompact(manual|auto)` handoff barrier.
Plugin-bundled hooks are non-managed — Codex will ask you to review/trust the
exact definitions on first use and again after they change.

## Antigravity (`agy`)

```
agy plugin install /path/to/agent-gate
agy plugin list        # confirm "agent-gate" enabled
```
Antigravity stages the plugin to `~/.gemini/antigravity-cli/plugins/agent-gate/`,
which the root `hooks.json` references by absolute path (`$HOME/...`) — the
staging location is fixed, so no plugin-root variable is needed.

### Antigravity coverage (by design, not omission)
- readiness gate (Pre/PostToolUse) and verifier (Stop) — supported via the
  `antigravity_adapter.py` shim (`write_to_file`→`Write`, `IsSkillFile`→skill).
- reinject — supported via `PreInvocation` + `injectSteps` on each `CHECKPOINT`
  (compaction).
- watermark — **unsupported**: the Antigravity CLI records no token/usage in
  any readable store, and gating on a char-count estimate would make a
  deterministic gate probabilistic. Reinject covers context-loss recovery.

## Rules and your personal skills

The plugin carries **your** ruleset — `.claude/skill-rules.json` in this repo —
and the verifier hooks point `--rules` at it, so the same enforcement travels
to every project you install the plugin into.

- **The rules reference skills, the host resolves them.** The verifier only
  checks the transcript for whether `Skill(X)` was invoked; it does not load
  the skill. The default policy references only the bundled `artifact-judge`.
- **To wire your own skills:** edit `.claude/skill-rules.json` — add a rule per
  skill (`when` = prompt/tool pattern, `require` = `{"skill": "<name>"}`). Add
  process rules only when an existing artifact or result validator cannot own
  the invariant. The bundled `artifact-judge` skill installs with the plugin.
- **Portability:** the rules file is shared across all three hosts. Keep skill
  names identical to how each host registers them.
- If you also want per-project rules on top of the global set, point `--rules`
  at a project file instead, or ask for merge support (plugin + project).

Readiness and scenario inheritance are host-neutral: a decomposed child writes
`_workspace/<child>/inherited-readiness.json` referencing a ready direct Full
parent and its P/AC scope. The same PostToolUse/PreToolUse hooks bind and
revalidate it on Claude Code, Codex, and Antigravity; unit size never creates a
Fast source-edit bypass. When the target repository opts in with
`.agent-gate/scenario-gate.json`, every child keeps the direct Full parent's
scenario contract and execution result as its completion boundary. The Stop
hook requires 100% scenario trace completeness and every
exclusive scenario check to pass. If a host cannot enforce Stop reliably, run
`scripts/scenario_gate.py completion` as the CI merge gate.

## Bundled skills

`artifact-judge` and `scenario-design` ship inside agent-gate. The default rules
require only `artifact-judge`, so installing a separate personal skill collection
is not necessary. A downstream rule that names another skill is satisfiable only
when that host has the named skill installed.

## Notes / unverified

- The Antigravity plugin script paths use the documented staging directory. If
  a future `agy` build changes it, update the `$HOME/...` prefix in `hooks.json`
  (or replace with a plugin-root variable once one is documented).
- `injectSteps` delivery to the model post-compaction is documented but not
  independently smoke-tested here.
- Scenario runner commands are trusted repository configuration. The plugin
  removes most inherited environment variables and never uses a shell, but it
  does not provide an OS-level network sandbox.
- Every scenario requires an exclusive runner, so one process result is never
  copied across scenario IDs. Scenario review may assist authoring, but only
  the fresh executable result participates in completion.
- The repo's workspace configs (`.claude/settings.json`, `.codex/hooks.json`,
  `.agents/hooks.json`) are for dogfooding agent-gate on itself and are separate
  from these plugin manifests.
