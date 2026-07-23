# agent-gate

AI 코딩 에이전트를 위한 결정론적 강제 계층입니다. 프롬프트(스킬, CLAUDE.md)는 에이전트가 *따르려 노력*하는 지시이고, 이 저장소의 hook과 스크립트는 *구조적으로 강제*되는 규칙입니다.

설계 원칙 하나로 요약됩니다: **LLM은 제안하고, 결정론적 코드가 처분한다.**

경계 판별 질문: "이 규칙이 확률적으로 지켜져도 되는가, 결정론적으로 보장되어야 하는가?" — 전자는 프롬프트([skills](../skills) 저장소), 후자는 여기.

## 계층 구조

| 계층 | 역할 | 구현 |
| --- | --- | --- |
| 게이트 (hooks/) | 에이전트 행동의 사전 차단·사후 검증 | Claude Code / Codex lifecycle hooks (`PreCompact` 포함) |
| 검증 스크립트 (scripts/) | 스킬이 호출하는 결정론적 검증기 | 단독 실행 가능한 스크립트 |
| 측정 (scripts/) | 상시 감시 지표 — LLM 없이 싸고 결정론적으로 | 해시 비교, 집합 유사도 |

## 로드맵

1. **skill-invocation-verifier** — `Stop` hook. 결과물만으로 대체할 수 없는 독립 의미 판정 절차가 누락되면 턴 종료를 차단. 기본 정책은 명시적 산출물 평가 요청에 대한 `artifact-judge` 규칙 하나뿐이며 일반적인 skill 선택은 모델에 맡긴다. ✅ 구현됨
2. **context-watermark** — `Stop` hook에서 transcript 사용률이 임계(기본 80%)를 넘으면 handoff 작성을 요구한다. Claude와 Codex는 `PreCompact(manual|auto)`에서도 usage와 무관하게 유효한 현재-turn handoff를 최종 확인한다. Stop은 후속 작업을 요청하는 `decision:block`, PreCompact는 압축 자체를 멈추는 `continue:false`를 사용한다. ✅ 구현됨
3. **handoff-reinject** — `SessionStart(compact)` hook. 컴팩션 직후 현재 세션에 바인딩된 handoff.md(기본 24h 이내, 8k 상한)를 세션 컨텍스트에 자동 주입. 다른 작업의 최신 파일이나 프로젝트 밖 symlink는 주입하지 않음. ✅ 구현됨
4. **Design Gate** — active task의 `task.md`, 번호형 수도코드·흐름도가 있는 `implementation.md`, argv 기반 `scenario-contract.json`이 구조적으로 유효해야 보호 파일 편집을 허용. LLM 점수·session binding·부모 상속 없음. ✅ 구현됨
5. **Completion Gate** — 선언한 scenario argv를 shell 없이 실행하고 task·flow·contract·source에 바인딩된 최신 결과가 100% 통과해야 명시적 CLI/CI 완료를 허용. 일반 대화의 Stop에는 연결하지 않음. ✅ 구현됨
6. **stage1-autorun** — 미도입. 편집마다 검증을 실행하지 않고 작업 완료·CI 경계의 scenario run/completion이 실행 증거를 소유한다.
7. **artifact-lint** — 산출물 구조 lint (1층/$0). 가중합 점수 + 차원별 하한선(floor), handoff/task/implementation 타입. Full-tier implementation은 번호형 수도코드와 제어 흐름 Mermaid가 필수다. watermark와 연동돼 "빈껍데기 handoff"로는 block을 통과 못 함. 2층(LLM rubric)은 [docs/rubric-judge.md](docs/rubric-judge.md) 템플릿. ✅ 구현됨
8. **scope-gate** — 미도입. 파일 범위 DSL과 별도 hook 정책은 만들지 않고 active design과 scenario 완료 검증만 유지한다.

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

## Design Gate 사용법

`_workspace/<task>/`에 `task.md`, `implementation.md`, `scenario-contract.json`을 작성합니다. task와 implementation은 `artifact-lint`를 통과해야 하고, scenario contract는 아래 strict 구조를 사용합니다.

```json
{
  "schema_version": 1,
  "scenarios": [
    {
      "id": "S-LOGIN-SUCCESS",
      "title": "Valid credentials start a session",
      "command": ["go", "test", "./tests/integration/...", "-run", "TestLoginSuccess"],
      "given": ["a registered user"],
      "when": ["valid credentials are submitted"],
      "then": ["a session is observable"]
    }
  ]
}
```

