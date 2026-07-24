#!/usr/bin/env python3
"""SessionStart hook (matcher: compact): re-inject the latest handoff artifact
after compaction.

Closes the loop context_watermark.py opens: the watermark forces a handoff
file to be written before the context fills up; this hook feeds that file
back in right after compaction, so judgments and decisions that the lossy
summary dropped come back deterministically.

For SessionStart hooks, plain stdout is added to the session context.
No handoff, a stale one, or any internal error → silent exit 0 (fail-open).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from handoff_state import iter_handoff_candidates, load_session_handoff, resolve_handoff
from transcript import note, read_hook_input, run_fail_open

LABEL = "handoff-reinject"
MAX_CHARS = 8_000
DEFAULT_MAX_AGE_HOURS = 24.0


def _fresh(path: Path, cutoff: float) -> bool:
    try:
        return path.stat().st_mtime >= cutoff
    except OSError:
        return False


def find_latest_handoff(cwd: Path, max_age_hours: float,
                        session_id: str | None = None) -> Path | None:
    """Find the session-bound handoff, or one unambiguous legacy candidate."""
    try:
        root = cwd.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    cutoff = time.time() - max_age_hours * 3600
    if session_id:
        marker_exists, session_handoff = load_session_handoff(root, session_id)
        if marker_exists:
            if session_handoff is not None and _fresh(session_handoff, cutoff):
                return session_handoff
            return None

    fresh = []
    for candidate in iter_handoff_candidates(root):
        path = resolve_handoff(root, candidate)
        if path is not None and _fresh(path, cutoff):
            fresh.append(path)
    unique = list(dict.fromkeys(fresh))
    return unique[0] if len(unique) == 1 else None


def _run_hook(hook_input: dict, max_age_hours: float) -> int:
    cwd = Path(hook_input["cwd"])
    handoff = find_latest_handoff(cwd, max_age_hours, hook_input.get("session_id"))
    if handoff is None:
        return 0
    try:
        content = handoff.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        note(LABEL, f"cannot read {handoff}: {exc}")
        return 0
    if len(content) > MAX_CHARS:
        content = content[:MAX_CHARS] + "\n... [truncated — read the file for the rest]"
    print(
        f"[agent-loop] Context was just compacted. The pre-compaction handoff below is the "
        f"authoritative record of in-progress work — trust it over the compaction summary, "
        f"especially user corrections and value judgments. Source: {handoff}\n\n{content}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS)
    args = parser.parse_args()
    hook_input = read_hook_input(LABEL)
    if hook_input is None:
        return 0
    return run_fail_open(LABEL, lambda: _run_hook(hook_input, args.max_age_hours))


if __name__ == "__main__":
    sys.exit(main())
