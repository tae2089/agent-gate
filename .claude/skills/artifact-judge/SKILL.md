---
name: artifact-judge
description: Score an agent artifact's semantic quality (vagueness, grounding, actionability) with an independent LLM judge. Use when asked to evaluate, score, or judge an artifact (handoff.md, review brief, task/spec, dispatch plan, implementation notes), or to check whether one is too vague to act on. Not for reviewing code — this judges documents.
---

# Artifact Judge

Two-tier gate: the deterministic lint catches absence for free; this skill pays
for one LLM judgment only when the artifact passes it. The judge must be an
independent context — never the session that wrote the artifact.

Scoring rules, the judge prompt template, the per-type source-material table,
and the honesty caveats live in `docs/rubric-judge.md`. Read it before step 3
and use its template verbatim; do not restate or fork the rubric here.

## Procedure

1. **Resolve inputs.** Identify the artifact path, its type, and its contract
   (one line: what this artifact must achieve). Collect the source material
   the type requires per the table in `docs/rubric-judge.md` — paths and
   excerpts, e.g. `git log` + changed files for a handoff. Done when: artifact
   path, contract line, and every source-material path are listed.
2. **Tier-1 gate.** If the type is registered in `scripts/artifact_lint.py`,
   run it. On FAIL, stop: report the missing checks as the fix list — do not
   run the judge on a structurally broken artifact. Done when: PASS recorded,
   or the run ended with the lint report.
3. **Spawn the judge.** Launch one independent subagent (fresh context). Give
   it ONLY the filled template from `docs/rubric-judge.md`: artifact content,
   source material, contract. Never include the authoring conversation or
   your own opinion of the artifact. Require the template's JSON as the entire
   reply. Done when: a JSON reply is captured.
4. **Validate the verdict.** For each scored dimension, check the evidence is
   a real quote — a substring actually present in the artifact or source
   material. A score whose evidence is missing or fabricated counts as
   "insufficient evidence". If any dimension is insufficient, re-run step 3
   once with a note naming the failed dimension; if it fails again, report
   "judgment unreliable" instead of a score. Done when: every reported score
   has verified evidence, or the run is marked unreliable.
5. **Decide and report.** Approval line: every dimension ≥ 0.6 AND weighted
   total ≥ 0.8. Report per-dimension scores with their evidence quotes, the
   verdict, and `top_fix`. For decisions that matter (an artifact about to be
   handed to another team/session), run step 3 three times and report the
   median. Done when: the user has scores, verdict, and the single next fix.

## Boundaries

- One artifact per run; judge multiple artifacts as separate runs.
- The judge scores the document, not the work it describes — a truthful
  handoff about failed work should score high on honesty.
- A downstream outcome (the next session proceeding without re-asking) beats
  any score; when they disagree, trust the outcome and note the rubric gap.
