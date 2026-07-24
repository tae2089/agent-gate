# agent-loop

AI 코딩 에이전트를 위한 결정론적 **Loop Engineering** 플랫폼입니다. 프롬프트(스킬, AGENTS.md)는 에이전트가 *따르려 노력*하는 지시이고, 이 저장소의 engine, gate, hook은 상태 전이와 완료 조건을 *구조적으로 판정*합니다.

설계 원칙 하나로 요약됩니다: **LLM은 제안하고, 결정론적 코드가 처분한다.**

## 도메인 모델

| 용어 | 책임 |
| --- | --- |
| **Agent Loop** | 여러 피드백 루프를 제공하는 제품과 플러그인 |
| **Loop Engine** | 상태 전이, iteration budget, 원자적 기록을 담당하는 공통 결정론적 커널 |
| **Loop Pack** | phase, 입력 artifact, 평가·종료 정책을 소유하는 구체적 루프 |
| **Main Loop** | 사용자 목표·root scope·권한·budget·phase·Subloop 선택·최종 Completion을 소유하는 루프 |
| **Subloop** | Main이 위임한 범위와 budget 안에서 전문 피드백을 반환하는 자식 실행 |
| **Gate** | evidence가 전이를 허용하는지 결정하는 재사용 가능한 transition guard |

`artifact-lint`, context watermark, handoff reinjection은 각각 validator와 lifecycle support이며 Loop Pack으로 포장하지 않습니다.
구현된 pack과 후보의 convergence 기준은 [`docs/loop-catalog.md`](docs/loop-catalog.md)에 정리합니다.

## 계층 구조

| 계층 | 역할 | 구현 |
| --- | --- | --- |
| Loop Engine (scripts/) | pack 공통 상태 전이·budget·원자 기록 | `loop_engine.py` |
| Main Loop | root 실행과 한 번에 하나의 자식 호출·결과 수용 | 초기 Main: `evolution-loop` |
| Subloop Pack (skills/ + scripts/) | bounded 전문 피드백과 공통 결과 계약 | `assurance-loop`, `debug-loop`, `ci-repair-loop`, `research-adoption-loop` |
| Gate (hooks/ + scripts/) | 에이전트 행동의 사전 차단과 evidence 기반 전이 허용 | Design Gate, Completion |
| Lifecycle support (hooks/) | 선택적 verifier·context 보존 | opt-in `Stop`·`PreCompact`·`SessionStart` |
| 검증 스크립트 (scripts/) | 스킬이 호출하는 결정론적 검증기 | 단독 실행 가능한 스크립트 |
| 측정 (scripts/) | 상시 감시 지표 — LLM 없이 싸고 결정론적으로 | 해시 비교, 집합 유사도 |

## 기능 구성

핵심은 두 가지입니다.

1. **Design Gate** — active task의 `task.md`, 번호형 수도코드·흐름도가 있는 `implementation.md`, argv 기반 `scenario-contract.json`이 구조적으로 유효해야 지원되는 direct-edit 도구의 보호 파일 편집을 허용합니다.
2. **Completion validation** — 선언한 scenario argv를 shell 없이 실행하고 task·flow·contract·Git 작업 트리에 바인딩된 최신 결과가 100%인지 명시적 local completion 명령으로 확인합니다. 일반 대화의 Stop에는 연결하지 않습니다.

다음 lifecycle 지원 기능도 번들되지만 기본 hook manifest에는 연결하지 않습니다. 필요한 프로젝트만 **opt-in**하며 두 Gate의 판정에는 참여하지 않습니다.

