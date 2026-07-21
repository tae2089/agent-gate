#!/usr/bin/env python3
"""PreInvocation hook: re-inject the latest handoff after an Antigravity
compaction, the counterpart to handoff_reinject.py on Claude/Codex.

Antigravity has no session-start command hook, but PreInvocation may inject
steps into the conversation trajectory and it receives the transcript path.
Antigravity records a compaction as a CHECKPOINT entry, so this hook injects
the handoff exactly once per new checkpoint (tracked by a session marker),
mirroring Claude's SessionStart(compact) reinject.

Output: {"injectSteps": [{"ephemeralMessage": ...}]} to inject, else {}.
Fail-open: any error emits {} so a shim bug never disrupts the session.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from handoff_reinject import MAX_CHARS, find_latest_handoff  # noqa: E402
from session_marker import read_marker, write_marker  # noqa: E402
from transcript import note, parse_transcript, read_hook_input  # noqa: E402

LABEL = "antigravity-reinject"
MARKER_NS = ".antigravity-reinject"


def latest_checkpoint(entries: list[dict]) -> int | None:
    """Highest step_index among CHECKPOINT entries; None if never compacted."""
    checkpoints = [e.get("step_index") for e in entries
                   if e.get("type") == "CHECKPOINT" and isinstance(e.get("step_index"), int)]
    return max(checkpoints) if checkpoints else None


def _no_inject() -> int:
    print("{}")
    return 0


def _run_hook(hook_input: dict, max_age_hours: float) -> int:
    transcript_path = hook_input.get("transcriptPath")
    workspaces = hook_input.get("workspacePaths")
    session_id = hook_input.get("conversationId")
    cwd = workspaces[0] if isinstance(workspaces, list) and workspaces else None
    if not isinstance(transcript_path, str) or not isinstance(cwd, str) \
            or not isinstance(session_id, str) or not session_id:
        return _no_inject()

    checkpoint = latest_checkpoint(parse_transcript(Path(transcript_path)))
    if checkpoint is None:
        return _no_inject()  # no compaction yet

    root = Path(cwd)
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError):
        return _no_inject()
    state, marker = read_marker(resolved, MARKER_NS, session_id)
    if state == "unsafe":
        return _no_inject()
    already = marker.get("checkpoint") if isinstance(marker, dict) else None
    if isinstance(already, int) and already >= checkpoint:
        return _no_inject()  # this compaction was already re-injected

    handoff = find_latest_handoff(root, max_age_hours, session_id)
    if handoff is None:
        return _no_inject()
    try:
        content = handoff.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        note(LABEL, f"cannot read {handoff}: {exc}")
        return _no_inject()
    if len(content) > MAX_CHARS:
        content = content[:MAX_CHARS] + "\n... [truncated — read the file for the rest]"

    write_marker(resolved, MARKER_NS, session_id, {"checkpoint": checkpoint})
    message = (
        f"[agent-gate] Context was just compacted. The pre-compaction handoff below is the "
        f"authoritative record of in-progress work — trust it over the compaction summary, "
        f"especially user corrections and value judgments. Source: {handoff}\n\n{content}"
    )
    print(json.dumps({"injectSteps": [{"ephemeralMessage": message}]}, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-age-hours", type=float, default=24.0)
    args = parser.parse_args()
    hook_input = read_hook_input(LABEL)
    if hook_input is None:
        return _no_inject()
    try:
        return _run_hook(hook_input, args.max_age_hours)
    except Exception as exc:  # always emit valid JSON; a shim bug must not disrupt the loop
        note(LABEL, f"fail-open: {type(exc).__name__}")
        return _no_inject()


if __name__ == "__main__":
    sys.exit(main())
