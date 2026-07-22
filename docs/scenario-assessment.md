# Scenario Contract Assessment

AI가 만든 시나리오가 요구사항을 빠뜨리거나 구현 구조를 고정하지 않는지 독립 컨텍스트에서 한 번 검토한다. 결정론적 코드는 schema, AC/P 참조, ID 집합, 파일 hash를 검증하고, 독립 판정자는 의미 완전성·관찰 가능성·최소성·ownership을 검토한다.

## Workflow

```bash
python3 scripts/scenario_gate.py review-template \
  _workspace/<task> --project-root . > /tmp/scenario-review-template.json
# 독립 판정 결과를 _workspace/<task>/scenario-review.json으로 작성
python3 scripts/scenario_gate.py readiness _workspace/<task> --project-root .
```

마지막 명령이 exit 0이어야 한다. `task.md`, 부모 `implementation.md`, parent contract 또는 child overlay가 바뀌면 review hash가 stale이므로 template부터 다시 생성한다.

## Independent Judge Prompt

```text
당신은 구현 전에 Scenario Contract만 검토하는 독립 판정자다.
문서를 작성하지 않았고 작성 대화를 모른다.

제공된 task.md Contract/AC, implementation.md Pseudocode, scenario-contract.json
또는 child scenario-overlay.json, review template만 사용한다.

다음을 검토하라.
- 모든 AC와 중요한 성공/실패/거부/복구 terminal이 최소 시나리오 집합에 포함되는가?
- Then이 내부 함수, mock 호출, 코드 구조가 아니라 관찰 가능한 결과인가?
- 같은 결과를 반복하거나 P 단계마다 억지 시나리오를 만들지 않았는가?
- child ownership이 parent-candidate를 숨기거나 불필요하게 승격하지 않는가?
- runner level이 해당 행동을 검증할 수 있는 가장 저렴한 경계인가?

차단 문제가 하나라도 있으면 verdict를 revise로 유지하고 blocking_findings에
구체적인 scenario ID와 누락 terminal을 적어라. 문제가 없을 때만 verdict를 pass로
바꾸고 findings를 비워라. template의 hash와 reviewed_scenarios는 변경하지 마라.
완성된 JSON 객체만 반환하라.
```

## Review Schema

```json
{
  "schema_version": 1,
  "task_sha256": "<template>",
  "flow_sha256": "<template>",
  "subject_sha256": "<template>",
  "parent_contract_sha256": "<template or empty parent value>",
  "reviewed_scenarios": ["<template IDs>"],
  "verdict": "pass",
  "blocking_findings": []
}
```

## Decision Boundary

- 작성 AI는 contract/overlay를 제안한다.
- 독립 판정자는 의미 결함만 제안하고 테스트 성공을 선언하지 않는다.
- `scenario_gate.py`가 hash, 참조, 실행 결과와 rollout mode를 최종 판정한다.
- 성공 결과가 있어도 잘못된 요구사항을 증명하지는 못하므로 고위험 AC는 사용자 또는 도메인 승인 정책을 별도로 둔다.
