# Task and Implementation Readiness

코드 편집 전에 두 질문을 분리해 답한다.

1. `task.md`는 **무엇을 끝내야 하는지** 모호하지 않은가?
2. `implementation.md`는 **어떻게 구현할지** 작업자가 다시 설계하지 않아도 되는가?

LLM은 의미 점수와 근거를 제안한다. `scripts/readiness_gate.py`는 문서 구조, 점수 범위, 차원별 하한, 가중합, AC 추적성, 원문 인용, SHA-256 freshness를 다시 계산해 최종 판정한다. 평가 JSON의 합계나 verdict는 신뢰하지 않는다.

## Workflow

```bash
python3 scripts/artifact_lint.py --type task _workspace/<task>/task.md
python3 scripts/artifact_lint.py --type implementation _workspace/<task>/implementation.md
python3 scripts/readiness_gate.py --template _workspace/<task>
# independent judge output을 _workspace/<task>/assessment.json에 저장
python3 scripts/readiness_gate.py _workspace/<task>
```

마지막 명령이 `READY`와 exit 0을 반환해야 한다. `task.md` 또는 `implementation.md`를 한 글자라도 바꾸면 해시가 달라지므로 assessment를 다시 생성·판정해야 한다.

Full root의 `implementation.md`는 `flow-design`을 적용해 작성한다. Tier-1 lint는 fenced `text`/`pseudocode` 블록 안의 최소 `P1`·`P2` 단계와, fenced Mermaid 블록 안의 `flowchart`/`graph`/`sequenceDiagram`/`stateDiagram` 제어 흐름을 0점 floor로 요구한다. 블록의 존재는 결정론적으로 차단하지만 분기 폐쇄성, 실패 경로, 다이어그램-수도코드 대응은 아래 독립 판정자가 의미적으로 평가한다.

## Inherited Child Workflow

분해된 작업은 작아져도 governance tier가 내려가지 않는다. 자식은 독립 Fast로 재판정하지 않고 직접 Full 부모의 readiness와 담당 flow/AC 범위를 상속한다.

```bash
python3 scripts/readiness_gate.py _workspace/<parent>
python3 scripts/readiness_gate.py \
  --inherit-from _workspace/<parent> \
  _workspace/<child> > _workspace/<child>/inherited-readiness.json
# generated hash와 parent/mode는 유지하고 flow_refs/acceptance_refs만 채운다.
python3 scripts/readiness_gate.py _workspace/<child>
```

manifest는 `mode: "inherit-full"`, direct Full `parent_task`, 현재 `child_task_sha256`, 비어 있지 않고 중복 없는 `flow_refs`/`acceptance_refs`만 허용한다. `flow_refs`는 부모 `implementation.md`의 `P<number>`, 모든 child AC는 부모와 자식 `task.md` 양쪽에 존재하고 `acceptance_refs`에 포함돼야 한다. unknown field(`tier: fast` 포함), symlink/경로 이동, inherited parent chain, child의 자체 `implementation.md`/`assessment.json`, stale parent/child, 가짜 참조는 모두 fail-closed한다.

유효 manifest PostToolUse가 session을 child task에 bind한다. 이후 보호 편집마다 child manifest와 Full parent assessment를 함께 재검증하므로 부모 flow가 바뀌면 모든 child session이 다음 편집에서 차단된다. 독립 Fast는 `_workspace`와 명시적 문서 예외에만 남고 보호된 source edit 승인 경로는 아니다.

## Task Ambiguity

`ambiguity = 1 - Σ(score × weight)`. 통과 조건은 ambiguity ≤ 0.20, 모든 floor 충족, `blocking_unknowns`가 빈 배열인 것이다.

| 차원 | 가중치 | floor | 판정 질문 |
| --- | ---: | ---: | --- |
| `outcome_clarity` | 0.35 | 0.75 | 원하는 최종 상태와 범위가 한 가지로 읽히는가? |
| `constraint_clarity` | 0.25 | 0.65 | must/must-not, 호환성, 비목표가 명시됐는가? |
| `acceptance_clarity` | 0.25 | 0.70 | `AC-<number>`가 관찰·검증 가능한가? |
| `grounding_clarity` | 0.15 | 0.60 | 실제 파일·시스템·실측 사실에 연결되는가? |

의도적으로 미룬 선택이 비목표나 제약으로 명시됐다면 모호함으로 감점하지 않는다. 문서 길이나 항목 수는 점수가 아니다.

## Implementation Readiness

