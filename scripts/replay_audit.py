#!/usr/bin/env python3
"""Replay a corpus of fixture transcripts against the skill verifier and check
each recorded verdict — the cheapest way to catch a rule edit that silently
breaks a past decision (CI regression, promptfoo-style).

Manifest (JSON):
    {"cases": [
        {"name": "...", "transcript": "case.jsonl", "rules": "rules.json",
         "expect_block": ["rule-id", ...]}   # [] means the turn should pass
    ]}

`transcript` and `rules` resolve relative to the manifest's directory unless
absolute. `expect_block` is the set of rule ids expected to be violated; the
audit fails when the actual violated set differs. Exit 0 all match, 1 on any
mismatch, 2 usage/read error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
from skill_invocation_verifier import evaluate_transcript, load_rules  # noqa: E402
from transcript import parse_transcript  # noqa: E402


def _resolve(base: Path, raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else base / path


def run_case(base: Path, case: dict) -> tuple[bool, str]:
    name = case.get("name", "unnamed")
    rules = load_rules(str(_resolve(base, case["rules"])), None)
    if not rules:
        return False, f"FAIL {name}: rules not loaded"
    transcript = _resolve(base, case["transcript"])
    violations = evaluate_transcript(rules, parse_transcript(transcript))
    actual = sorted(v.rule_id for v in violations)
    expected = sorted(case.get("expect_block", []))
    if actual == expected:
        return True, f"ok   {name}: {actual or 'pass'}"
    return False, f"FAIL {name}: expected {expected}, got {actual}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest")
    args = parser.parse_args()
    manifest_path = Path(args.manifest)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read manifest: {exc}", file=sys.stderr)
        return 2
    cases = manifest.get("cases") if isinstance(manifest, dict) else None
    if not isinstance(cases, list) or not cases:
        print("manifest has no 'cases' list", file=sys.stderr)
        return 2
    base = manifest_path.resolve().parent
    passed = 0
    ok_all = True
    for case in cases:
        try:
            ok, line = run_case(base, case)
        except (OSError, KeyError, ValueError) as exc:
            ok, line = False, f"FAIL {case.get('name', 'unnamed')}: {exc}"
        print(line)
        passed += ok
        ok_all &= ok
    print(f"{passed}/{len(cases)} passed" if not ok_all else f"{passed} passed")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
