# agent-gate as a plugin (Claude Code · Codex CLI · Antigravity)

One repo, three host manifests. The hook scripts under `hooks/` and the rules
in `.claude/skill-rules.json` are shared; each host reads its own manifest.

| Host | Manifest | Hooks file | Script root |
|------|----------|-----------|-------------|
| Claude Code | `.claude-plugin/plugin.json` (+ `.claude-plugin/marketplace.json`) | `hooks/hooks.json` | `${CLAUDE_PLUGIN_ROOT}` |
| Codex CLI | `.codex-plugin/plugin.json` | `hooks/hooks.json` (shared) | `${CLAUDE_PLUGIN_ROOT}` (Codex compat shim) |
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
codex_hooks = true
```
Then:
```
codex plugin marketplace add <owner>/agent-gate
codex plugin add agent-gate
/reload-plugins
```
Codex exports `CLAUDE_PLUGIN_ROOT` for Claude-plugin compatibility, so it reads
the same `hooks/hooks.json`. Plugin-bundled hooks are non-managed — Codex will
ask you to review/trust them on first use.

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

## Notes / unverified

- The Antigravity plugin script paths use the documented staging directory. If
  a future `agy` build changes it, update the `$HOME/...` prefix in `hooks.json`
  (or replace with a plugin-root variable once one is documented).
- `injectSteps` delivery to the model post-compaction is documented but not
  independently smoke-tested here.
- The repo's workspace configs (`.claude/settings.json`, `.codex/hooks.json`,
  `.agents/hooks.json`) are for dogfooding agent-gate on itself and are separate
  from these plugin manifests.