`readiness = AC coverage × 0.35 + decision_closure × 0.30 + change_specificity × 0.20 + risk_response × 0.15`. 통과 조건은 readiness ≥ 0.80, AC coverage = 1.0, `unresolved_decisions`가 빈 배열인 것이다.

| 차원 | 판정 질문 |
| --- | --- |
| `decision_closure` | 구현자가 정책·기본값·실패 동작을 다시 선택해야 하는가? |
| `change_specificity` | 대상 모듈, 책임 경계, 데이터/제어 흐름이 특정됐는가? |
| `risk_response` | 보안, 호환성, 오류, edge case에 대응 정책이 있는가? |

AC coverage는 `task.md`의 고유 `AC-<number>`가 `implementation.md`에 모두 등장하는지 코드가 계산한다.

## Independent Judge Prompt

아래 내용만 독립 컨텍스트에 제공한다. 작성 대화나 작성자의 자기평가는 제외한다.

```text
당신은 코드 편집 전 readiness 판정자다. 문서를 작성하지 않았고 작성 과정을 모른다.
제공된 task.md, implementation.md, assessment template만 사용한다.

task 차원과 implementation 차원을 이 문서의 정의대로 0.0~1.0 채점하라.
각 evidence는 해당 문서에 실제로 존재하는 짧은 연속 문자열을 정확히 복사하라.
차단되는 미결정은 blocking_unknowns 또는 unresolved_decisions에 구체적으로 적어라.
확신 부족을 높은 점수로 숨기지 말고, 장황함에는 가점을 주지 마라.
template의 키와 SHA-256 값은 변경하지 말고 완성된 JSON 객체만 반환하라.
```

판정 결과가 미달이면 숫자만 올리지 않는다. 진단에 맞춰 `task.md` 또는 `implementation.md`를 고치고 새 template부터 다시 시작한다.

## Assessment Schema

```json
{
  "schema_version": 1,
  "task": {
    "sha256": "<template value>",
    "dimensions": {
      "outcome_clarity": {"score": 0.0, "evidence": "exact task excerpt"},
      "constraint_clarity": {"score": 0.0, "evidence": "exact task excerpt"},
      "acceptance_clarity": {"score": 0.0, "evidence": "exact task excerpt"},
      "grounding_clarity": {"score": 0.0, "evidence": "exact task excerpt"}
    },
    "blocking_unknowns": []
  },
  "implementation": {
    "sha256": "<template value>",
    "dimensions": {
      "decision_closure": {"score": 0.0, "evidence": "exact implementation excerpt"},
      "change_specificity": {"score": 0.0, "evidence": "exact implementation excerpt"},
      "risk_response": {"score": 0.0, "evidence": "exact implementation excerpt"}
    },
    "unresolved_decisions": []
  }
}
```

## Enforcement Boundary

프로젝트 hook은 유효한 Full assessment 또는 inherited readiness manifest 작성 후 현재 session을 그 task에 바인딩한다. 이후 직접 `Write`/`Edit`/`apply_patch`로 프로젝트 파일을 편집할 때마다 bound proof를 재검증한다. 확장자 없는 파일과 미등록 확장자도 기본 보호하며, 검증된 `_workspace/**`, `.md`/`.rst`/`.txt`, README·LICENSE 등 명시적 문서 파일만 부트스트랩과 문서 작업을 위해 예외로 둔다. target은 예외 판정 전에 프로젝트 내부의 비심링크 경로로 확인하므로 외부·심링크 경로가 문서 확장자로 우회할 수 없다.

hook 입력, 직접 target, project root를 확정하지 못하면 stderr 진단 후 fail-open한다. 프로젝트 root와 target이 확정된 뒤에는 unsafe target, session marker, assessment freshness 또는 readiness 검증 실패를 fail-closed한다. readiness와 handoff marker는 같은 원자적 JSON 저장소를 사용하지만 payload 경로 정책과 caller-facing 상태는 각 도메인 모듈이 소유한다. readiness 결과 캐시는 현재 실측 비용이 sub-millisecond라 도입하지 않는다. 셸 명령 등 간접 파일 쓰기는 안전한 명령 분석 정책이 별도로 필요하므로 보장 범위가 아니다.

## Attribution

모호함을 `1 - weighted clarity`로 계산하고 차원별 floor를 함께 두는 핵심은 [Q00/ouroboros ambiguity evaluator](https://github.com/Q00/ouroboros/blob/6202662eae2dad0531225a93e27b18f792bb139b/src/ouroboros/bigbang/ambiguity.py#L35-L55)에서 가져왔다. 이 프로젝트는 Ouroboros의 인터뷰·오케스트레이션·이벤트 저장소는 복제하지 않는다.
