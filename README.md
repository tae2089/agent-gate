# agent-gate

AI 코딩 에이전트를 위한 결정론적 강제 계층입니다. 프롬프트(스킬, CLAUDE.md)는 에이전트가 *따르려 노력*하는 지시이고, 이 저장소의 hook과 스크립트는 *구조적으로 강제*되는 규칙입니다.

설계 원칙 하나로 요약됩니다: **LLM은 제안하고, 결정론적 코드가 처분한다.**

경계 판별 질문: "이 규칙이 확률적으로 지켜져도 되는가, 결정론적으로 보장되어야 하는가?" — 전자는 프롬프트([skills](../skills) 저장소), 후자는 여기.

## 계층 구조

| 계층 | 역할 | 구현 |
| --- | --- | --- |
| 게이트 (hooks/) | 에이전트 행동의 사전 차단·사후 검증 | Claude Code / Codex `PreToolUse` / `PostToolUse` / `Stop` hooks |
| 검증 스크립트 (scripts/) | 스킬이 호출하는 결정론적 검증기 | 단독 실행 가능한 스크립트 |
| 측정 (scripts/) | 상시 감시 지표 — LLM 없이 싸고 결정론적으로 | 해시 비교, 집합 유사도 |

## 로드맵

1. **skill-invocation-verifier** — `Stop` hook. transcript를 검사해 라우팅 규칙이 요구한 Skill/MCP 호출이 누락되면 턴 종료를 차단. "스킬 무시" 문제의 직접 해법. ✅ 구현됨
2. **context-watermark** — `Stop` hook. transcript의 토큰 usage로 컨텍스트 사용률 계산, 임계(기본 90%) 초과 시 현재 턴에서 성공적으로 작성된 handoff 산출물(`_workspace/<task>/handoff.md`)을 구조 lint한 뒤에만 턴 종료 허용. 컴팩션으로 작업 맥락이 증발하기 전에 파일로 대피. ✅ 구현됨
3. **handoff-reinject** — `SessionStart(compact)` hook. 컴팩션 직후 현재 세션에 바인딩된 handoff.md(기본 24h 이내, 8k 상한)를 세션 컨텍스트에 자동 주입. 다른 작업의 최신 파일이나 프로젝트 밖 symlink는 주입하지 않음. ✅ 구현됨
4. **readiness-gate** — `task.md` 모호함과 `implementation.md` 준비도를 독립 판정하고, 문서 해시·근거 인용·AC 추적성을 코드가 재검증. 분해된 자식은 별도 설계를 축약하지 않고 ready Full 부모의 P/AC 범위를 상속한다. 유효한 readiness proof에 session이 바인딩되기 전에는 명시적 문서 예외 외의 직접 프로젝트 편집을 차단. Claude/Codex 설정 포함. ✅ 구현됨
5. **scenario-gate** — AC/P에서 만든 관찰 가능한 Scenario Contract를 독립 검토·해시 고정하고, 프로젝트 언어의 실제 runner 결과를 current source/config에 결합. 자식은 부모 시나리오를 상속하고 local refinement와 parent promotion candidate를 기록한다. advisory → critical → full enforcement와 Stop/CI 완료 gate 포함. ✅ 구현됨
6. **stage1-autorun** — `PostToolUse(Write|Edit)` hook. 편집마다 lint/test 자동 실행 — 기계 검증이 에이전트 선의에 의존하지 않게.
7. **artifact-lint** — 산출물 구조 lint (1층/$0). 가중합 점수 + 차원별 하한선(floor), handoff/task/implementation 타입. Full-tier implementation은 번호형 수도코드와 제어 흐름 Mermaid가 필수다. watermark와 연동돼 "빈껍데기 handoff"로는 block을 통과 못 함. 2층(LLM rubric)은 [docs/rubric-judge.md](docs/rubric-judge.md) 템플릿. ✅ 구현됨
8. **scope-gate** — `PreToolUse` hook. dispatch packet의 `allowed_scope`를 읽어 범위 밖 편집 차단.

