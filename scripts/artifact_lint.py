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
    line_ratio: float | None = None    # with pattern: share of content lines that must match


FILE_PATH_PATTERN = r"[\w./-]+\.(md|py|go|ts|tsx|java|kt|json|yaml|yml|sh|toml)\b"
# Stable acceptance-criterion id; shared with scripts/readiness_gate.py.
AC_ID_PATTERN = r"\bAC-\d+\b"

# Full-tier design artifacts must be real fenced blocks, not prose that merely
# mentions P1 or Mermaid. P1/P2 establish a minimum trace; semantic branch and
# failure completeness remains the independent judge's responsibility.
PSEUDOCODE_BLOCK_PATTERN = (
    r"(?im)^```(?:text|pseudocode)[ \t]*\r?\n"
    r"(?:(?!^```[ \t]*$)[\s\S])*?"
    r"^[ \t]*P1(?:[ \t]+|[.:])[^\r\n]+\r?\n"
    r"(?:(?!^```[ \t]*$)[\s\S])*?"
    r"^[ \t]*P2(?:[ \t]+|[.:])[^\r\n]+\r?\n"
    r"(?:(?!^```[ \t]*$)[\s\S])*?^```[ \t]*$"
)
FLOW_DIAGRAM_BLOCK_PATTERN = (
    r"(?im)^```mermaid[ \t]*\r?\n[ \t]*"
    r"(?:(?:flowchart|graph)[ \t]+(?:TB|TD|BT|RL|LR)"
    r"|sequenceDiagram|stateDiagram(?:-v2)?)[^\r\n]*\r?\n"
    r"(?:(?!^```[ \t]*$)[\s\S])*?^```[ \t]*$"
)

HANDOFF_CHECKS = [
    Check("goal", 0.15, False, "goal section", ["목표", "goal"]),
    Check("completed", 0.10, False, "completed-work section", ["완료", "completed", "work done"]),
    Check("decisions", 0.20, True, "decisions/judgments section", ["결정", "판단", "decision", "judgment"]),
    Check("verified", 0.10, False, "verified-state section", ["검증", "verified", "verification"]),
    Check("next", 0.20, True, "next-steps section", ["다음", "next"]),
    Check("file_paths", 0.15, False, "at least one file path cited", pattern=FILE_PATH_PATTERN),
    # Paraphrased user judgments lose recall vs verbatim text (arXiv 2601.00821),
    # so reward a literal quote or blockquote — the "task B is low-value" case.
    Check("user_quotes", 0.10, False, "a verbatim quote or blockquote preserved",
          pattern=r"(?m)(^\s*>\s*\S|[\"“”「『][^\"“”」』\n]{3,}[\"“”」』])"),
]

# implementation.md uses whole-document checks. Full-tier existence is already
# decided by the project workflow, so pseudocode and a control-flow diagram are
# deterministic floors here; their semantic quality remains a tier-2 judgment.
IMPLEMENTATION_CHECKS = [
    Check("approach", 0.25, False, "design approach stated",
          pattern=r"설계|접근|구조|방식|위치|(?i:approach|design|architecture)"),
    Check("assumptions", 0.15, False, "assumptions or measured evidence labeled",
          pattern=r"가정|전제|실측|근거|(?i:assumption|measured)"),
    Check("affected_files", 0.30, True, "affected modules/files cited", pattern=FILE_PATH_PATTERN),
    Check("risks", 0.30, True, "risks/edge cases listed",
          pattern=r"위험|엣지|한계|(?i:risk|edge)"),
    Check("pseudocode", 0.0, True, "fenced P1/P2 pseudocode block",
          pattern=PSEUDOCODE_BLOCK_PATTERN),
    Check("flow_diagram", 0.0, True, "fenced control-flow Mermaid block",
          pattern=FLOW_DIAGRAM_BLOCK_PATTERN),
]

