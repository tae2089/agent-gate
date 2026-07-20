#!/usr/bin/env python3
"""Shared transcript parsing and hook I/O boundary for agent-gate hooks.

Transcript entries are Claude Code JSONL lines, or Codex CLI rollout lines
({payload, timestamp, type} envelopes) which parse_transcript normalizes into
Claude-shaped entries so every helper and hook works unchanged. All helpers
ignore sidechain entries and tolerate malformed shapes, because hooks must
fail open.

Normalization contract beyond the Claude schema:
- Synthetic tool_use blocks (Skill from SKILL.md reads, Write/Edit from
  apply_patch hunks and handoff paths) reuse their parent call's id, so their
  success is inherited from the parent's tool output.
- A normalized usage dict may carry "model_context_window" when the source
  transcript reports one; context_window() exposes it.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ToolCall:
    name: str
    input: dict
    tool_use_id: str | None = None


def note(label: str, msg: str) -> None:
    print(f"[{label}] {msg}", file=sys.stderr)


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
                entries.append(_normalize_codex(entry))
    return entries


# Codex has no Skill tool; agents apply a skill by shell-reading its SKILL.md,
# so a "<name>/SKILL.md" path inside a tool input is the invocation record.
# Quantifiers are bounded (and findall calls gated on a substring check)
# because an unbounded run before a literal backtracks quadratically on long
# matchless inputs such as base64 blobs.
_CODEX_SKILL_RE = re.compile(r"([\w.-]{1,255})/SKILL\.md\b")

# Codex has no Write tool either (files change via apply_patch/exec), so a
# handoff path in any tool input becomes a synthetic Write. This is broader
# than "actually wrote it" — acceptable, because the watermark still lints the
# on-disk file, and the alternative is a block no Codex agent can ever satisfy.
_CODEX_HANDOFF_RE = re.compile(r"[\w./~-]{0,512}handoff\.md")

# apply_patch hunks are Codex's file mutations; mapping them to Write/Edit lets
# tool-based rules fire unchanged. Paths end at a real newline, or at the
# backslash/quote that terminates them when the patch is embedded in exec JS.
_CODEX_PATCH_RE = re.compile(r"\*\*\* (Add|Update) File: ([^\n\\\"']+)")
_PATCH_ACTION_TOOL = {"Add": "Write", "Update": "Edit"}


def _codex_output_is_error(payload: dict) -> bool:
    """Read failure metadata without interpreting the command's stdout."""
    if payload.get("is_error") is True:
        return True
    output = payload.get("output")
    blocks = output if isinstance(output, list) else [output]
    for block in blocks:
        status = block.get("text") if (
            isinstance(block, dict) and block.get("type") == "input_text"
        ) else block
        if isinstance(status, str):
            try:
                status = json.loads(status)
            except json.JSONDecodeError:
                continue
        if not isinstance(status, dict):
            continue
        exit_code = status.get("exit_code")
        if status.get("is_error") is True or (type(exit_code) is int and exit_code != 0):
            return True
    return False


