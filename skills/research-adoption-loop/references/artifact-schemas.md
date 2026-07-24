# Research Adoption Artifact Schemas

Use these exact JSON shapes. Unknown or missing fields fail validation. Hash
fields are lowercase SHA-256 values over the exact persisted artifact bytes.

## Request input

```json
{
  "schema_version": 2,
  "source": "manual",
  "source_ref": "conversation:<request-ref>",
  "request": "<verbatim user request>",
  "question": "<one adoption question>",
  "requirements": [
    "<one atomic requirement>"
  ],
  "constraints": [
    "<repository constraint or non-goal>"
  ],
  "success_criteria": [
    "<observable success criterion>"
  ],
  "evidence": [
    "<request-scoped framing evidence>"
  ]
}
```

## Requirements assessment

Every failed criterion requires at least one concrete finding. Passing criteria
must have no findings.

```json
{
  "schema_version": 1,
  "request_sha256": "<active request hash>",
  "criteria": {
    "clarity": {
      "status": "pass",
      "findings": []
    },
    "completeness": {
      "status": "pass",
      "findings": []
    },
    "consistency": {
      "status": "pass",
      "findings": []
    },
    "necessity": {
      "status": "pass",
      "findings": []
    },
    "traceability": {
      "status": "pass",
      "findings": []
    },
    "feasibility": {
      "status": "pass",
      "findings": []
    },
    "verifiability": {
      "status": "pass",
      "findings": []
    },
    "atomicity": {
      "status": "pass",
      "findings": []
    }
  }
}
```

Criterion meanings:

| Criterion | Pass condition |
| --- | --- |
| clarity | Wording has one unambiguous interpretation in repository context. |
| completeness | Required behavior, constraints, success conditions, and relevant boundaries are present. |
| consistency | Requirements do not contradict one another or verified repository contracts. |
| necessity | Each requirement is needed for the adoption question or an explicit constraint. |
| traceability | Each requirement maps to the user request and can later map to evidence and verification. |
| feasibility | A safe bounded investigation and prototype are possible with available repository capabilities. |
| verifiability | Each requirement has an observable check or decision criterion. |
| atomicity | Each requirement expresses one independently assessable obligation. |

## Evidence grade

The grade expresses source certainty only. It is not a requirement-quality
result, repository-fit result, prototype result, or adoption score.

```json
{
  "schema_version": 1,
  "request_sha256": "<active request hash>",
  "grade": "moderate",
  "sources": [
    {
      "url": "https://example.org/authoritative-source",
      "title": "Authoritative source title",
      "claims": [
        "<specific claim used>"
      ]
    }
  ],
  "rationale": [
    "<why this certainty label is justified>"
  ],
  "limitations": [
    "<conflict, indirectness, missing data, or applicability limit>"
  ]
}
```

Allowed grades are `high`, `moderate`, `low`, and `very-low`.

## Adoption brief

`evidence_certainty` must exactly match the persisted evidence grade and
rationale. `repository_fit` and `prototype_result` remain independent.

```json
{
  "schema_version": 1,
  "request_sha256": "<active request hash>",
  "requirements_assessment_sha256": "<requirements assessment hash>",
  "evidence_grade_sha256": "<evidence grade hash>",
  "prototype_result_sha256": "<captured prototype result hash>",
  "scenario_result_sha256": "<current scenario result hash>",
  "verdict": "adopt",
  "evidence_certainty": {
    "grade": "moderate",
    "rationale": [
      "<same rationale as evidence-grade.json>"
    ]
  },
  "repository_fit": {
    "status": "pass",
    "evidence": [
      "<repository architecture or constraint evidence>"
    ],
    "findings": []
  },
  "prototype_result": {
    "status": "pass",
    "evidence": [
      "<observable prototype verification evidence>"
    ],
    "findings": []
  },
  "findings": [],
  "prototype_disposition": "adopted",
  "evolution_candidate": {
    "kind": "feature",
    "title": "<candidate title>",
    "problem": "<repository problem the adoption solves>",
    "evidence": [
      "<evidence suitable for Evolution admission>"
    ],
    "labels": [
      "research-adoption"
    ]
  }
}
```

For `reject`:

- keep all hash and axis fields;
- set `findings` to one or more evidence-backed reasons;
- set `prototype_disposition` to `removed` or `not-created`;
- set `evolution_candidate` to `null`.

## Supporting and derived artifacts

- `evidence-grade.json`: persisted intermediate evidence-certainty receipt.
- `iterations/001/prototype-result.json`: exact captured prototype result.
- `scenario-result.json`: current repository result used for Completion.
- `evolution-candidate.json`: derived only from an adopted, Gate-passed,
  current brief and validated by the native Evolution candidate validator.
