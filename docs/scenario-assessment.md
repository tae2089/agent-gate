# Scenario Evidence Assessment

AI가 만든 관찰 가능한 시나리오가 실제 구현과 검증 코드에 연결됐는지 독립 컨텍스트에서 검토한다. Python은 ID 완전성, 파일 위치, hash, 실행 결과와 점수를 검증하고, 독립 판정자는 관찰 항목과 코드 근거의 의미적 일치만 판단한다. Runner 명령은 판정 대상이 아니다.

## Workflow

```bash
python3 scripts/scenario_gate.py evidence-template \
  _workspace/<full-task> --project-root . > /tmp/scenario-evidence-template.json
# 독립 판정 결과를 _workspace/<full-task>/scenario-evidence.json으로 작성
python3 scripts/scenario_gate.py run _workspace/<full-task> --project-root . --json
python3 scripts/scenario_gate.py completion _workspace/<full-task> --project-root . --json
```

마지막 명령은 모든 필수 관찰 항목의 구현 매핑, 검증 매핑, 최신 실행이 갖춰져 Scenario Evidence Coverage가 100%일 때만 exit 0이다. Task, flow, contract 또는 source가 바뀌면 evidence가 stale이므로 template부터 다시 생성한다. Runner 설정 변경은 실행 결과만 무효화하며 의미 evidence를 다시 판정하지 않는다.

## Independent Judge Prompt

```text
당신은 구현 후 Scenario Evidence를 검토하는 독립 판정자다.
시나리오나 코드를 작성하지 않았고 작성 대화를 모른다.

제공된 Full task.md Contract/AC, implementation.md Pseudocode,
scenario-contract.json, 변경된 구현/검증 소스, evidence template만 사용한다.
Runner configuration, runner command, 성공 로그는 입력으로 받지 않는다.

각 observation에 대해 다음을 검토하라.
- expectation이 외부에서 관찰 가능하고 관련 AC/flow terminal을 실제로 표현하는가?
- implementation 위치가 그 행동을 실제로 구현하는 구체적인 코드인가?
- verification 위치가 해당 관찰 결과를 실제로 실패 가능하게 확인하는가?
- 존재 여부, 호출 여부, mock 상호작용처럼 내부 구조만 확인하지 않는가?
- 빠진 성공/실패/거부/복구 terminal 때문에 contract 자체가 불완전하지 않은가?

근거가 충분한 observation에는 project-relative path와 1-based line을
implementation 및 verification 배열에 기록하라. 의미 관계를 입증할 수 없거나
contract terminal이 누락됐으면 verdict를 revise로 유지하고 blocking_findings에
observation/AC/P ID와 이유를 적어라. 문제가 없을 때만 verdict를 pass로 바꾸고
findings를 비워라. Template의 hash와 observation ID/순서는 변경하지 마라.
완성된 JSON 객체만 반환하라.
```

## Evidence Schema

```json
{
  "schema_version": 1,
  "task_sha256": "<template>",
  "flow_sha256": "<template>",
  "contract_sha256": "<template>",
  "source_fingerprint": "<template>",
  "observations": [
    {
      "id": "O-EXAMPLE",
      "implementation": [{"path": "src/example.py", "line": 42}],
      "verification": [{"path": "tests/example_test.py", "line": 31}]
    }
  ],
  "verdict": "pass",
  "blocking_findings": []
}
```

## Score Boundary

```text
implementation mapping = valid implementation locations / required observations
verification mapping   = valid verification locations / required observations
fresh execution        = observations whose owning scenario passed / required observations
verified coverage      = all three conditions satisfied / required observations
```

- 필수 관찰 항목은 가중하지 않으며 완료 기준은 100%다.
- 이 값은 코드의 절대 정확도가 아니라 작성된 시나리오에 대한 추적 증거 완성도다.
- 저장소가 신뢰한 runner는 실행만 담당하며 점수나 의미 판정에 참여하지 않는다.
- inherited child도 Full 부모의 contract, evidence, result를 사용한다.
