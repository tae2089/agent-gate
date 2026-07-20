#!/usr/bin/env python3
"""Stop-hook verifier: block turn end when routing rules required a Skill/MCP
call that was never made.

Hook mode (default): reads Claude Code Stop-hook JSON on stdin, prints
`{"decision": "block", "reason": ...}` on violation, prints nothing otherwise.
Every internal failure is fail-open (exit 0 + stderr note): the action of this
tool is blocking, so a verifier bug must never lock a session.

Audit mode: `--check <transcript.jsonl>` prints a violation report and exits 1
when violations exist. Use it for post-hoc or CI auditing, which also covers
the blind spot fail-open creates.

Rule file schema (JSON):
    {"rules": [{
        "id": "code-edits-need-guardrails",
        "when": {"prompt_pattern": "...", "tool": "Write|Edit", "input_pattern": "\\.go"},
        "require": {"skill": "coding-quality-guardrails"},   # or {"tool_pattern": "^mcp__context7__"}
        "message": "optional extra guidance"
    }]}

`when` conditions are AND-ed. Trigger scope is the current turn (after the
last real user prompt); satisfaction scope is the whole session, because an
invoked skill's instructions stay in context for the rest of the session.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ToolCall:
    name: str
    input: dict


@dataclass
class Violation:
    rule_id: str
    guidance: str


def _note(msg: str) -> None:
    print(f"[skill-invocation-verifier] {msg}", file=sys.stderr)


def parse_transcript(path: Path) -> list[dict]:
    entries = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue  # partial line from a concurrent append
            if isinstance(entry, dict):
                entries.append(entry)
    return entries


def _prompt_text(entry: dict) -> str | None:
    """Text of a real user prompt; None for tool_result carriers and sidechains."""
    if entry.get("type") != "user" or entry.get("isSidechain") is True:
        return None
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return content if content.strip() else None
    if isinstance(content, list):
        texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        joined = "\n".join(t for t in texts if t)
        return joined if joined.strip() else None
    return None


def _tool_calls(entry: dict) -> list[ToolCall]:
    if entry.get("type") != "assistant" or entry.get("isSidechain") is True:
        return []
    content = (entry.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    calls = []
    for c in content:
        if isinstance(c, dict) and c.get("type") == "tool_use" and isinstance(c.get("name"), str):
            calls.append(ToolCall(name=c["name"], input=c.get("input") or {}))
    return calls


def collect(entries: list[dict]) -> tuple[str | None, list[ToolCall], list[ToolCall]]:
    """Return (last prompt text, current-turn calls, whole-session calls)."""
    last_prompt_idx = None
    last_prompt_text = None
    for i, entry in enumerate(entries):
        text = _prompt_text(entry)
        if text is not None:
            last_prompt_idx, last_prompt_text = i, text
    session_calls: list[ToolCall] = []
    turn_calls: list[ToolCall] = []
    for i, entry in enumerate(entries):
        calls = _tool_calls(entry)
        session_calls.extend(calls)
        if last_prompt_idx is not None and i > last_prompt_idx:
            turn_calls.extend(calls)
    return last_prompt_text, turn_calls, session_calls


def _triggered(rule: dict, prompt: str, turn_calls: list[ToolCall]) -> bool:
    when = rule.get("when") or {}
    conditions = {k: when[k] for k in ("prompt_pattern", "tool", "input_pattern") if k in when}
    if not conditions:
        raise ValueError("rule has no when-conditions")
    if "prompt_pattern" in conditions and not re.search(conditions["prompt_pattern"], prompt):
        return False
    if "tool" in conditions or "input_pattern" in conditions:
        tool_re = conditions.get("tool")
        input_re = conditions.get("input_pattern")
        for call in turn_calls:
            if tool_re and not re.fullmatch(tool_re, call.name):
                continue
            if input_re and not re.search(input_re, json.dumps(call.input, ensure_ascii=False)):
                continue
            return True
        return False
    return True


def _satisfied(rule: dict, session_calls: list[ToolCall]) -> bool:
    require = rule.get("require") or {}
    if "skill" in require:
        return any(c.name == "Skill" and c.input.get("skill") == require["skill"] for c in session_calls)
    if "tool_pattern" in require:
        return any(re.search(require["tool_pattern"], c.name) for c in session_calls)
    raise ValueError("rule has no require target")


def evaluate(rules: list[dict], prompt: str | None, turn_calls: list[ToolCall],
             session_calls: list[ToolCall]) -> list[Violation]:
    if prompt is None:
        return []
    violations = []
    for rule in rules:
        rule_id = str(rule.get("id", "unnamed-rule"))
        try:
            if not _triggered(rule, prompt, turn_calls):
                continue
            if _satisfied(rule, session_calls):
                continue
        except (ValueError, re.error) as exc:
            _note(f"skipping rule '{rule_id}': {exc}")
            continue
        require = rule.get("require") or {}
        if "skill" in require:
            guidance = f"invoke Skill({require['skill']})"
        else:
            guidance = f"call a tool matching {require['tool_pattern']!r}"
        if rule.get("message"):
            guidance += f" — {rule['message']}"
        violations.append(Violation(rule_id=rule_id, guidance=guidance))
    return violations


def load_rules(rules_arg: str | None, cwd: str | None) -> list[dict] | None:
    candidates = []
    if rules_arg:
        candidates.append(Path(rules_arg))
    if cwd:
        candidates.append(Path(cwd) / ".claude" / "skill-rules.json")
    for path in candidates:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            rules = data.get("rules")
            if isinstance(rules, list):
                return rules
            _note(f"{path}: no 'rules' list")
        except (OSError, json.JSONDecodeError) as exc:
            _note(f"cannot load rules from {path}: {exc}")
        return None
    return None


def run_hook(rules_arg: str | None) -> int:
    try:
        hook_input = json.loads(sys.stdin.read())
        if not isinstance(hook_input, dict):
            raise ValueError("hook input is not an object")
    except (ValueError, json.JSONDecodeError) as exc:
        _note(f"unreadable hook input, fail-open: {exc}")
        return 0
    if hook_input.get("stop_hook_active"):
        return 0  # already continuing because of a stop hook; never loop
    rules = load_rules(rules_arg, hook_input.get("cwd"))
    if not rules:
        return 0
    transcript_path = hook_input.get("transcript_path")
    try:
        entries = parse_transcript(Path(transcript_path))
    except (OSError, TypeError) as exc:
        _note(f"unreadable transcript, fail-open: {exc}")
        return 0
    violations = evaluate(rules, *collect(entries))
    if not violations:
        return 0
    lines = [f"rule '{v.rule_id}': {v.guidance}" for v in violations]
    reason = (
        "[agent-gate] Required skill/tool calls are missing for this turn: "
        + "; ".join(lines)
        + ". Invoke them now, apply their instructions to the work just done, then finish the turn."
    )
    print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))
    return 0


def run_check(rules_arg: str | None, transcript: str) -> int:
    rules = load_rules(rules_arg, None)
    if not rules:
        print("no rules loaded", file=sys.stderr)
        return 2
    try:
        entries = parse_transcript(Path(transcript))
    except OSError as exc:
        print(f"cannot read transcript: {exc}", file=sys.stderr)
        return 2
    violations = evaluate(rules, *collect(entries))
    if not violations:
        print("OK: no violations")
        return 0
    for v in violations:
        print(f"VIOLATION {v.rule_id}: {v.guidance}")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rules", help="rules JSON path (default: {cwd}/.claude/skill-rules.json)")
    parser.add_argument("--check", metavar="TRANSCRIPT", help="audit a transcript instead of running as a hook")
    args = parser.parse_args()
    if args.check:
        return run_check(args.rules, args.check)
    return run_hook(args.rules)


if __name__ == "__main__":
    sys.exit(main())
