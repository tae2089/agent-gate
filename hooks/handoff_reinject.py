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
import json
import sys
import time
from pathlib import Path

MAX_CHARS = 8_000
DEFAULT_MAX_AGE_HOURS = 24.0


def find_latest_handoff(cwd: Path, max_age_hours: float) -> Path | None:
    candidates = list(cwd.glob("_workspace/*/handoff.md")) + [cwd / "handoff.md"]
    fresh = []
    cutoff = time.time() - max_age_hours * 3600
    for path in candidates:
        try:
            if path.is_file() and path.stat().st_mtime >= cutoff:
                fresh.append((path.stat().st_mtime, path))
        except OSError:
            continue
    return max(fresh)[1] if fresh else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS)
    args = parser.parse_args()
    try:
        hook_input = json.loads(sys.stdin.read())
        cwd = Path(hook_input["cwd"])
    except (ValueError, KeyError, TypeError) as exc:
        print(f"[handoff-reinject] unreadable hook input, fail-open: {exc}", file=sys.stderr)
        return 0
    handoff = find_latest_handoff(cwd, args.max_age_hours)
    if handoff is None:
        return 0
    try:
        content = handoff.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"[handoff-reinject] cannot read {handoff}: {exc}", file=sys.stderr)
        return 0
    if len(content) > MAX_CHARS:
        content = content[:MAX_CHARS] + "\n... [truncated — read the file for the rest]"
    print(
        f"[agent-gate] Context was just compacted. The pre-compaction handoff below is the "
        f"authoritative record of in-progress work — trust it over the compaction summary, "
        f"especially user corrections and value judgments. Source: {handoff}\n\n{content}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