TASK_CHECKS = [
    Check("contract", 0.25, True, "contract section", ["계약", "contract"]),
    Check("test_plan", 0.20, True, "ordered test-plan section", ["테스트 계획", "test plan"]),
    Check("implementation", 0.15, True, "implementation checklist section", ["구현", "implementation"]),
    Check("verification", 0.15, True, "verification checklist section", ["검증", "verification"]),
    Check("acceptance_ids", 0.25, True, "at least one stable AC-number identifier",
          pattern=AC_ID_PATTERN),
]

_WALKTHROUGH_ENTRY = r"^\[[^\]]*\]\s*(decision|error|verification)\s*:"

WALKTHROUGH_CHECKS = [
    Check("decisions", 0.35, True, "at least one decision entry",
          pattern=r"(?m)^\[[^\]]*\]\s*decision\s*:"),
    Check("verifications", 0.35, True, "at least one verification entry",
          pattern=r"(?m)^\[[^\]]*\]\s*verification\s*:"),
    Check("format_discipline", 0.30, False, "entries follow the [time] type: line format",
          pattern=_WALKTHROUGH_ENTRY, line_ratio=0.8),
]

TYPES = {
    "handoff": HANDOFF_CHECKS,
    "implementation": IMPLEMENTATION_CHECKS,
    "task": TASK_CHECKS,
    "walkthrough": WALKTHROUGH_CHECKS,
}

# High-signal phrases that try to steer a judge reading this artifact, rather
# than describe the work. Deterministic pre-screen for the tier-2 judge — a
# match is surfaced, not auto-scored (JudgeDeceiver, CCS 2024). Curated tight
# to limit false positives on artifacts that merely discuss approval.
INJECTION_PATTERNS = [
    r"(?i)ignore\s+(all\s+|the\s+)?(previous|prior|above)\s+instructions?",
    r"(?i)disregard\s+(the\s+)?(above|previous|prior)",
    r"이전.{0,8}지시.{0,10}(무시|무효|잊)",
    r"(?i)(you\s+are|act\s+as)\s+(the\s+|an?\s+)?(judge|evaluator|grader)",
    r"(?i)(assign|give|output|return)\s+[^\n]{0,24}"
    r"(1\.0|100%|full\s+marks|highest\s+score|max(?:imum)?\s+score)",
    r"(만점|최고점|최고 ?점수)(으로|을|를)?\s*(평가|채점|줘|주세요|매겨)",
    r"(?i)verdict\s*[:=]\s*(approve|pass)",
]


def scan_injection(text: str) -> list[str]:
    """Verbatim spans matching a judge-directed injection pattern."""
    found = []
    for pattern in INJECTION_PATTERNS:
        found.extend(m.group(0).strip() for m in re.finditer(pattern, text))
    return found


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


def _line_ratio_met(check: Check, text: str) -> bool:
    """True when enough content lines (non-empty, non-heading) match the pattern."""
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        return False
    matching = sum(1 for ln in lines if re.match(check.pattern, ln))
    return matching / len(lines) >= check.line_ratio


def lint(text: str, checks: list[Check]) -> dict:
    sections = _sections(text)
    results = {}
    score = 0.0
    floor_failures = []
    for check in checks:
        if check.line_ratio is not None:
            ok = _line_ratio_met(check, text)
        elif check.pattern:
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


def _run_injection_scan(path: str) -> int:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        print(f"cannot read {path}: {exc}", file=sys.stderr)
        return 2
    findings = scan_injection(text)
    if not findings:
        return 0
    print(f"SUSPICIOUS: {len(findings)} judge-directed instruction(s) found:")
    for span in findings:
        print(f"  {span!r}")
    return 3


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--type", help=f"artifact type ({', '.join(sorted(TYPES))})")
    parser.add_argument("--injection-scan", action="store_true",
                        help="scan for judge-directed injection instead of structural lint")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("file")
    args = parser.parse_args()
    if args.injection_scan:
        return _run_injection_scan(args.file)
    if not args.type:
        print("--type is required unless --injection-scan is given", file=sys.stderr)
        return 2
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
