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

## Rules and your personal skills

The plugin carries **your** ruleset — `.claude/skill-rules.json` in this repo —
and the verifier hooks point `--rules` at it, so the same enforcement travels
to every project you install the plugin into.

- **The rules reference skills, the host resolves them.** The verifier only
  checks the transcript for whether `Skill(X)` was invoked; it does not load
  the skill. Your personal skills (`coding-quality-guardrails`,
  `diagnosing-bugs`, `flow-design`, …) live in your global skill dirs
  (`~/.claude/skills`, `~/.agents/skills`, `~/.gemini/antigravity-cli/skills`),
  where each host already finds them — so a rule requiring one is satisfiable
  wherever those skills are installed.
- **To wire your own skills:** edit `.claude/skill-rules.json` — add a rule per
  skill (`when` = prompt/tool pattern, `require` = `{"skill": "<name>"}`). The
  bundled `artifact-judge` skill installs with the plugin; the rest resolve
  from your global install.
- **Portability:** the rules file is shared across all three hosts. Keep skill
  names identical to how each host registers them.
- If you also want per-project rules on top of the global set, point `--rules`
  at a project file instead, or ask for merge support (plugin + project).

## Companion skills (for others installing this)

The default `.claude/skill-rules.json` requires skills that live in a separate
collection — **https://github.com/tae2089/skills** — namely
`coding-quality-guardrails`, `diagnosing-bugs`, `writing-great-skills`,
`flow-design`, `execute-dispatch-unit`, `decompose-and-dispatch`, and
`ready-code-review`. Only `artifact-judge` ships inside agent-gate. A rule that
requires a skill the host can't find will block unsatisfiably, so install both:

```
# 1) enforcement (this repo) — see the per-host steps above
# 2) the skills the rules reference — one command, all hosts auto-detected:
npx skills add github.com/tae2089/skills -g          # global (~/<agent>/skills)
# or target hosts / a subset:
npx skills add github.com/tae2089/skills -g -a claude-code codex
npx skills add github.com/tae2089/skills -g -s coding-quality-guardrails diagnosing-bugs
```

`npx skills` (Vercel Labs `skills`) clones the repo, discovers all 14 SKILL.md
skills, and symlinks them into each detected host's skill dir (`~/.claude/skills`,
`~/.codex/skills`, `~/.gemini/antigravity/skills`, …). Omit `-g` to install
project-locally instead. If you only want a subset, use `-s` (or trim the rules).

## Notes / unverified

- The Antigravity plugin script paths use the documented staging directory. If
  a future `agy` build changes it, update the `$HOME/...` prefix in `hooks.json`
  (or replace with a plugin-root variable once one is documented).
- `injectSteps` delivery to the model post-compaction is documented but not
  independently smoke-tested here.
- The repo's workspace configs (`.claude/settings.json`, `.codex/hooks.json`,
  `.agents/hooks.json`) are for dogfooding agent-gate on itself and are separate
  from these plugin manifests.