## artifact-lint 사용법

```bash
python3 scripts/artifact_lint.py --type handoff _workspace/my-task/handoff.md [--json]
python3 scripts/artifact_lint.py --type task _workspace/my-task/task.md [--json]
python3 scripts/artifact_lint.py --type implementation _workspace/my-task/implementation.md [--json]
# PASS score=1.0 (threshold 0.8)  — exit 0 / FAIL exit 1 / 오류 exit 2
```

- 점수 = 검사 항목 가중합. **floor 항목**은 누락 시 점수 무관 FAIL — implementation의 fenced `P1`/`P2` 수도코드와 제어 흐름 Mermaid도 0점 floor로 강제
- 잡는 것: 섹션 부재·빈 섹션·파일 경로 인용 없음. 못 잡는 것: 내용이 틀림 — 그건 2층 [rubric judge](docs/rubric-judge.md) 몫
- 수치화의 Goodhart 위험: 1층 점수는 "미달 차단"용이지 품질 보증이 아님. 최종 신뢰는 하류 결과(컴팩션 후 재질문 없이 이어졌는가)

## readiness-gate 사용법

`task.md`와 `implementation.md`를 만든 뒤 `artifact-judge`의 readiness 절차를 실행합니다. 상세 차원과 JSON 스키마는 [readiness assessment](docs/readiness-assessment.md)에 있습니다.

```bash
python3 scripts/readiness_gate.py --template _workspace/my-task
# 독립 판정 결과를 _workspace/my-task/assessment.json으로 작성
python3 scripts/readiness_gate.py _workspace/my-task
```

- `task.md`: outcome/constraints/acceptance/grounding의 가중 명확도로 ambiguity를 계산하고 차원별 floor를 적용
- `implementation.md`: task의 모든 `AC-<number>` 추적 + 결정 종결성/변경 구체성/위험 대응으로 readiness 계산
- Full root에서 `implementation.md`를 편집한 세션은 턴 종료 전에 `flow-design` 호출이 필요하고, structural lint가 번호형 수도코드와 제어 흐름 Mermaid 블록을 필수 검사
- assessment의 합계는 신뢰하지 않음 — validator가 원문 SHA-256, exact evidence, score 범위, floor, 가중합을 다시 계산
- 유효한 Full assessment 또는 inherited readiness manifest를 쓴 session만 직접 프로젝트 `Write`/`Edit`/`apply_patch` 가능; 확장자 없는 빌드 파일과 미등록 확장자도 기본 보호되며 관련 문서 변경 즉시 stale 처리
- 잘못된 readiness proof를 쓰면 PostToolUse가 validator의 첫 진단들을 즉시 반환하며 session을 바인딩하지 않음
- Claude의 canonical `artifact-judge`를 `.agents/skills` 상대 symlink로 공유해 두 runtime의 절차가 함께 갱신됨
- 검증된 `_workspace/**`, `.md`/`.rst`/`.txt`, README·LICENSE 등 명시적 문서 파일만 예외; 외부·심링크·혼합 패치의 보호 대상은 차단
- 입력·target·project root를 확정할 수 없는 hook 이벤트는 stderr 진단 후 fail-open하고, 보호 대상이 확정된 뒤 marker/readiness 실패는 fail-closed
- readiness와 handoff의 session marker는 공통 원자적 JSON 저장소를 사용하며, 검증 캐시는 실측 병목이 없어 도입하지 않음

### 분해된 자식 작업

작업 크기는 tier 강등 근거가 아니다. 작은 자식은 Full 문서와 LLM 판정을 복제하는 대신 직접 Full 부모의 검증된 흐름을 상속한다.

