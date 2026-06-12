"""Jarvis Core — 세 시나리오를 한 지휘자 아래 통합.

구조 = orchestrator-workers 의 "코드 지휘자" 버전:
  - 지휘 (어떤 시나리오를 언제 돌릴지) = deterministic 코드   ← LLM 아님 (예측 가능해야 하는 부분)
  - 판단 (발주량 추천 / 입금 매칭 / 견적 검증) = 각 시나리오 안의 LLM agent

설계 근거: Anthropic "Building Effective Agents" — 예측 가능한 흐름은
정해진 코드 경로 (workflow) 로, LLM 은 비정형 판단이 필요한 단계에만.

한 사이클 = cron 1회 분량:
  ① 안전재고 점검 (시나리오 1)    ─┐
  ② 미매칭 입금 처리 (시나리오 2)  ─┼─ asyncio 병렬 (서로 독립 → 동시 실행)
  ③ 수신 견적서 처리 (시나리오 3)  ─┘
  ④ Flow OPS 채널에 종합 보고
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agent import InventoryAnalystAgent
from .decision import DecisionConfig, PaymentDecisionConfig
from .flow import CHANNEL_OPS, FlowClient
from .knowledge import TacitRuleExtractor, load_all_messages
from .orchestrator import run_cycle, run_payment_cycle, run_purchase_cycle
from .payment import PaymentMatchAgent


@dataclass
class JarvisRunReport:
    inventory: list = field(default_factory=list)
    payments: list = field(default_factory=list)
    purchases: list = field(default_factory=list)
    rules: list = field(default_factory=list)   # Flow 대화에서 추출한 사내 판단 룰 (암묵지)
    duration_s: float = 0.0

    def _count(self, results) -> Counter:
        return Counter(r.decision.action for r in results)

    def summary(self) -> dict[str, Any]:
        return {
            "inventory": dict(self._count(self.inventory)),
            "payments": dict(self._count(self.payments)),
            "purchases": dict(self._count(self.purchases)),
            "duration_s": round(self.duration_s, 2),
        }


def _fmt_ops_summary(report: JarvisRunReport, db) -> str:
    inv_c = report._count(report.inventory)
    pay_c = report._count(report.payments)
    po_c = report._count(report.purchases)

    def line(c: Counter) -> str:
        return f"자동 {c.get('auto_execute', 0)} / 승인대기 {c.get('request_confirm', 0)} / 수동 {c.get('manual_review', 0)} / 보류 {c.get('skip', 0)}"

    processed = len(db.purchase_orders) + len(db.payment_matches) + len(db.purchase_entries)
    rules_block = ""
    if report.rules:
        rules_lines = "\n".join(f"  - {r.one_line()}" for r in report.rules)
        rules_block = (
            f"🧠 사내 판단 룰 — Flow 대화에서 자동 추출 {len(report.rules)}건 (적용 전 담당자 확인 필요):\n"
            f"{rules_lines}\n"
        )
    return (
        f"📊 *Jarvis 사이클 종합 보고* ({report.duration_s:.1f}s)\n"
        f"① 안전재고 ({len(report.inventory)}건 점검): {line(inv_c)}\n"
        f"   → 발주 등록 {len(db.purchase_orders)}건\n"
        f"② 입금매칭 ({len(report.payments)}건 처리): {line(pay_c)}\n"
        f"   → 매칭 저장 {len(db.payment_matches)}건 / 남은 미매칭 {len(db.list_pending_payments())}건\n"
        f"③ 구매입력 ({len(report.purchases)}건 견적): {line(po_c)}\n"
        f"   → 전표 등록 {len(db.purchase_entries)}건\n"
        f"{rules_block}"
        f"⏱️ 자동/승인 처리 합계 {processed}건 — 절감 시간은 운영 실측으로 산정 예정\n"
        f"상세는 각 채널 (재고 C-INVENTORY / 재무 C-FINANCE / 구매 C-PURCHASE) 참조."
    )


async def run_jarvis_cycle(
    db,
    flow: FlowClient,
    quotes: list[Path] | None = None,
    mock: bool = True,
    payment_limit: int | None = None,
) -> JarvisRunReport:
    """세 시나리오 병렬 실행 + 종합 보고.

    db = EcountDBClient (세 시나리오 공통 단일 진실원).
    각 시나리오는 서로 다른 테이블을 만지므로 병렬 안전:
      ① items/movements 읽기 + 발주 (in-memory)
      ② payments/sales 읽고 씀
      ③ items/partners 읽기 + 전표 (in-memory)
    """
    quotes = quotes or []
    start = time.perf_counter()

    # 암묵지 룰 추출 (사이클 시작 시 1회) — Flow 대화 → 구조화된 판단 룰
    rules = TacitRuleExtractor(mock=mock).extract(load_all_messages(flow))

    inv_agent = InventoryAnalystAgent(mock=mock)
    pay_agent = PaymentMatchAgent(mock=mock)

    def _inventory():
        return run_cycle(db, flow, inv_agent, DecisionConfig())

    def _payments():
        return run_payment_cycle(db, flow, pay_agent, PaymentDecisionConfig(), limit=payment_limit)

    def _purchases():
        out = []
        for q in quotes:
            out.append(run_purchase_cycle(db, flow, q.read_text(encoding="utf-8"), q.stem, mock=mock))
        return out

    inv, pays, pos = await asyncio.gather(
        asyncio.to_thread(_inventory),
        asyncio.to_thread(_payments),
        asyncio.to_thread(_purchases),
    )

    report = JarvisRunReport(
        inventory=inv, payments=pays, purchases=pos, rules=rules,
        duration_s=time.perf_counter() - start,
    )
    flow.send_message(CHANNEL_OPS, _fmt_ops_summary(report, db))
    return report
