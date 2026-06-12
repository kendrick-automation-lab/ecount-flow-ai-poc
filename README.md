# ERP (이카운트) × 협업툴 (Flow) — AI 업무 자동화 PoC

> 가상의 **건축자재 중소 제조·유통 기업**을 가정하고, 이카운트 ERP 와 Flow 협업툴 사이에서
> 반복 업무 3종을 LLM 에이전트로 자동화하는 개념 증명 (PoC).
> **전 과정 더미 환경** — 실제 회사·실계정 데이터 없음.

🔗 **라이브 대시보드 데모**: https://bongbongcrypto.github.io/ecount-flow-ai-poc/

## 무엇을 자동화하나

| # | 시나리오 | 흐름 |
|---|--------|------|
| ① | **안전재고 → 발주 추천** | 재고 위반 감지 (룰) → 발주량·거래처 추천 (LLM) → 신뢰도·금액 임계값 분기 → 협업툴 승인 |
| ② | **입금 ↔ 청구서 매칭** | 거래처명 표기 변형 fuzzy 매칭 + 분할 입금 조합 추론 → 자동/승인/수동 분기 |
| ③ | **견적서 → 구매전표 초안** | 추출 (LLM) → SKU 매칭 (결정적 알고리즘) → 단가·수량 검증 (LLM Critic) → 승인 |
| + | **암묵지 룰 추출** | 협업툴 대화에서 사내 판단 기준을 구조화된 룰로 승격 (적용 전 담당자 확인) |
| + | **Jarvis Core** | 위 시나리오들을 병렬 실행하고 운영 채널에 종합 보고하는 지휘 코드 |

## 구조 — 시뮬레이터 트랙 위의 자동차

```
🏟️ 가짜 환경 (입사·도입 시 실제 API 로 교체되는 부분)
   data/*.db        가짜 ERP (거래처 50 / 품목 200 / 입출고 1000+ / 청구서·입금 각 100 — seed 로 재생성)
   src/ecount_db.py 가짜 ERP 창구 (실제 이카운트 OAPI 응답 패턴 모방)
   src/flow.py      가짜 협업툴 채널 (메시지·승인 버튼 mock)

🚗 진짜 자동화 로직 (교체 없이 그대로 쓰는 부분)
   src/agent.py     ① 발주 추천 (+ 출고 이력 없으면 STALE_DATA 가드 — ERP 입력 누락 의심 시 자동 발주 금지)
   src/payment.py   ② 입금 매칭 (rapidfuzz + itertools 조합)
   src/purchase.py  ③ 견적 추출→검증 (specialized agent chain)
   src/knowledge.py 암묵지 룰 추출
   src/decision.py  자동 / 사람 승인 / 수동 분기 규칙
   src/jarvis_core.py 병렬 지휘 + 종합 보고
```

**설계 원칙** (Anthropic *Building Effective Agents* 정렬): 지휘는 결정적 코드로, LLM 은 비정형 판단이 필요한 단계에만. SKU 매칭처럼 알고리즘이 더 정확·저렴한 곳엔 LLM 을 쓰지 않음.

## 실행

```bash
python -m venv .venv && .venv/Scripts/activate   # (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
python scripts/db_seed.py --reset                # 더미 데이터 생성

# --mock = LLM 없이 휴리스틱 데모 / .env 에 ANTHROPIC_API_KEY 넣으면 실제 LLM 모드
python -m src.main --scenario inventory --mock --confirm-approve
python -m src.main --scenario payment   --mock --approve-all
python -m src.main --scenario purchase  --mock --approve-all --quote samples/quote_confirm.txt
python -m src.main --scenario all       --mock --approve-all --limit 10   # 풀 사이클

python scripts/build_dashboard.py                # 대시보드 재생성 → docs/index.html
```

## 검증된 동작 (mock 실행)

- 입금 100건 → 자동 매칭 / 사람 승인 / 수동 검토 분기. 먼저 매칭된 입금이 청구서를 가져가면 후순위는 자동으로 수동 전환 (트랜잭션)
- 단가 40% 인상 견적 → `PRICE_DEVIATION` 플래그 → 승인 요청 / 미등록 품목 → 수동 차단
- 협업툴 대화의 "이미 발주 계획" 단서 → 중복 발주 차단 (ERP 정형 + 대화 비정형 컨텍스트 결합)
- 대화 로그에서 사내 판단 룰 3건 자동 추출 ("단가 10%↑ 시 보고" 등)

## 정직성 명시

- 이카운트·Flow 의 **공개 문서 기반 분석 + 가설 구현** — 실제 운영 환경 연동 아님
- mock 모드 검증 단계 (실 LLM 호출 검증은 API 키 셋업 후 — 토큰 로깅 필드는 코드에 준비됨)
- 더미 데이터 분포는 시나리오 검증용 설계값 — 실제 운영 분포와 다름
- 효율 개선의 정량 효과는 실측 전까지 산정하지 않음
- 개발 과정에서 **Claude Code 를 깊이 활용** (설계·검증·운영 판단은 사람)