- **skill-invocation-verifier** — 명시적 artifact 평가 요청에 필요한 `artifact-judge` 호출을 Stop에서 확인할 수 있습니다.
- **context-watermark / handoff-reinject** — 컨텍스트 임계점의 handoff 작성과 compaction 이후 재주입을 함께 활성화할 수 있습니다.
- **artifact-lint** — task, implementation, handoff의 구조를 결정론적으로 검사합니다.
- **evolution-loop** — Main Loop로서 사용자 요청 하나와 root scope·권한·budget을 소유하고 필요한 Subloop를 호출합니다.
- **ci-repair-loop** — 사용자가 지정한 failing check를 `Inspect → Repair → Verify`로 반복하고 fresh 100% Completion일 때만 `checks-green`으로 종료합니다.
- **assurance-loop** — 요구사항 적합성, 누락·과잉 구현, 실패·경계·호환성, 모듈 품질, 복잡도, 회귀 방지를 평가합니다.
- **debug-loop** — `Frame → Reproduce → Diagnose → Fix → Verify`로 관찰된 실패의 원인을 증명하고 선택적으로 수정합니다.
- **research-adoption-loop** — `Frame → Requirements Gate → Research → Evidence Grade → Prototype → Verification → Adopt/Reject`로 진행하며, 요구사항 품질·근거 확실성·저장소 적합성·프로토타입 결과를 분리해 기록합니다.

Standalone Pack은 하나의 root 실행 포인터 `_workspace/.active-run`을
공유합니다. Evolution Main이 호출한 Subloop는
`_workspace/<main>/subloops/<invocation-id>/` 아래에서만 실행하고 전역
포인터를 만들지 않습니다. 부모 scope·권한·budget을 확장할 수 없으며
push·publish·merge·deploy 권한을 받지 않습니다. 결과는
`completed`, `changes-requested`, `needs-decision`, `blocked`,
`budget-exhausted` 중 하나입니다. Subloop는 다른 Subloop를 호출하지 않고,
Main만 다음 호출과 최종 Completion을 결정합니다.

`stage1-autorun`과 별도 `scope-gate`는 도입하지 않았습니다. Completion의 CI 사용은 task artifact를 CI에 제공하고 명령을 separately wired한 downstream 구성에서만 가능합니다.

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

PreToolUse hook은 `_workspace/.active-task`의 구조를 지원되는 direct-edit 이벤트마다 다시 검사합니다. 기존 정책은 알려진 소스 확장자만 보호할 때 빠지던 Makefile·Dockerfile·SQL·Proto 등을 포함하기 위해 프로젝트 파일을 기본 보호하고, 일반 문서 편집과 설계 복구를 허용하도록 `_workspace/**`, `.md`/`.rst`/`.txt`, README·LICENSE 계열만 예외로 두었습니다. 따라서 skill이나 정책처럼 행동을 정의하는 Markdown도 현재 suffix 규칙상 예외입니다.

외부 경로·symlink·문서와 보호 파일이 섞인 patch의 보호 대상은 차단합니다. 이 hook은 host가 전달하는 `Write`, `Edit`, `apply_patch` 계열 이벤트를 검사하며 모든 filesystem mutation을 가로채는 보안 sandbox는 아닙니다.

한 worktree에는 active task 하나만 둡니다. 병렬 작업은 별도 worktree를 사용합니다. `assessment.json`, LLM readiness 점수, session marker, child inheritance는 Design Gate에 참여하지 않습니다. `artifact-judge`는 사용자가 의미 품질 평가를 요청할 때만 선택적으로 사용합니다.

## Completion Gate 사용법

```bash
# active task의 모든 scenario command 실행 및 결과 원자 기록
python3 scripts/scenario_gate.py run --project-root . --json

# 결과만 확인하고 active task는 유지
python3 scripts/scenario_gate.py completion --project-root . --json

# 최신 100%를 확인하고 active task 해제
python3 scripts/scenario_gate.py completion --project-root . --finish --json

# 중단·차단·budget 소진 시 Completion 주장 없이 정확한 active task만 해제
python3 scripts/scenario_gate.py release _workspace/<task> \
  --project-root . --json
```

시나리오 추적 완성도는 저장된 실행에서 통과한 scenario 수를 전체 선언 수로 나눈 값이며 완료 기준은 `current=true`이면서 정확히 100%입니다. 결과는 task·implementation·contract SHA-256과 `_workspace/**`를 제외한 **whole worktree** Git source fingerprint에 결합됩니다. untracked file도 내용으로 fingerprint하므로 크기와 timestamp가 같아도 내용이 바뀌면 stale입니다. 관련 없는 작업 트리 변경도 결과를 stale로 만들 수 있습니다. 어느 하나라도 바뀌면 `current=false`로 차단되지만 기존 pass 수를 0으로 왜곡하지 않으며, 다시 실행해야 합니다.

