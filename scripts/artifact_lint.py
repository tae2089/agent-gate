#!/usr/bin/env python3
"""Deterministic structural lint for agent artifacts (tier-1 of the quality
ladder: cheap checks gate expensive LLM-rubric checks).

Scoring: weighted sum of checks in [0, 1]; PASS requires score >= 0.8 AND
every floor check present — a floor failure fails the artifact regardless of
score, so one strong dimension cannot mask a missing critical one.

This catches absence, not wrongness: a section can be present and still be
bad. Semantic quality belongs to an independent LLM judge (docs/rubric-judge.md).

Exit codes: 0 pass, 1 fail, 2 usage/read error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

PASS_THRESHOLD = 0.8
MIN_SECTION_CHARS = 10


@dataclass
class Check:
    key: str
    weight: float
    floor: bool
    description: str
    keywords: list[str] | None = None  # section checks
    pattern: str | None = None         # whole-document regex checks


HANDOFF_CHECKS = [
    Check("goal", 0.15, False, "goal section", ["목표", "goal"]),
    Check("completed", 0.15, False, "completed-work section", ["완료", "completed", "work done"]),
    Check("decisions", 0.20, True, "decisions/judgments section", ["결정", "판단", "decision", "judgment"]),
    Check("verified", 0.15, False, "verified-state section", ["검증", "verified", "verification"]),
    Check("next", 0.20, True, "next-steps section", ["다음", "next"]),
    Check("file_paths", 0.15, False, "at least one file path cited",
          pattern=r"[\w./-]+\.(md|py|go|ts|tsx|java|kt|json|yaml|yml|sh|toml)\b"),
]

TYPES = {"handoff": HANDOFF_CHECKS}


def _sections(text: str) -> list[tuple[str, str]]:
    """(heading, body) pairs from markdown headings; body is text until next heading."""
    parts = re.split(r"^(#{1,6} .+)$", text, flags=re.MULTILINE)
    sections = []
    for i in range(1, len(parts) - 1, 2):
        sections.append((parts[i].lstrip("# ").strip(), parts[i + 1]))
    if len(parts) % 2 == 0 and len(parts) >= 2:  # trailing heading with no body
        sections.append((parts[-1].lstrip("# ").strip(), ""))
    return sections


def _section_present(check: Check, sections: list[tuple[str, str]]) -> bool:
    for heading, body in sections:
        low = heading.lower()
        if any(k.lower() in low for k in check.keywords or []):
            if len(re.sub(r"\s", "", body)) >= MIN_SECTION_CHARS:
                return True
    return False


def lint(text: str, checks: list[Check]) -> dict:
    sections = _sections(text)
    results = {}
    score = 0.0
    floor_failures = []
    for check in checks:
        if check.pattern:
            ok = bool(re.search(check.pattern, text))
        else:
            ok = _section_present(check, sections)
        results[check.key] = ok
        if ok:
            score += check.weight
        elif check.floor:
            floor_failures.append(check.key)
    passed = score >= PASS_THRESHOLD and not floor_failures
    return {"score": round(score, 3), "passed": passed,
            "floor_failures": floor_failures, "checks": results}


def lint_file(path: Path, artifact_type: str) -> dict | None:
    """Library entry point for hooks. None means could-not-lint (caller fail-open)."""
    checks = TYPES.get(artifact_type)
    if checks is None:
        return None
    try:
        return lint(path.read_text(encoding="utf-8"), checks)
    except (OSError, UnicodeError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--type", required=True, help=f"artifact type ({', '.join(sorted(TYPES))})")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("file")
    args = parser.parse_args()
    checks = TYPES.get(args.type)
    if checks is None:
        print(f"unknown artifact type: {args.type} (known: {', '.join(sorted(TYPES))})", file=sys.stderr)
        return 2
    try:
        text = Path(args.file).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        print(f"cannot read {args.file}: {exc}", file=sys.stderr)
        return 2
    result = lint(text, checks)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        status = "PASS" if result["passed"] else "FAIL"
        print(f"{status} score={result['score']} (threshold {PASS_THRESHOLD})")
        for check in checks:
            mark = "ok" if result["checks"][check.key] else ("MISSING(floor)" if check.floor else "missing")
            print(f"  {check.key:12} {mark:14} {check.description}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
