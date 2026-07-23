# agent-gate as a plugin (Claude Code · Codex CLI · Antigravity)

One repo, three host manifests. The hook scripts under `hooks/` and the rules
in `.claude/skill-rules.json` are shared; each host reads its own manifest.

| Host | Manifest | Hooks file | Script root |
|------|----------|-----------|-------------|
| Claude Code | `.claude-plugin/plugin.json` (+ `.claude-plugin/marketplace.json`) | `hooks/hooks.json` | `${CLAUDE_PLUGIN_ROOT}` |
| Codex CLI | `.codex-plugin/plugin.json` | `hooks/hooks.json` | `${CLAUDE_PLUGIN_ROOT}` (Codex compat shim) |
| Antigravity | `plugin.json` (root) | `hooks.json` (root) | `$HOME/.gemini/antigravity-cli/plugins/agent-gate` |

Requires `python3` on PATH. Hooks are stdlib-only.

The core product is the structural Design Gate plus executable Completion
validation. Default manifests wire only Design Gate. The verifier, watermark,
and handoff reinjection remain bundled lifecycle support but require explicit
opt-in; they do not participate in either gate's verdict.

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
Codex exports `CLAUDE_PLUGIN_ROOT` for Claude-plugin compatibility. The shared
`hooks/hooks.json` contains only the Design Gate by default.
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
- Design Gate (PreToolUse) is enabled by default through the
  `antigravity_adapter.py` shim (`write_to_file`→`Write`, `IsSkillFile`→skill).
- Completion is an explicit host-neutral local completion CLI command, not a
  lifecycle hook. CI enforcement exists only when task artifacts are available
  and the command is separately wired by the downstream project.
- verifier and reinject are supported as opt-in hooks via
  `skill_invocation_verifier.py` and `antigravity_reinject.py`.
- watermark — **unsupported**: the Antigravity CLI records no token/usage in
  any readable store, and gating on a char-count estimate would make a
  deterministic gate probabilistic. Reinject covers context-loss recovery.

### Optional lifecycle capability matrix

| Capability | Claude Code | Codex CLI | Antigravity |
|------------|-------------|-----------|-------------|
| Pre-edit Design Gate | native hook | native hook | adapter |
| Explicit local Completion CLI | supported | supported | supported |
| Stop verifier gate | native hook | native hook | adapter |
| Token watermark at Stop | supported | supported | unsupported (no deterministic usage source) |
| PreCompact handoff barrier | native hook | native hook | unavailable |
| Post-compaction reinject | `SessionStart(compact)` | `SessionStart(compact)` | `CHECKPOINT` injection |

The shared semantic verdict for Stop is “continue the agent with corrective
feedback.” Claude and Codex both express it as `decision:block` plus `reason`.
The different PreCompact meaning is “cancel this compaction,” expressed as
`continue:false`. Antigravity translates only the lifecycle events it exposes;
missing capabilities are not approximated.

The matrix and contract tests do not prove that an installed host consumes
lifecycle verdicts end to end.

## Optional lifecycle wiring

The default installation needs no lifecycle configuration beyond Design Gate.
To enforce semantic skill invocation, copy a Stop entry that invokes
`skill_invocation_verifier.py --rules .claude/skill-rules.json`.

For Claude and Codex context preservation, **Enable watermark and reinject
together**: wire `context_watermark.py` to Stop and
`PreCompact(manual|auto)`, then wire `handoff_reinject.py` to
`SessionStart(compact)`. The complete repository-root JSON example is in
`README.md`.

Antigravity has no deterministic watermark source. Its independent opt-in
choices are a Stop verifier through `antigravity_adapter.py` and checkpoint
reinjection through `antigravity_reinject.py`.

After editing hook configuration, reload the plugin or start a new session so
the host does not retain a cached manifest. Codex supports `/reload-plugins`.

## Rules and your personal skills

The plugin carries **your** ruleset — `.claude/skill-rules.json` in this repo.
When the optional verifier hook is enabled, point `--rules` at this file so the
same enforcement travels to every project using that configuration.

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

The two gates are host-neutral. `scenario_gate.py design ... --activate`
records one active task per worktree; PreToolUse checks only its structural
task, flow, and scenario artifacts. Completion is checked explicitly with
`scripts/scenario_gate.py completion --project-root .`, and `--finish` clears
the active task only after a fresh 100 percent result. No semantic readiness
score, session binding, child inheritance, PostToolUse binding, or global
Completion Stop hook participates.

Design Gate observes supported direct-edit events from each host. It is not a
filesystem security sandbox. `_workspace/**`, `.md`, `.rst`, `.txt`, and named
project documents are exempt so task recovery and ordinary documentation do
not require an active design; behavior-bearing Markdown is exempt as well.
Completion freshness covers the whole worktree except `_workspace/**`, so an
unrelated worktree change can make an otherwise passing result stale.

This repository's GitHub workflow tests agent-gate itself; it does not enforce
a task Completion result. Downstream CI must provide the task artifacts and
separately wire the Completion command.

## Bundled skills

`artifact-judge`, `scenario-design`, and `completion-check` ship inside
agent-gate. `completion-check` is implicit prompt guidance for the final report
after implementation edits; it is intentionally absent from Stop hooks and
verifier rules so ordinary conversation remains unaffected. The default rules
require only `artifact-judge`, so installing a separate personal skill collection
is not necessary. A downstream rule that names another skill is satisfiable only
when that host has the named skill installed.

## Notes / unverified

- The Antigravity plugin script paths use the documented staging directory. If
  a future `agy` build changes it, update the `$HOME/...` prefix in `hooks.json`
  (or replace with a plugin-root variable once one is documented).
- `injectSteps` delivery to the model post-compaction is documented but not
  independently smoke-tested here.
- Scenario commands are trusted task artifacts. The plugin
  removes most inherited environment variables and never uses a shell, but it
  does not provide an OS-level network sandbox.
- Every scenario command runs once and records its own exit status. Scenario
  review may assist authoring, but only the fresh executable result
  participates in completion.
- The repo's workspace configs (`.claude/settings.json`, `.codex/hooks.json`,
  `.agents/hooks.json`) are for dogfooding agent-gate on itself and are separate
  from these plugin manifests.