```bash
# 부모는 먼저 일반 Full readiness를 통과해야 한다.
python3 scripts/readiness_gate.py _workspace/parent-task

# 자식 task.md 작성 후 bound template 생성
python3 scripts/readiness_gate.py \
  --inherit-from _workspace/parent-task \
  _workspace/child-task > _workspace/child-task/inherited-readiness.json

# flow_refs와 acceptance_refs를 채운 뒤 검증
python3 scripts/readiness_gate.py _workspace/child-task
```

`inherited-readiness.json`의 `flow_refs`는 부모 `implementation.md`의 실제 `P<number>`, `acceptance_refs`는 부모와 자식 `task.md`에 모두 있는 `AC-<number>`여야 한다. 부모는 다른 inherited child가 아닌 직접 Full task여야 하며, 자식은 자체 `implementation.md`나 `assessment.json`을 두지 않는다. 유효한 manifest를 작성한 세션은 자식에 bind되고, 부모 또는 자식 문서가 바뀌면 다음 보호 편집에서 fail-closed한다. 보호된 소스 편집에는 Full assessment 또는 inherited Full proof가 필요하므로 독립 Fast 판정으로 우회할 수 없다.

## scenario-gate 사용법

Scenario gate는 opt-in이다. 저장소 루트에 `.agent-gate/scenario-gate.json`이 없으면 기존 readiness 동작이 그대로 유지된다. 설정은 strict JSON이며 runner는 shell 문자열이 아니라 argv 배열이다.

```json
{
  "schema_version": 1,
  "mode": "advisory",
  "runners": {
    "integration": {
      "command": ["go", "test", "./tests/integration/..."],
      "format": "exit-code",
      "timeout_seconds": 300,
      "max_output_bytes": 1048576
    }
  }
}
```

Full 부모는 `scenario-design`으로 `_workspace/<task>/scenario-contract.json`을 만들고, 자식은 부모 ID를 상속하면서 `scenario-overlay.json`에 local scenario와 `child|parent-candidate` ownership을 기록한다. 구현 세부나 함수 호출 순서가 아니라 외부에서 관찰되는 Given/When/Then만 허용한다.

```bash
# 독립 검토용 current hash template
python3 scripts/scenario_gate.py review-template \
  _workspace/<task> --project-root . > /tmp/scenario-review-template.json

# artifact-judge의 독립 결과를 scenario-review.json에 기록한 뒤
python3 scripts/scenario_gate.py readiness _workspace/<task> --project-root .

# 저장소가 선택한 언어/프레임워크 runner 실행 및 결과 원자 기록
python3 scripts/scenario_gate.py run _workspace/<task> --project-root . --json

# Stop hook/CI와 동일한 완료 판정
python3 scripts/scenario_gate.py completion _workspace/<task> --project-root . --json
```

| mode | 편집 전 | 완료 시 |
| --- | --- | --- |
| `advisory` | 결함을 경고하고 허용 | stale/실패를 경고하고 허용 |
| `critical-enforce` | current contract/review 필수 | `risk: critical` 결과만 차단, standard 실패는 경고 |
| `enforce` | current contract/review 필수 | 모든 scenario와 미해결 `parent-candidate`를 차단 |

Runner는 exit-code, agent-gate JSON, JUnit XML을 지원하고 shell 없이 실행한다. 상속 환경은 PATH·locale·temp·주요 언어 toolchain 경로로 제한한다. 결과는 effective scenario canonical hash, exact runner config hash, Git HEAD/경로 제한 tracked diff, untracked 파일의 내용이 아닌 path/mode/size/mtime metadata에 결합한다. 따라서 로컬 freshness는 우발적 stale 방지이고, CI의 clean commit identity가 가장 강한 증거다. OS 수준 network sandbox는 제공하지 않으므로 runner 설정은 신뢰된 저장소에서만 사용하고 production credential을 주입하지 않는다.

