#!/usr/bin/env python3
"""Shared transcript parsing and hook I/O boundary for agent-gate hooks.

Transcript entries are Claude Code JSONL lines. All helpers ignore sidechain
entries and tolerate malformed shapes, because hooks must fail open.
"""

from __future__ import annotations

import json
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
                entries.append(entry)
    return entries


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
