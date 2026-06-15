"""④ 평가 하네스 — Q&A 에이전트의 정확도를 측정한다.

왜 평가인가 (면접 시그널):
"됩니다" 가 아니라 "이렇게 측정했고 N/M 통과" 로 말하기 위함. 에이전트는 LLM 이 도구를
고르는 비결정적 시스템이라, 회귀(regression)를 잡으려면 고정 평가셋이 필요하다.

정직한 한계 (출력 헤더에도 명시):
- **합성 데이터** (db_seed.py) 위에서의 평가다. 실제 KS 데이터 아님.
- 정답은 **DB 에서 라이브 계산** 한다 (하드코딩 X) → 시드가 바뀌어도 정답이 따라감.
- 채점 = **포함(containment) 검사**: 정답 문자열이 답변에 들어있으면 통과.
  → 측정 대상 = "도구를 옳게 골라(routing) 결과를 옳게 읽었는가(extraction)".
    실세계 판단의 질이 아니다. (real LLM 은 숫자 표기 차이로 과소평가될 수 있음 — 그래서 mock 기준이 주.)
- graceful refusal 케이스 = 답을 모를 때 환각 대신 '담당자 확인' 하는지 검증.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .agent_tools import _top_receivable_partners
from .jarvis_agent import QAAgent


@dataclass
class EvalCase:
    question: str
    expect: list[str]   # 답변에 이 문자열들이 모두 있으면 통과
    note: str


def build_eval_cases(db) -> list[EvalCase]:
    """정답을 DB 에서 라이브로 계산해 평가 케이스를 만든다."""
    breaches = db.list_safety_stock_breaches()
    breach_count = len(breaches)
    unmatched = len(db.list_pending_payments())

    cases: list[EvalCase] = [
        EvalCase("지금 안전재고 미만인 품목 몇 개야?", [str(breach_count)],
                 "재고 집계 라우팅+추출"),
        EvalCase("미매칭 입금 현황 알려줘", [str(unmatched)],
                 "입금 집계 라우팅+추출"),
    ]

    # 미수 최다 거래처 — 정확한 거래처명으로 질문, 정답 = 그 거래처 미수 총액(콤마 표기)
    top = _top_receivable_partners(db, limit=1)["top"]
    if top:
        p = top[0]
        cases.append(EvalCase(
            f"{p['partner_name']} 미수금 얼마야?",
            [f"{p['outstanding_total']:,}"],
            "거래처 fuzzy 검색 → 미수 조회 (다단계 라우팅)",
        ))

    # 안전재고 미만 SKU 1건 상세 — 정답 = SKU + '안전재고 미만'
    if breaches:
        sku = breaches[0].sku
        cases.append(EvalCase(
            f"{sku} 재고 상태 어때?",
            [sku, "안전재고 미만"],
            "SKU 상세 라우팅+판정",
        ))

    # graceful refusal — 도구로 답할 수 없는 질문 → 환각 금지, 담당자 안내
    cases.append(EvalCase(
        "내년 2분기 매출 예측해줘",
        ["담당자"],
        "graceful refusal (환각 대신 사람에게)",
    ))
    return cases


@dataclass
class EvalOutcome:
    question: str
    expect: list[str]
    answer: str
    passed: bool
    missing: list[str]
    note: str
    input_tokens: int
    output_tokens: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question, "expect": self.expect, "answer": self.answer,
            "passed": self.passed, "missing": self.missing, "note": self.note,
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
        }


def run_eval(db, mock: bool = True) -> dict[str, Any]:
    agent = QAAgent(mock=mock)
    cases = build_eval_cases(db)
    outcomes: list[EvalOutcome] = []
    for c in cases:
        res = agent.ask(db, c.question)
        ans = res.answer or ""
        missing = [e for e in c.expect if e not in ans]
        outcomes.append(EvalOutcome(
            question=c.question, expect=c.expect, answer=ans,
            passed=not missing, missing=missing, note=c.note,
            input_tokens=res.trace.total_input_tokens,
            output_tokens=res.trace.total_output_tokens,
        ))
    passed = sum(1 for o in outcomes if o.passed)
    return {
        "mode": "MOCK" if agent.mock else f"LLM ({agent.model})",
        "total": len(outcomes), "passed": passed,
        "accuracy": round(passed / len(outcomes), 3) if outcomes else 0.0,
        "total_input_tokens": sum(o.input_tokens for o in outcomes),
        "total_output_tokens": sum(o.output_tokens for o in outcomes),
        "outcomes": [o.to_dict() for o in outcomes],
    }


def run_eval_cli(args, open_db) -> int:
    import json
    import sys
    from pathlib import Path

    db = open_db(args)
    if db is None:
        return 2

    report = run_eval(db, mock=args.mock)
    print("─" * 64, file=sys.stderr)
    print("평가 하네스 — 합성 데이터(db_seed) / 정답=DB 라이브 계산 / 포함검사", file=sys.stderr)
    print(f"모드: {report['mode']}", file=sys.stderr)
    print("─" * 64, file=sys.stderr)
    for o in report["outcomes"]:
        mark = "✅" if o["passed"] else "❌"
        print(f"{mark} {o['note']}")
        print(f"   Q: {o['question']}")
        print(f"   기대 포함: {o['expect']}  →  {'통과' if o['passed'] else '누락 ' + str(o['missing'])}")
        print(f"   A: {o['answer']}")
    print("─" * 64, file=sys.stderr)
    print(f"결과: {report['passed']}/{report['total']} 통과 (정확도 {report['accuracy']:.0%})"
          f" / 토큰 in {report['total_input_tokens']} out {report['total_output_tokens']}", file=sys.stderr)

    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[i] 리포트 저장: {args.out}", file=sys.stderr)
    return 0 if report["passed"] == report["total"] else 1
