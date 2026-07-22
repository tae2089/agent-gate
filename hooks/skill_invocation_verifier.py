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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from transcript import (  # noqa: E402
    ToolCall,
    last_prompt_index,
    note,
    parse_transcript,
    prompt_text,
    read_hook_input,
    run_fail_open,
    tool_calls,
)

LABEL = "skill-invocation-verifier"


@dataclass
class Violation:
    rule_id: str
    guidance: str


def collect(entries: list[dict]) -> tuple[str | None, list[ToolCall], list[ToolCall]]:
    """Return (last prompt text, current-turn calls, whole-session calls)."""
    last_prompt_idx = last_prompt_index(entries)
    last_prompt_text = prompt_text(entries[last_prompt_idx]) if last_prompt_idx is not None else None
    session_calls: list[ToolCall] = []
    turn_calls: list[ToolCall] = []
    for i, entry in enumerate(entries):
        calls = tool_calls(entry)
        session_calls.extend(calls)
        if last_prompt_idx is not None and i > last_prompt_idx:
            turn_calls.extend(calls)
    return last_prompt_text, turn_calls, session_calls


def _triggered(rule: dict, prompt: str, turn_calls: list[ToolCall]) -> bool:
    when = rule.get("when") or {}
    if not isinstance(when, dict):
        raise ValueError("rule 'when' must be an object")
    conditions = {k: when[k] for k in ("prompt_pattern", "tool", "input_pattern") if k in when}
    if not conditions:
        raise ValueError("rule has no when-conditions")
    if any(not isinstance(pattern, str) for pattern in conditions.values()):
        raise ValueError("rule patterns must be strings")
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
    if not isinstance(require, dict):
        raise ValueError("rule 'require' must be an object")
    if "skill" in require:
        if not isinstance(require["skill"], str):
            raise ValueError("required skill must be a string")
        return any(c.name == "Skill" and c.input.get("skill") == require["skill"] for c in session_calls)
    if "tool_pattern" in require:
        if not isinstance(require["tool_pattern"], str):
            raise ValueError("required tool pattern must be a string")
        return any(re.search(require["tool_pattern"], c.name) for c in session_calls)
    raise ValueError("rule has no require target")


def evaluate(rules: list[dict], prompt: str | None, turn_calls: list[ToolCall],
             session_calls: list[ToolCall]) -> list[Violation]:
    if prompt is None:
        return []
    violations = []
    for rule in rules:
        if not isinstance(rule, dict):
            note(LABEL, "skipping non-object rule")
            continue
        rule_id = str(rule.get("id", "unnamed-rule"))
        try:
            if not _triggered(rule, prompt, turn_calls):
                continue
            if _satisfied(rule, session_calls):
                continue
        except (TypeError, ValueError, re.error) as exc:
            note(LABEL, f"skipping rule '{rule_id}': {exc}")
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


def evaluate_transcript(rules: list[dict], entries: list[dict]) -> list[Violation]:
    """Single composition of the verifier pipeline, shared by the hook, the
    audit --check mode, and the replay harness so all three run the same path."""
    return evaluate(rules, *collect(entries))


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
            rules = data.get("rules") if isinstance(data, dict) else None
            if isinstance(rules, list):
                return rules
            note(LABEL, f"{path}: no 'rules' list")
        except (OSError, json.JSONDecodeError) as exc:
            note(LABEL, f"cannot load rules from {path}: {exc}")
        return None
    return None


def run_hook(rules_arg: str | None) -> int:
    hook_input = read_hook_input(LABEL)
    if hook_input is None:
        return 0
    return run_fail_open(LABEL, lambda: _run_hook(hook_input, rules_arg))


def _run_hook(hook_input: dict, rules_arg: str | None) -> int:
    rules = load_rules(rules_arg, hook_input.get("cwd"))
    if not rules:
        return 0
    entries = parse_transcript(Path(hook_input.get("transcript_path")))
    violations = evaluate_transcript(rules, entries)
    if not violations:
        return 0
    lines = [f"rule '{v.rule_id}': {v.guidance}" for v in violations]
    reason = (
        "[agent-gate] This turn required skill/tool calls that are missing: "
        + "; ".join(lines)
        + ". To finish the turn, invoke them now and apply their instructions to the work just done."
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
    violations = evaluate_transcript(rules, entries)
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