토큰 비용은 contract/overlay가 바뀔 때의 독립 검토에만 발생한다. Hook과 runner 판정은 LLM을 호출하지 않으며, 같은 task/flow/subject hash의 review는 재사용한다. 자식은 부모 시나리오를 다시 생성하지 않고 할당된 ID만 상속한다.

## context-watermark 사용법

```json
{
  "type": "command",
  "command": "python3",
  "args": ["${CLAUDE_PROJECT_DIR}/hooks/context_watermark.py"]
}
```

를 Stop hook에 추가. 옵션: `--window 200000` (컨텍스트 윈도우), `--threshold 0.9` (차단 임계).

- 사용량 = 마지막 assistant 메시지의 `input_tokens + cache_read + cache_creation` (transcript 실측)
- 만족 조건: 현재 턴의 성공한 `Write` 결과 + 프로젝트 내부의 정확한 `handoff.md` 경로 + 구조 lint 통과
- 만족한 경로는 `_workspace/.handoff-sessions/`에 session id의 SHA-256 marker로 기록하며, reinject는 이 marker를 우선 사용
- 임계 미만 / handoff 작성됨 / usage 없음 / 내부 오류 → 통과 (fail-open, verifier와 동일 정책)
- 사용률 확인: `python3 hooks/context_watermark.py --check <transcript.jsonl>`

## skill-invocation-verifier 사용법

라우팅 규칙을 프로젝트의 `.claude/skill-rules.json`에 선언합니다 (스키마는 [`hooks/rules.example.json`](hooks/rules.example.json) 참고):

- `when` — 트리거 조건 (AND 결합): `prompt_pattern`(현재 턴 사용자 프롬프트 regex), `tool` + `input_pattern`(현재 턴 tool 호출 매칭)
- `require` — `skill`(Skill 도구로 해당 스킬 호출) 또는 `tool_pattern`(도구 이름 regex, MCP 강제용)
- 트리거 범위는 **현재 턴**, 만족 범위는 **전 세션** — 한 번 호출된 스킬 지침은 세션에 잔존하므로

`~/.claude/settings.json` (또는 프로젝트 `.claude/settings.json`)에 등록:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3",
            "args": [
              "${CLAUDE_PROJECT_DIR}/hooks/skill_invocation_verifier.py",
              "--rules",
              "${CLAUDE_PROJECT_DIR}/.claude/skill-rules.json"
            ]
          }
        ]
      }
    ]
  }
}
```

위반 시 `{"decision": "block", "reason": ...}`을 출력해 Claude가 턴을 끝내지 못하고 누락된 스킬을 호출하게 만듭니다. 재Stop에서도 transcript를 다시 평가하므로 요구 호출이 실제로 나타나기 전에는 계속 차단합니다.

- 검증기 내부 오류는 전부 fail-open (exit 0 + stderr) — 검증기 버그가 세션을 잠그면 안 됨
- fail-open의 사각지대는 감사 모드로 보완: `python3 hooks/skill_invocation_verifier.py --rules <rules.json> --check <transcript.jsonl>` (위반 시 exit 1)
- Claude Code 자체의 연속 Stop-hook block 상한은 외부 런타임 한계이며, 이 저장소가 무한 차단을 보장하지는 않음

### 검증 상태

- 전체 단위 테스트 실행: `python3 -m unittest discover -s tests`
- 실 세션 transcript 대상 `--check` 스모크: 트리거 발동·만족·위반 세 경로 모두 확인
- 실 세션 Stop hook 스모크 통과 (claude 2.1.215): block 사유 표시 → 누락 스킬 자동 호출 → 재Stop 통과, block 루프 없음

## 참고

설계 배경: [Ouroboros 코드 분석 — 왜 프롬프트가 아니라 코드인가](https://codex.epril.com/wiki/ouroboros-code-analysis-why-code-over-prompts)

- [Claude Code hooks reference](https://code.claude.com/docs/en/hooks)
- [Codex hooks reference](https://learn.chatgpt.com/docs/hooks)
