#!/usr/bin/env python3
"""Stop-hook watermark: when context usage crosses a threshold, block turn end
until the agent writes a handoff artifact.

Detection is deterministic (token usage from the transcript); writing the
handoff is creative work left to the agent. Context size is the last assistant
message's input_tokens + cache_read_input_tokens + cache_creation_input_tokens.

Satisfaction is stateless: any Write call this session whose file_path
contains "handoff" counts. Same fail-open policy as skill_invocation_verifier:
this tool's action is blocking, so its own failure must never lock a session.

Audit mode: `--check <transcript.jsonl>` prints current usage percentage.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from skill_invocation_verifier import parse_transcript, _tool_calls, _note  # noqa: E402

DEFAULT_WINDOW = 200_000
DEFAULT_THRESHOLD = 0.9


def context_tokens(entries: list[dict]) -> int | None:
    last = None
    for entry in entries:
        if entry.get("type") != "assistant":
            continue
        usage = (entry.get("message") or {}).get("usage")
        if isinstance(usage, dict):
            last = usage
    if last is None:
        return None
    return sum(int(last.get(k) or 0) for k in
               ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))


def handoff_written(entries: list[dict]) -> bool:
    for entry in entries:
        for call in _tool_calls(entry):
            if call.name == "Write" and "handoff" in str(call.input.get("file_path", "")).lower():
                return True
    return False


def run_hook(window: int, threshold: float) -> int:
    try:
        hook_input = json.loads(sys.stdin.read())
        if not isinstance(hook_input, dict):
            raise ValueError("hook input is not an object")
    except (ValueError, json.JSONDecodeError) as exc:
        _note(f"unreadable hook input, fail-open: {exc}")
        return 0
    if hook_input.get("stop_hook_active"):
        return 0
    try:
        entries = parse_transcript(Path(hook_input.get("transcript_path")))
    except (OSError, TypeError) as exc:
        _note(f"unreadable transcript, fail-open: {exc}")
        return 0
    tokens = context_tokens(entries)
    if tokens is None:
        return 0
    pct = tokens / window
    if pct < threshold or handoff_written(entries):
        return 0
    reason = (
        f"[agent-gate] Context is {pct:.0%} full ({tokens}/{window} tokens). "
        "Before finishing, write a handoff artifact so work survives compaction: "
        "create _workspace/<current-task>/handoff.md (or handoff.md in cwd if no task folder) "
        "covering: current goal, work completed with file paths, key decisions and why, "
        "verified state (tests/commands run), and exact next steps. Then finish the turn."
    )
    print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))
    return 0


def run_check(window: int, transcript: str) -> int:
    try:
        entries = parse_transcript(Path(transcript))
    except OSError as exc:
        print(f"cannot read transcript: {exc}", file=sys.stderr)
        return 2
    tokens = context_tokens(entries)
    if tokens is None:
        print("no usage data found")
        return 0
    print(f"context: {tokens}/{window} tokens ({tokens / window:.1%}), "
          f"handoff written: {handoff_written(entries)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW, help="context window size in tokens")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help="block threshold ratio")
    parser.add_argument("--check", metavar="TRANSCRIPT", help="report usage instead of running as a hook")
    args = parser.parse_args()
    if args.check:
        return run_check(args.window, args.check)
    return run_hook(args.window, args.threshold)


if __name__ == "__main__":
    sys.exit(main())