각 command는 project root에서 shell 없이 argv로 실행합니다. 환경은 PATH·locale·temp·주요 언어 toolchain 경로로 제한되고, 고정 300초 timeout·1 MiB 출력 상한·process-tree 종료를 적용합니다. OS network sandbox는 제공하지 않으므로 production credential을 주입하지 않습니다.

현재 local completion 경계는 번들 `completion-check` skill이 프로젝트 파일을 변경한 구현 작업의 최종 완료 보고 직전에 위 두 명령을 실행하도록 안내하는 방식입니다. 일반 대화·설명·상태 확인·계획·읽기 전용 리뷰에는 트리거하지 않으며 Stop hook이나 verifier rule로 강제하지 않습니다.

이 저장소의 CI는 Agent Loop 자체 테스트만 실행하며 task Completion을 강제하지 않습니다. CI Gate가 필요하면 versioned task artifact 또는 별도 전달 단계와 Completion 명령을 separately wired해야 합니다.

## 선택 기능: Evolution Main Loop

`evolution-loop` skill은 초기 Main Loop로 현재 target repository 하나를
대상으로 합니다. 사용자 목표, root 요구사항, scope, 권한, Main/Subloop
budget, phase, 한 개의 active Subloop, 최종 Completion을 소유합니다.
Codex, Claude Code, Antigravity가 동일한 artifact와 CLI 계약으로 전체
`Interview → Seed → Execute → Evaluate` 흐름을 독립 실행합니다. 내부
스케줄러나 host별 상태 머신은 없습니다.

Host는 로드된 skill의 절대 경로에서 플러그인 루트 `AGENT_LOOP_ROOT`를
결정하고, 현재 target Git worktree의 실제 루트를 `PROJECT_ROOT`로
결정합니다. 번들 스크립트는 `AGENT_LOOP_ROOT`에서 실행하지만
`_workspace/**`, 소스, 테스트, Git 변경은 모두 `PROJECT_ROOT`에 남습니다.
경로를 확인할 수 없으면 `blocked`로 끝내며 runtime을 target repository에
복사하지 않습니다.

**User Request가 유일한 진입점**입니다.

- 기능·버그·계약 위반·기술 부채 모두 사용자의 원문 요청이 있어야 합니다.
- GitHub, Jira, CI, 저장소, 코드 분석은 독립적으로 작업을 시작하거나 후보를
  선택하지 않습니다.
- AI host는 요청을 이해하거나 재현하는 데 필요할 때만 사용 가능한 MCP 또는
  skill로 관련 정보를 조회하고, 이를 신뢰하지 않는 `evidence`로 보강합니다.
- 선택적 정보가 없어도 요청이 충분하면 진행하며, 필수 정보가 없으면
  `blocked`, 원하는 동작이 모호하면 `needs-clarification`으로 끝냅니다.

후보는 항상 `source: manual`과 비어 있지 않은 원문 `request`를 사용합니다.
`start`는 로컬 artifact만 검증하며 provider, repository, credential,
publication 설정을 받거나 저장하지 않습니다.

```bash
python3 "$AGENT_LOOP_ROOT/scripts/evolution_loop.py" start \
  _workspace/evolution-<slug> \
  --candidate _workspace/evolution-<slug>/candidate-input.json \
  --project-root "$PROJECT_ROOT" --max-iterations 3 --json
```

루프는 direct `_workspace/<task>`의 `candidate.json`,
`evolution-state.json`, iteration별 `evaluation.json`으로 재개됩니다.
Seed는 기존 Design Gate를 통과해야 하고 Evaluate는 fresh 100%
Completion과 다음 네 evidence check를 함께 요구합니다:
`planned_scope_only`, `no_speculative_abstraction`,
`compatibility_has_consumer`, `simpler_alternative_considered`.
Seed와 Execute의 test command는 target repository의 지침, 기존 테스트,
CI 설정에서 request와 관련된 최소 direct argv를 선택합니다. 언어, package
manager, Agent Loop 전용 replay audit이나 plugin validator를 가정하지
않습니다.