def _normalize_codex(entry: dict) -> dict:
    """Map a Codex rollout entry to the Claude entry shape; non-Codex or
    unmappable entries pass through untouched (helpers then ignore them)."""
    payload = entry.get("payload")
    if entry.get("type") not in ("event_msg", "response_item") or not isinstance(payload, dict):
        return entry
    ptype = payload.get("type")
    if ptype == "user_message" and isinstance(payload.get("message"), str):
        return {"type": "user", "message": {"role": "user", "content": payload["message"]}}
    if ptype in ("custom_tool_call", "function_call") and isinstance(payload.get("name"), str):
        return _codex_tool_call(payload, ptype)
    if ptype in ("custom_tool_call_output", "function_call_output") \
            and isinstance(payload.get("call_id"), str):
        result = {"type": "tool_result", "tool_use_id": payload["call_id"]}
        if _codex_output_is_error(payload):
            result["is_error"] = True
        return {"type": "user", "message": {"role": "user", "content": [
            result]}}
    if ptype == "token_count":
        info = payload.get("info")
        usage = info.get("last_token_usage") if isinstance(info, dict) else None
        if isinstance(usage, dict):
            # Codex input_tokens already includes cached tokens (measured).
            normalized = {"input_tokens": usage.get("input_tokens") or 0,
                          "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
            window = info.get("model_context_window")
            if isinstance(window, int) and window > 0:
                normalized["model_context_window"] = window
            return {"type": "assistant", "message": {"role": "assistant", "usage": normalized}}
    return entry


def _codex_tool_call(payload: dict, ptype: str) -> dict:
    raw = payload.get("arguments") if ptype == "function_call" else payload.get("input")
    raw = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    if ptype == "function_call":
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        input_ = parsed if isinstance(parsed, dict) else {"raw": raw}
    else:
        input_ = {"raw": raw}
    content = [{"type": "tool_use", "name": payload["name"], "input": input_,
                "id": payload.get("call_id")}]
    if "SKILL.md" in raw:
        for skill in dict.fromkeys(_CODEX_SKILL_RE.findall(raw)):
            content.append({"type": "tool_use", "name": "Skill", "input": {"skill": skill}})
    files = [(_PATCH_ACTION_TOOL[action], path.strip())
             for action, path in _CODEX_PATCH_RE.findall(raw)]
    if "handoff.md" in raw:
        files += [("Write", path) for path in _CODEX_HANDOFF_RE.findall(raw)
                  if Path(path).name == "handoff.md"]  # skip e.g. my-handoff.md
    for tool_name, path in dict.fromkeys(files):
        # Reuse the real call_id so the tool output marks these successful.
        content.append({"type": "tool_use", "name": tool_name,
                        "input": {"file_path": path}, "id": payload.get("call_id")})
    return {"type": "assistant", "message": {"role": "assistant", "content": content}}


def prompt_text(entry: dict) -> str | None:
    """Text of a real user prompt; None for tool_result carriers and sidechains."""
    if entry.get("type") != "user" or entry.get("isSidechain") is True:
        return None
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content if content.strip() else None
    if isinstance(content, list):
        texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        joined = "\n".join(t for t in texts if t)
        return joined if joined.strip() else None
    return None


def last_prompt_index(entries: list[dict]) -> int | None:
    """Index of the last real user prompt; entries after it form the current turn."""
    last = None
    for i, entry in enumerate(entries):
        if prompt_text(entry) is not None:
            last = i
    return last


def tool_calls(entry: dict) -> list[ToolCall]:
    if entry.get("type") != "assistant" or entry.get("isSidechain") is True:
        return []
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    calls = []
    for c in content:
        if not isinstance(c, dict) or c.get("type") != "tool_use":
            continue
        input_ = c.get("input") or {}
        if not isinstance(c.get("name"), str) or not isinstance(input_, dict):
            continue
        tool_use_id = c.get("id") if isinstance(c.get("id"), str) else None
        calls.append(ToolCall(name=c["name"], input=input_, tool_use_id=tool_use_id))
    return calls


def successful_tool_results(entries: list[dict]) -> set[str]:
    """tool_use_ids whose tool_result came back without is_error."""
    successful = set()
    for entry in entries:
        if entry.get("type") != "user" or entry.get("isSidechain") is True:
            continue
        message = entry.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tool_use_id = block.get("tool_use_id")
            if isinstance(tool_use_id, str) and block.get("is_error") is not True:
                successful.add(tool_use_id)
    return successful


def _last_usage(entries: list[dict]) -> dict | None:
    last = None
    for entry in entries:
        if entry.get("type") != "assistant":
            continue
        usage = (entry.get("message") or {}).get("usage")
        if isinstance(usage, dict):
            last = usage
    return last


def context_tokens(entries: list[dict]) -> int | None:
    """Current context size: the last assistant usage's input-side tokens."""
    last = _last_usage(entries)
    if last is None:
        return None
    return sum(int(last.get(k) or 0) for k in
               ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))


def context_window(entries: list[dict]) -> int | None:
    """Window reported by the transcript itself; None when it doesn't carry
    one (Claude transcripts don't) — callers fall back to their own default."""
    last = _last_usage(entries)
    window = last.get("model_context_window") if last else None
    return window if isinstance(window, int) and window > 0 else None


def read_hook_input(label: str) -> dict | None:
    """Parse hook JSON from stdin; None (with a stderr note) means fail open."""
    try:
        hook_input = json.loads(sys.stdin.read())
        if not isinstance(hook_input, dict):
            raise ValueError("hook input is not an object")
        return hook_input
    except (ValueError, json.JSONDecodeError) as exc:
        note(label, f"unreadable hook input, fail-open: {exc}")
        return None


def run_fail_open(label: str, body: Callable[[], int]) -> int:
    """Hook boundary: an internal failure must never lock a session (exit 0)."""
    try:
        return body()
    except Exception as exc:
        note(label, f"hook evaluation failed, fail-open: {type(exc).__name__}")
        return 0
