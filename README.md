# agent-gate

AI 코딩 에이전트를 위한 결정론적 강제 계층입니다. 프롬프트(스킬, CLAUDE.md)는 에이전트가 *따르려 노력*하는 지시이고, 이 저장소의 hook과 스크립트는 *구조적으로 강제*되는 규칙입니다.

설계 원칙 하나로 요약됩니다: **LLM은 제안하고, 결정론적 코드가 처분한다.**

경계 판별 질문: "이 규칙이 확률적으로 지켜져도 되는가, 결정론적으로 보장되어야 하는가?" — 전자는 프롬프트([skills](../skills) 저장소), 후자는 여기.

## 계층 구조

| 계층 | 역할 | 구현 |
| --- | --- | --- |
| 게이트 (hooks/) | 에이전트 행동의 사전 차단·사후 검증 | Claude Code `PreToolUse` / `PostToolUse` / `Stop` hooks |
| 검증 스크립트 (scripts/) | 스킬이 호출하는 결정론적 검증기 | 단독 실행 가능한 스크립트 |
| 측정 (scripts/) | 상시 감시 지표 — LLM 없이 싸고 결정론적으로 | 해시 비교, 집합 유사도 |

## 로드맵

1. **skill-invocation-verifier** — `Stop` hook. transcript를 검사해 라우팅 규칙이 요구한 Skill/MCP 호출이 누락되면 턴 종료를 차단. "스킬 무시" 문제의 직접 해법. ✅ 구현됨
2. **context-watermark** — `Stop` hook. transcript의 토큰 usage로 컨텍스트 사용률 계산, 임계(기본 90%) 초과 시 handoff 산출물(`_workspace/<task>/handoff.md`) 작성을 강제한 뒤에만 턴 종료 허용. 컴팩션으로 작업 맥락이 증발하기 전에 파일로 대피. ✅ 구현됨
3. **handoff-reinject** — `SessionStart(compact)` hook. 컴팩션 직후 최신 handoff.md(기본 24h 이내, 8k 상한)를 세션 컨텍스트에 자동 주입. watermark가 대피시킨 판단("이 작업은 저가치라 스킵" 같은 사용자 교정)을 lossy 컴팩션 요약과 무관하게 결정론적으로 회수. ✅ 구현됨
4. **stage1-autorun** — `PostToolUse(Write|Edit)` hook. 편집마다 lint/test 자동 실행 — 기계 검증이 에이전트 선의에 의존하지 않게.
5. **artifact-lint** — 산출물 구조 lint (1층/$0). 가중합 점수 + 차원별 하한선(floor), handoff 타입부터. watermark와 연동돼 "빈껍데기 handoff"로는 block을 통과 못 함. 2층(LLM rubric)은 [docs/rubric-judge.md](docs/rubric-judge.md) 템플릿. ✅ 구현됨
6. **scope-gate** — `PreToolUse` hook. dispatch packet의 `allowed_scope`를 읽어 범위 밖 편집 차단.

## artifact-lint 사용법

```bash
python3 scripts/artifact_lint.py --type handoff _workspace/my-task/handoff.md [--json]
# PASS score=1.0 (threshold 0.8)  — exit 0 / FAIL exit 1 / 오류 exit 2
```

- 점수 = 검사 항목 가중합. **floor 항목**(결정, 다음 단계)은 누락 시 점수 무관 FAIL — 한 차원의 우수함이 다른 차원의 결핍을 가리는 것 방지
- 잡는 것: 섹션 부재·빈 섹션·파일 경로 인용 없음. 못 잡는 것: 내용이 틀림 — 그건 2층 [rubric judge](docs/rubric-judge.md) 몫
- 수치화의 Goodhart 위험: 1층 점수는 "미달 차단"용이지 품질 보증이 아님. 최종 신뢰는 하류 결과(컴팩션 후 재질문 없이 이어졌는가)

## context-watermark 사용법

```json
{ "type": "command", "command": "python3 /path/to/agent-gate/hooks/context_watermark.py" }
```

를 Stop hook에 추가. 옵션: `--window 200000` (컨텍스트 윈도우), `--threshold 0.9` (차단 임계).

- 사용량 = 마지막 assistant 메시지의 `input_tokens + cache_read + cache_creation` (transcript 실측)
- 만족 조건: 세션 내 파일명에 `handoff`가 포함된 Write 호출 존재 — 상태 파일 없이 transcript만으로 판정
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
            "command": "python3 /Users/imtaebin/Documents/codes/agent-gate/hooks/skill_invocation_verifier.py"
          }
        ]
      }
    ]
  }
}
```

위반 시 `{"decision": "block", "reason": ...}`을 출력해 Claude가 턴을 끝내지 못하고 누락된 스킬을 호출하게 만듭니다. 안전장치:

- `stop_hook_active`면 무조건 통과 — block 무한 루프 방지
- 검증기 내부 오류는 전부 fail-open (exit 0 + stderr) — 검증기 버그가 세션을 잠그면 안 됨
- fail-open의 사각지대는 감사 모드로 보완: `python3 hooks/skill_invocation_verifier.py --rules <rules.json> --check <transcript.jsonl>` (위반 시 exit 1)

### 검증 상태

- 단위 테스트 10건 통과 (`python3 -m unittest discover -s tests`)
- 실 세션 transcript 대상 `--check` 스모크: 트리거 발동·만족·위반 세 경로 모두 확인
- 실 세션 Stop hook 스모크 통과 (claude 2.1.215): block 사유 표시 → 누락 스킬 자동 호출 → 재Stop 통과, block 루프 없음

## 참고

설계 배경: [Ouroboros 코드 분석 — 왜 프롬프트가 아니라 코드인가](https://codex.epril.com/wiki/ouroboros-code-analysis-why-code-over-prompts)
