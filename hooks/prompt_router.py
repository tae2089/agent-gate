#!/usr/bin/env python3
"""UserPromptSubmit hook: proactively hint which skill a prompt should use.

The Stop-gate verifier blocks a turn that skipped a required skill — reactive.
This hook is the proactive complement: when the just-submitted prompt matches a
routing rule's prompt_pattern, it injects a one-line reminder to invoke that
skill, so the model complies on the first turn instead of being blocked and
retrying. It is an OPTIMIZATION, not the guarantee — the deterministic gate
still enforces. Only prompt-pattern rules requiring a skill are hinted; rules
that hinge on a tool call are unknowable at prompt-submit time and are skipped.

For UserPromptSubmit, plain stdout is added to the turn's context.
Fail-open: any error prints nothing (exit 0).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from skill_invocation_verifier import load_rules, routed_skill_hints  # noqa: E402
from transcript import read_hook_input, run_fail_open  # noqa: E402

LABEL = "prompt-router"


def _run_hook(hook_input: dict, rules_arg: str | None) -> int:
    prompt = hook_input.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return 0
    rules = load_rules(rules_arg, hook_input.get("cwd"))
    if not rules:
        return 0
    hints = routed_skill_hints(rules, prompt)
    if not hints:
        return 0
    listed = "; ".join(f"Skill({skill}) (rule '{rule_id}')" for rule_id, skill in hints)
    print(
        "[agent-gate] This prompt matches routing rules — invoke now and apply: "
        + listed
        + ". The Stop gate will require them before the turn can end."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rules", help="rules JSON path (default: {cwd}/.claude/skill-rules.json)")
    args = parser.parse_args()
    hook_input = read_hook_input(LABEL)
    if hook_input is None:
        return 0
    return run_fail_open(LABEL, lambda: _run_hook(hook_input, args.rules))


if __name__ == "__main__":
    sys.exit(main())