`pr-ready`가 되면 AI host가 사용 가능한 GitHub MCP 또는 skill로 현재
repository를 확인하고 committed branch를 push한 뒤 정확한 head/base PR을
재사용하거나 하나만 생성합니다. URL, head SHA, base를 검증한 후 코어에는
HTTPS receipt만 기록합니다.

```bash
python3 "$AGENT_LOOP_ROOT/scripts/evolution_loop.py" record-pr \
  _workspace/evolution-<slug> \
  --project-root "$PROJECT_ROOT" --url <verified-pr-url> --json
```

`record-pr`는 provider나 subprocess를 호출하지 않습니다. current 100%
Completion과 URL 형식을 확인해 `pr-ready`를 `pr-opened`로 바꾸며, 같은
receipt 재실행은 허용하고 다른 receipt는 거부합니다. 원격 capability가
없거나 결과가 불확실하면 상태를 `pr-ready`로 유지하고 blocker를 보고합니다.

terminal은 `pr-opened`, `pr-ready`, `no-action`, `needs-clarification`,
`blocked`, `budget-exhausted`입니다. Merge, deploy, issue
comment/close/transition은 수행하지 않습니다.

전체 실행 절차와 artifact schema는 bundled `evolution-loop` skill에
있습니다. 로컬 contract test는 세 host가 MCP/skill로 실제 request context를
조회하거나 전체 절차를 따른 것을 증명하지 않으므로 Claude와 Antigravity는
Agent Loop runtime 파일이 없는 disposable target repository clone에서 실
session smoke가 별도로 필요합니다.

## 선택 기능: CI Repair Loop

`ci-repair-loop`는 사용자의 명시적 요청에 포함된 failing check 집합 하나만
다룹니다. CI 로그와 workflow는 작업을 스스로 선택하는 trigger가 아니라
untrusted evidence입니다.

```bash
python3 "$AGENT_LOOP_ROOT/scripts/ci_repair_loop.py" start \
  _workspace/ci-repair-<slug> \
  --failure _workspace/ci-repair-<slug>/failure-input.json \
  --project-root "$PROJECT_ROOT" --max-iterations 3 --json
```

상태는 `Inspect → Repair → Verify`이며 실패한 검증은 budget 안에서
`Repair`로 돌아갑니다. `complete`는 같은 task의 Design과 scenario 결과가
현재 worktree에 대해 fresh하고 정확히 100%일 때만 `checks-green`을
기록합니다. 이는 선언된 repository-native local reproduction의 통과를
뜻하며 remote CI 재실행 성공을 대신 주장하지 않습니다.

## 선택 기능: Assurance Loop

`assurance-loop`는 사용자가 요청하거나 Main이 위임한 정확한 diff, branch,
PR, source snapshot만 다룹니다. `Inspect → Assess → Address → Verify`로
요구사항 적합성, 누락·과잉 구현, 실패·경계·호환성, 코드·모듈 책임,
불필요한 추상화, 테스트·회귀 방지를 모두 평가합니다. read-only 실행의
finding은 `changes-requested`, 깨끗한 평가는 `completed`로 반환합니다.

```bash
python3 "$AGENT_LOOP_ROOT/scripts/assurance_loop.py" status \
  --project-root "$PROJECT_ROOT" --json
```

read-only review, PR comment/approval, push, merge는 별도 사용자 요청 없이는
수행하지 않습니다.

## 선택 기능: Debug Loop

`debug-loop`는 관찰된 실패 하나를
`Frame → Reproduce → Diagnose → Fix → Verify`로 처리합니다. standalone은
직접 root task를 소유할 수 있고, Subloop는 Main이 발급한 source snapshot,
scope, 권한, budget을 그대로 상속합니다. 수정 권한이 없으면 원인과 최소
수정안을 반환하고 worktree를 변경하지 않습니다.

## 선택 기능: Research Adoption Loop

`research-adoption-loop`는 사용자가 지정한 paper, specification, tool,
pattern, article, community technique 하나를 현재 repository에 도입할지
검증합니다. 흐름은
`Frame → Requirements Gate → Research → Evidence Grade → Prototype → Verification → Adopt/Reject`
입니다. Research 전에 명확성·완전성·일관성·필요성·추적성·실현 가능성·
검증 가능성·단일성을 각각 pass/fail과 findings로 평가하며, 하나라도
실패하면 `needs-clarification`으로 종료합니다.

