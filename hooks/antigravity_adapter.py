#!/usr/bin/env python3
"""Boundary shim so agent-gate hooks run under Google Antigravity unmodified.

Antigravity command hooks speak a different contract than Claude Code / Codex
(camelCase stdin, and a per-event decision key: PreToolUse blocks with
`{"decision":"deny"}`, Stop prevents termination with `{"decision":"continue"}`
— antigravity.google/docs/hooks). This adapter translates Antigravity stdin
into the Claude schema our hooks expect, runs the underlying hook, and maps its
`{"decision":"block"}` verdict back to the event's Antigravity shape.

Usage (from .agents/hooks.json):
    antigravity_adapter.py --event {pretooluse,stop} -- <hook command...>

Fail-open: any adapter-level failure emits an allow/no-op so a shim bug can
never lock an Antigravity session — the same policy the hooks themselves use.

Antigravity coverage (measured against transcript_full.jsonl, codex-cli-style):
- readiness (Pre/PostToolUse): supported — the tool call is normalized here.
- verifier (Stop): supported — transcript.py normalizes Antigravity lines and
  reads skill use from a view_file with args.IsSkillFile.
- reinject: supported via PreInvocation — see antigravity_reinject.py, which
  injects the handoff once per CHECKPOINT (compaction) using injectSteps.
- watermark (Stop): unsupported. Verified across every Antigravity CLI store:
  the brain transcript (transcriptPath) and the conversations/*.db|*.pb carry
  no token counts, and the only real-token file (~/.gemini/tmp/<slug>/chats/
  *.json, tokens{input,output,cached,total}) is written by the IDE, not the
  CLI. A chars/4 estimate is the only reachable signal, and gating on an
  estimate would make a deterministic gate probabilistic — so it is left
  unwired. Reinject already covers Antigravity's context-loss recovery.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from transcript import antigravity_tool  # noqa: E402


def to_claude_stdin(event_json: dict) -> dict:
    """Map Antigravity's camelCase hook input to the Claude hook schema."""
    workspaces = event_json.get("workspacePaths")
    cwd = workspaces[0] if isinstance(workspaces, list) and workspaces else None
    claude = {
        "transcript_path": event_json.get("transcriptPath"),
        "cwd": cwd,
        "session_id": event_json.get("conversationId"),
        "stop_hook_active": False,
    }
    tool_call = event_json.get("toolCall")
    if isinstance(tool_call, dict) and isinstance(tool_call.get("name"), str):
        args = tool_call.get("args") if isinstance(tool_call.get("args"), dict) else {}
        claude["tool_name"], claude["tool_input"] = antigravity_tool(tool_call["name"], args)
    return claude


def underlying_blocked(stdout: str) -> tuple[bool, str | None]:
    """Read a blocking Claude hook verdict from an underlying hook's stdout."""
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            verdict = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(verdict, dict):
            continue
        if verdict.get("decision") == "block":
            reason = verdict.get("reason")
            return True, reason if isinstance(reason, str) else None
        output = verdict.get("hookSpecificOutput")
        if isinstance(output, dict) and output.get("permissionDecision") == "deny":
            reason = output.get("permissionDecisionReason")
            return True, reason if isinstance(reason, str) else None
    return False, None


def emit(event: str, blocked: bool, reason: str | None) -> None:
    """Print the Antigravity verdict for this event (docs/hooks contract)."""
    if event == "pretooluse":
        if blocked:
            print(json.dumps({"decision": "deny", "reason": reason or "blocked by agent-gate"}))
        else:
            print(json.dumps({"decision": "allow"}))
    elif event == "stop":
        if blocked:
            print(json.dumps({"decision": "continue", "reason": reason or "blocked by agent-gate"}))
        else:
            print("{}")
    elif event == "posttooluse":
        # PostToolUse cannot block (docs); the hook still ran (e.g. bind).
        print("{}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", required=True, choices=("pretooluse", "posttooluse", "stop"))
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="-- followed by the underlying hook command")
    args = parser.parse_args()
    command = args.command[1:] if args.command and args.command[0] == "--" else args.command

    try:
        event_json = json.loads(sys.stdin.read())
        if not isinstance(event_json, dict) or not command:
            raise ValueError("bad adapter input")
        proc = subprocess.run(command, input=json.dumps(to_claude_stdin(event_json)),
                              capture_output=True, text=True, timeout=55)
        blocked, reason = underlying_blocked(proc.stdout)
    except Exception as exc:  # fail open: never lock the session on a shim bug
        print(f"[antigravity-adapter] fail-open: {type(exc).__name__}", file=sys.stderr)
        blocked, reason = False, None
    emit(args.event, blocked, reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