```bash
# 구조를 확인하고 이 worktree의 active task로 지정
python3 scripts/scenario_gate.py design _workspace/<task> \
  --project-root . --activate
```

PreToolUse hook은 `_workspace/.active-task`의 구조를 매 편집 전에 다시 검사합니다. `_workspace/**`, `.md`/`.rst`/`.txt`, README·LICENSE 계열 문서는 설계 작성과 복구를 위해 예외입니다. 외부 경로·symlink·혼합 패치의 보호 대상은 차단합니다.

한 worktree에는 active task 하나만 둡니다. 병렬 작업은 별도 worktree를 사용합니다. `assessment.json`, LLM readiness 점수, session marker, child inheritance는 Design Gate에 참여하지 않습니다. `artifact-judge`는 사용자가 의미 품질 평가를 요청할 때만 선택적으로 사용합니다.

## Completion Gate 사용법

```bash
# active task의 모든 scenario command 실행 및 결과 원자 기록
python3 scripts/scenario_gate.py run --project-root . --json

# 결과만 확인하고 active task는 유지
python3 scripts/scenario_gate.py completion --project-root . --json

# 최신 100%를 확인하고 active task 해제
python3 scripts/scenario_gate.py completion --project-root . --finish --json
```

시나리오 추적 완성도는 저장된 실행에서 통과한 scenario 수를 전체 선언 수로 나눈 값이며 완료 기준은 `current=true`이면서 정확히 100%입니다. 결과는 task·implementation·contract SHA-256과 Git source fingerprint에 결합됩니다. 어느 하나라도 바뀌면 `current=false`로 차단되지만 기존 pass 수를 0으로 왜곡하지 않으며, 다시 실행해야 합니다.

각 command는 project root에서 shell 없이 argv로 실행합니다. 환경은 PATH·locale·temp·주요 언어 toolchain 경로로 제한되고, 고정 300초 timeout·1 MiB 출력 상한·process-tree 종료를 적용합니다. OS network sandbox는 제공하지 않으므로 production credential을 주입하지 않습니다. Completion Gate는 명시적 CLI/CI에서만 실행하므로 worktree의 active task가 일반 대화 종료를 차단하지 않습니다.

번들 `completion-check` skill은 프로젝트 파일을 변경한 구현 작업의 최종 완료 보고 직전에 위 두 명령을 실행하도록 에이전트에게 안내합니다. 일반 대화·설명·상태 확인·계획·읽기 전용 리뷰에는 트리거하지 않으며 Stop hook이나 verifier rule로 강제하지 않습니다.

## context-watermark 사용법

```json
{
  "type": "command",
  "command": "python3",
  "args": ["${CLAUDE_PROJECT_DIR}/hooks/context_watermark.py"]
}
```

를 Stop hook에 추가. 옵션: `--window 200000` (컨텍스트 윈도우), `--threshold 0.8` (차단 임계). Claude와 Codex 설정은 같은 명령을 `PreCompact`의 `manual|auto`에도 연결한다.

- 사용량 = 마지막 assistant 메시지의 `input_tokens + cache_read + cache_creation` (transcript 실측)
- 만족 조건: 현재 턴의 성공한 `Write` 결과 + 프로젝트 내부의 정확한 `handoff.md` 경로 + 구조 lint 통과
- 만족한 경로는 `_workspace/.handoff-sessions/`에 session id의 SHA-256 marker로 기록하며, reinject는 이 marker를 우선 사용
- Stop에서는 임계 미만·usage 없음이면 통과한다. PreCompact는 usage가 없어도 handoff를 확인하며, 읽을 수 없는 입력·내부 오류만 fail-open한다.
- 차단 출력은 host가 아니라 lifecycle 의미에 따라 선택한다. Stop은 Claude와 Codex 모두 `decision:block`과 `reason`으로 에이전트가 handoff를 작성하게 하고, PreCompact는 `continue:false`로 압축을 중단한다.
- 사용률 확인: `python3 hooks/context_watermark.py --check <transcript.jsonl>`

## skill-invocation-verifier 사용법

라우팅 규칙을 프로젝트의 `.claude/skill-rules.json`에 선언합니다 (스키마는 [`hooks/rules.example.json`](hooks/rules.example.json) 참고):

이 저장소의 기본 규칙은 일반적인 coding/debugging/flow skill 선택을 강제하지 않는다. 구조나 실행 결과로 검증할 수 없는 독립 `artifact-judge` 절차만 강제하며, 나머지 예시는 downstream 저장소가 명시적으로 필요할 때 opt-in한다.

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
