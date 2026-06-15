# 이카운트 ERP × Flow 협업툴 — AI 자동화 PoC (대화형 Jarvis)

> 가상의 **건축자재 중소 제조·유통 기업**을 가정하고, ERP 반복 업무 자동화 + **대화형 AI 에이전트**를 구현하고,
> **실제 외부 API(Airtable·Slack)에 연동해 가시적으로 시연**한 개념 증명(PoC). 전 과정 더미 환경 — 실회사·실계정 데이터 없음.
>
> 설계 핵심: **예측 가능한 일은 workflow, 경로를 미리 모르는 일은 agent, 독립 분석은 multi-agent** — "어디에 무엇을 쓰는지 구분".

🔗 **라이브 대시보드**: https://kendrick-automation-lab.github.io/ecount-flow-ai-poc/

---

## 한눈에 — 3 레이어

### 1) Workflow — 코드가 순서를 정함 (`src/agent.py` `payment.py` `purchase.py`)
| # | 시나리오 | 처리 |
|---|---------|------|
| 1 | 안전재고 → 발주 추천 | LLM 수량·거래처 추천 + 데이터 신선도 가드(STALE_DATA) |
| 2 | 입금 ↔ 청구서 매칭 | RapidFuzz 거래처(표기 변형) + 금액(정확/분할/부분 조합) |
| 3 | 견적서 → 구매전표 | 추출 LLM → SKU 매칭(알고리즘) → 검증 LLM(Critic) |
- 위험 액션은 `src/decision.py` 가 **자동 / 사람 승인 / 수동** 분기 (human-in-the-loop).

### 2) Orchestration (`src/jarvis_core.py` `jarvis_briefing.py`)
- **Jarvis Core** — 시나리오 1·2·3 `asyncio` 병렬 + 종합 보고.
- **멀티에이전트 아침 브리핑** — 재고·재무·채권 분석가 병렬 → 각 팀 채널 자동 발송.

### 3) Agent — LLM 이 도구를 스스로 선택 (tool-use 루프, `src/jarvis_agent.py`)
- **① Q&A** — 자연어 질문 → 읽기 도구 7종(`agent_tools.py`) 조회 → 답변 (멀티턴 영속 메모리 `conversation_memory.py`).
- **② 자율 조사** — 이상 징후 스스로 조사 → 가설+권고. **행동은 안 함 — 쓰기 도구 미제공으로 구조적 차단.**
- **관측** `AgentTrace` + **평가 하네스** `agent_eval.py`(정답 대조 채점, 5/5).

> 설계 원칙 (Anthropic *Building Effective Agents* 정렬): 지휘는 결정적 코드, LLM 은 비정형 판단이 필요한 단계에만. SKU 매칭처럼 알고리즘이 정확·저렴한 곳엔 LLM 안 씀.

---

## 실 API 연동 (가시적 시연)
| 구분 | 구현 |
|------|------|
| **데이터** | **Airtable 실 API** (`src/airtable_client.py`) — 클라우드 그리드 가시화, `--backend airtable`. 기본은 SQLite 더미. Strategy 패턴으로 교체 |
| **협업툴** | **Slack** — Tier 3 **대화형 Jarvis**(Socket Mode, `scripts/slack_jarvis_listen.py`): 채널 멘션/DM → 실시간 답변. + 멀티채널 자동 브리핑 |

> 타깃 도구(이카운트 OAPI · Flow OpenAPI)는 어댑터 패턴으로 교체 가능하게 설계 — 각 사업자/유료 플랜 게이트가 있어, 동일 패턴을 게이트 없는 Airtable·Slack 실 API 로 시연.

## 실행
```bash
python -m venv .venv && .venv/Scripts/activate    # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python scripts/db_seed.py --reset

# Workflow (--mock = LLM 없이 / ANTHROPIC_API_KEY 있으면 --mock 빼면 실제 Claude)
python -m src.main --scenario inventory --mock --confirm-approve
python -m src.main --scenario payment   --mock --approve-all
python -m src.main --scenario purchase  --mock --approve-all --quote samples/quote_confirm.txt
python -m src.main --scenario all       --mock --approve-all --limit 10   # Jarvis Core 병렬

# Agent 레이어
python -m src.main --scenario ask         --mock     # 질의응답
python -m src.main --scenario investigate  --mock     # 자율 조사 (행동 X)
python -m src.main --scenario briefing     --mock     # 멀티에이전트 브리핑
python -m src.main --scenario eval         --mock     # 평가 하네스 (정답 대조)

# 실 API (선택, .env 필요 — .env.example 참조)
python scripts/airtable_connect_test.py ; python scripts/airtable_seed.py --reset
python -m src.main --scenario ask --backend airtable --question "거래처 미수금?"
python scripts/slack_jarvis_listen.py --backend airtable   # 대화형 Jarvis (Socket Mode)

python scripts/build_dashboard.py                  # 대시보드 재생성 → docs/index.html
```

## 검증된 동작
- 시나리오 1-3 + Jarvis Core (액션 분기 / fuzzy·분할 매칭 / 단가 이상 차단 / 병렬 사이클).
- Agent: 다단계 도구 라우팅 / 자율 조사 후 가설·권고(행동 X) / 평가 5/5 / 환각 질문엔 "데이터로 확인 불가" 거부.
- 실 API: Airtable 인증+조회, Slack 대화·DM·멀티채널 발송 (실제 Claude tool-use).
- 협업툴 대화의 "이미 발주 계획" 단서 → 중복 발주 차단 (정형 + 비정형 컨텍스트 결합).

## 정직성 명시
- **PoC/데모** — production 운영 아님. 더미 "회사" 데이터 (분포는 검증용 설계값, 실 운영 분포 아님).
- 이카운트·Flow 는 **공개 문서 분석 + 어댑터 설계**, 실 API 연동 시연은 **Airtable·Slack** 으로.
- mock(휴리스틱+실제 DB) / real(실제 Claude tool-use) 둘 다 동작. 정확도(합성 5문항 5/5)는 검증용 — 실데이터는 별도.
- 시크릿(API 키)은 `.env`(gitignore)로 **로컬에만** — repo 미포함.
- 개발 과정에서 **Claude Code 를 깊이 활용** (설계·검증·운영 판단은 사람).