근거 확실성은 `high/moderate/low/very-low`, 저장소 적합성과 프로토타입
결과는 각각 별도 pass/fail 축입니다. 임의의 가중치나 총점은 계산하지
않습니다.

```bash
python3 "$AGENT_LOOP_ROOT/scripts/research_adoption_loop.py" status \
  --project-root "$PROJECT_ROOT" --json

# prototype scenario 직후, cleanup 전에 반복별 증거 보존
python3 "$AGENT_LOOP_ROOT/scripts/research_adoption_loop.py" capture \
  _workspace/research-adoption-<slug> \
  --project-root "$PROJECT_ROOT" --json

# 채택된 brief만 native Evolution candidate로 변환
python3 "$AGENT_LOOP_ROOT/scripts/research_adoption_loop.py" handoff \
  _workspace/research-adoption-<slug> \
  --project-root "$PROJECT_ROOT" --json
```

adopt와 reject 모두 repository를 fresh 100% Completion 상태로 남겨야
합니다. reject 시 loop가 작성한 prototype은 제거하거나 생성하지 않은
상태여야 합니다. 최종 domain artifact는 `requirements-assessment.json`과
`adoption-brief.json`입니다. 채택된 brief는 모든 hash와 Requirements Gate를
재검증한 뒤에만 `evolution-candidate.json`으로 변환하며 Evolution을 자동
시작하지 않습니다.

실제 운영 데이터가 쌓이기 전에는 점수화를 보류합니다. 각 run에는 재질문 수,
scope 변경 수, 재작업 수, 첫 Completion 성공 여부만 관찰값으로 남기고,
충분한 표본 이후 상관관계를 별도 검토해 보정합니다.

## 선택 기능: context preservation

Watermark는 handoff를 검사하고 session marker를 만들며, reinject는 그 marker로 compaction 이후 동일 handoff를 찾습니다. 따라서 **Enable watermark and reinject together**. Claude/Codex 프로젝트 설정에는 다음 세 lifecycle entry를 함께 추가합니다.

```json
{
  "hooks": {
    "Stop": [{
      "hooks": [{
        "type": "command",
        "command": "python3",
        "args": ["${CLAUDE_PROJECT_DIR}/hooks/context_watermark.py"]
      }]
    }],
    "PreCompact": [{
      "matcher": "manual|auto",
      "hooks": [{
        "type": "command",
        "command": "python3",
        "args": ["${CLAUDE_PROJECT_DIR}/hooks/context_watermark.py"]
      }]
    }],
    "SessionStart": [{
      "matcher": "compact",
      "hooks": [{
        "type": "command",
        "command": "python3",
        "args": ["${CLAUDE_PROJECT_DIR}/hooks/handoff_reinject.py"]
      }]
    }]
  }
}
```

옵션: `--window 200000` (컨텍스트 윈도우), `--threshold 0.85` (차단 임계).

- 사용량 = 마지막 assistant 메시지의 `input_tokens + cache_read + cache_creation` (transcript 실측)
- 만족 조건: 현재 턴의 성공한 `Write` 결과 + 프로젝트 내부의 정확한 `handoff.md` 경로 + 구조 lint 통과
- 만족한 경로는 `_workspace/.handoff-sessions/`에 session id의 SHA-256 marker로 기록하며, reinject는 이 marker를 우선 사용
- Stop에서는 임계 미만·usage 없음이면 통과한다. PreCompact는 usage가 없어도 handoff를 확인하며, 읽을 수 없는 입력·내부 오류만 fail-open한다.
- 차단 출력은 host가 아니라 lifecycle 의미에 따라 선택한다. Stop은 Claude와 Codex 모두 `decision:block`과 `reason`으로 에이전트가 handoff를 작성하게 하고, PreCompact는 `continue:false`로 압축을 중단한다.
- 사용률 확인: `python3 hooks/context_watermark.py --check <transcript.jsonl>`

## 선택 기능: skill-invocation-verifier

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

Hook 설정을 바꾼 뒤 host가 이전 manifest를 cache하고 있다면 plugin을 reload하거나 새 세션을 시작합니다. Codex에서는 `/reload-plugins`를 사용할 수 있습니다.

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
