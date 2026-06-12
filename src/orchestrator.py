"""Orchestrator — 안전재고 자동화 흐름 조율.

흐름:
  1) 이카운트에서 안전재고 미만 품목 조회
  2) 품목별로 Flow 최근 멘션 fetch (Context Builder 역할)
  3) Inventory Analyst Agent 가 추천
  4) Decision Layer 가 액션 결정
  5) 액션 실행 (이카운트 발주 / Flow confirm / Flow 사람 검토)
  6) 결과 audit log
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .agent import InventoryAnalystAgent, Recommendation
from .decision import Decision, DecisionConfig, decide
from .ecount import EcountClient, Item
from .flow import FlowClient


@dataclass
class CycleResult:
    sku: str
    item_name: str
    recommendation: Recommendation
    decision: Decision
    action_result: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sku": self.sku,
            "item_name": self.item_name,
            "recommendation": self.recommendation.to_dict(),
            "decision": {"action": self.decision.action, "reason": self.decision.reason},
            "action_result": self.action_result,
        }


CONFIRM_CHANNEL = "C-PURCHASE"
MANUAL_REVIEW_CHANNEL = "C-INVENTORY"


def _format_confirm_message(item: Item, rec: Recommendation, dec: Decision) -> str:
    amount = rec.recommended_qty * rec.est_price
    flags = " ".join(f"`{f}`" for f in rec.risk_flags) if rec.risk_flags else "(없음)"
    return (
        f"📦 *발주 추천 — 사람 confirm 요청*\n"
        f"품목: {item.sku} ({item.name})\n"
        f"현재 재고: {item.total_stock} / 안전: {item.safety_stock}\n"
        f"추천: {rec.recommended_qty}개 @ {rec.est_price:,}원 → 총 {amount:,}원\n"
        f"거래처: {rec.supplier}\n"
        f"신뢰도: {rec.confidence:.0%}\n"
        f"위험 플래그: {flags}\n"
        f"이유: {rec.reason}\n"
        f"의사결정 근거: {dec.reason}"
    )


def _format_manual_message(item: Item, rec: Recommendation, dec: Decision) -> str:
    return (
        f"⚠️ *수동 검토 필요* — {item.sku} ({item.name})\n"
        f"AI 추천: {rec.recommended_qty}개 @ {rec.est_price:,}원 / 거래처 {rec.supplier}\n"
        f"신뢰도: {rec.confidence:.0%} / 위험: {', '.join(rec.risk_flags) or '없음'}\n"
        f"의사결정: {dec.reason}\n"
        f"이유: {rec.reason}"
    )


def _format_skip_message(item: Item, rec: Recommendation) -> str:
    return (
        f"⏭️ *발주 보류* — {item.sku} ({item.name})\n"
        f"이유: {rec.reason}\n"
        f"위험 플래그: {', '.join(rec.risk_flags) or '없음'}"
    )


def _format_auto_message(item: Item, rec: Recommendation, order: dict[str, Any]) -> str:
    return (
        f"✅ *자동 발주 완료* — {item.sku} ({item.name})\n"
        f"발주번호: {order['order_id']}\n"
        f"{rec.recommended_qty}개 @ {rec.est_price:,}원 → 총 {order['total']:,}원\n"
        f"거래처: {rec.supplier}\n"
        f"AI 신뢰도: {rec.confidence:.0%}"
    )


def run_cycle(
    ecount: EcountClient,
    flow: FlowClient,
    agent: InventoryAnalystAgent,
    cfg: DecisionConfig | None = None,
) -> list[CycleResult]:
    breaches = ecount.list_safety_stock_breaches()
    results: list[CycleResult] = []

    for item in breaches:
        flow_mentions = flow.fetch_recent_mentions(item.sku)
        rec = agent.recommend(item, flow_mentions)
        dec = decide(rec, cfg)

        result = CycleResult(sku=item.sku, item_name=item.name, recommendation=rec, decision=dec)

        if dec.action == "auto_execute":
            order = ecount.save_purchase_order(
                sku=item.sku,
                qty=rec.recommended_qty,
                supplier=rec.supplier or "(미정)",
                est_price=rec.est_price,
                note=f"AI 자동 발주 — 신뢰도 {rec.confidence:.0%}",
            )
            flow.send_message(CONFIRM_CHANNEL, _format_auto_message(item, rec, order))
            result.action_result = {"order": order}
        elif dec.action == "request_confirm":
            msg = flow.send_message(
                CONFIRM_CHANNEL,
                _format_confirm_message(item, rec, dec),
                actions=["/approve", "/reject", "/modify"],
            )
            response = flow.wait_for_confirm(item.sku)
            result.action_result = {"flow_message": msg, "confirm_response": response}
            if response == "approve":
                order = ecount.save_purchase_order(
                    sku=item.sku,
                    qty=rec.recommended_qty,
                    supplier=rec.supplier or "(미정)",
                    est_price=rec.est_price,
                    note=f"Confirm 후 발주 — 신뢰도 {rec.confidence:.0%}",
                )
                flow.send_message(CONFIRM_CHANNEL, _format_auto_message(item, rec, order))
                result.action_result["order"] = order
        elif dec.action == "manual_review":
            msg = flow.send_message(MANUAL_REVIEW_CHANNEL, _format_manual_message(item, rec, dec))
            result.action_result = {"flow_message": msg}
        else:  # skip
            msg = flow.send_message(MANUAL_REVIEW_CHANNEL, _format_skip_message(item, rec))
            result.action_result = {"flow_message": msg}

        results.append(result)

    return results


# ─────────────────────────────────────────────
# 시나리오 2 — 입금매칭 cycle
# ─────────────────────────────────────────────


@dataclass
class PaymentCycleResult:
    payment_id: int
    partner_name_as_received: str
    amount: int
    recommendation: Any
    decision: Decision
    action_result: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "payment_id": self.payment_id,
            "partner_name_as_received": self.partner_name_as_received,
            "amount": self.amount,
            "recommendation": self.recommendation.to_dict(),
            "decision": {"action": self.decision.action, "reason": self.decision.reason},
            "action_result": {k: v for k, v in self.action_result.items() if k != "flow_message"},
        }


def _fmt_payment_confirm(payment, rec, dec) -> str:
    total = sum(1 for _ in rec.selected_invoice_ids)
    flags = " ".join(f"`{f}`" for f in rec.risk_flags) if rec.risk_flags else "(없음)"
    return (
        f"입금: '{payment.partner_name_as_received}' / {payment.amount:,}원 / {payment.received_at[:10]}\n"
        f"추천 매칭: 청구서 {rec.selected_invoice_ids} ({total}건)\n"
        f"신뢰도: {rec.confidence:.0%} / 위험 플래그: {flags}\n"
        f"이유: {rec.reason}\n"
        f"의사결정 근거: {dec.reason}"
    )


def _fmt_payment_auto(payment, rec) -> str:
    return (
        f"✅ *자동 입금매칭 완료* — '{payment.partner_name_as_received}' {payment.amount:,}원\n"
        f"청구서 {rec.selected_invoice_ids} 매칭 (신뢰도 {rec.confidence:.0%})"
    )


def _fmt_payment_manual(payment, rec, dec) -> str:
    return (
        f"⚠️ *수동 검토 필요* — '{payment.partner_name_as_received}' {payment.amount:,}원 ({payment.received_at[:10]})\n"
        f"사유: {dec.reason}\n"
        f"AI 메모: {rec.reason or '(없음)'}"
    )


def run_payment_cycle(
    db,
    flow: FlowClient,
    agent,
    cfg=None,
    limit: int | None = None,
) -> list[PaymentCycleResult]:
    """시나리오 2 — 미매칭 입금 N건 처리.

    흐름: 미매칭 입금 조회 → PaymentMatchAgent 추천 → decide_payment 분기
          → 자동 등록 / Flow confirm / 수동 검토 → DB 업데이트 + audit.
    """
    from .decision import decide_payment
    from .flow import CHANNEL_FINANCE

    partners = db.list_partners()
    pending = db.list_pending_payments(limit=limit)
    results: list[PaymentCycleResult] = []

    for payment in pending:
        rec = agent.recommend(payment, partners, db)
        dec = decide_payment(rec, cfg)
        result = PaymentCycleResult(
            payment_id=payment.payment_id,
            partner_name_as_received=payment.partner_name_as_received,
            amount=payment.amount,
            recommendation=rec,
            decision=dec,
        )

        if dec.action == "auto_execute":
            match = db.save_payment_match(payment.payment_id, rec.selected_invoice_ids, "matched")
            flow.send_message(CHANNEL_FINANCE, _fmt_payment_auto(payment, rec))
            result.action_result = {"match": match}

        elif dec.action == "request_confirm":
            key = f"pay-{payment.payment_id}"
            msg = flow.request_decision(
                CHANNEL_FINANCE,
                decision_key=key,
                title="입금매칭 confirm 요청",
                body=_fmt_payment_confirm(payment, rec, dec),
                options=[
                    {"key": "approve", "label": "추천대로 매칭"},
                    {"key": "reject", "label": "매칭 거절 (미매칭 유지)"},
                    {"key": "manual", "label": "수동 처리로 전환"},
                ],
                mentions=["재무팀"],
            )
            response = flow.wait_for_user_response(key)
            result.action_result = {"flow_message": msg, "confirm_response": response}
            if response == "approve":
                status = "partial" if "PARTIAL_PAYMENT" in rec.risk_flags else "matched"
                match = db.save_payment_match(payment.payment_id, rec.selected_invoice_ids, status)
                flow.send_message(CHANNEL_FINANCE, _fmt_payment_auto(payment, rec))
                result.action_result["match"] = match
            elif response == "timeout":
                flow.send_message(CHANNEL_FINANCE, f"⏰ confirm timeout — '{payment.partner_name_as_received}' {payment.amount:,}원 미매칭 유지, 수동 검토로 이관")

        else:  # manual_review
            msg = flow.send_message(CHANNEL_FINANCE, _fmt_payment_manual(payment, rec, dec))
            result.action_result = {"flow_message": msg}

        results.append(result)

    return results


# ─────────────────────────────────────────────
# 시나리오 3 — 구매입력 cycle (multi-agent pipeline)
# ─────────────────────────────────────────────


@dataclass
class PurchaseCycleResult:
    quote_ref: str
    recommendation: Any
    decision: Decision
    action_result: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "quote_ref": self.quote_ref,
            "recommendation": self.recommendation.to_dict(),
            "decision": {"action": self.decision.action, "reason": self.decision.reason},
            "action_result": {k: v for k, v in self.action_result.items() if k != "flow_message"},
        }


def _fmt_purchase_lines(rec) -> str:
    rows = []
    for ln in rec.lines:
        sku = ln.matched_sku or "❓미매칭"
        dev = ""
        if ln.db_price:
            pct = (ln.unit_price - ln.db_price) / ln.db_price * 100
            dev = f" (DB 대비 {pct:+.0f}%)"
        rows.append(f"  - {ln.raw_name} → {sku} / {ln.qty}개 @ {ln.unit_price:,}원{dev}")
    return "\n".join(rows)


def _fmt_purchase_confirm(rec, dec) -> str:
    flags = " ".join(f"`{f}`" for f in rec.risk_flags) if rec.risk_flags else "(없음)"
    return (
        f"견적: {rec.quote_ref} / 거래처: {rec.partner_name} (일치율 {rec.partner_score:.0f})\n"
        f"{_fmt_purchase_lines(rec)}\n"
        f"총액: {rec.total:,}원 / 신뢰도: {rec.confidence:.0%} / 위험: {flags}\n"
        f"의사결정 근거: {dec.reason}"
    )


def _fmt_purchase_auto(rec, entry) -> str:
    return (
        f"✅ *구매전표 자동 등록* — {entry['entry_id']} ({rec.quote_ref})\n"
        f"거래처: {rec.partner_name} / 총액 {rec.total:,}원\n"
        f"{_fmt_purchase_lines(rec)}"
    )


def _fmt_purchase_manual(rec, dec) -> str:
    return (
        f"⚠️ *수동 검토 필요* — 견적 {rec.quote_ref}\n"
        f"사유: {dec.reason}\n"
        f"{_fmt_purchase_lines(rec)}\n"
        f"AI 메모: {rec.reason or '(없음)'}"
    )


def run_purchase_cycle(
    db,
    flow: FlowClient,
    quote_text: str,
    quote_ref: str,
    mock: bool = False,
    cfg=None,
) -> PurchaseCycleResult:
    """시나리오 3 — 견적서 1건 처리.

    흐름: Extractor (LLM) → SkuMatcher (알고리즘) → Validator (LLM Critic)
          → decide_purchase → 자동 등록 / Flow confirm / 수동 검토.
    """
    from .decision import decide_purchase
    from .flow import CHANNEL_PURCHASE
    from .purchase import run_purchase_pipeline

    rec = run_purchase_pipeline(quote_text, quote_ref, db, mock=mock)
    dec = decide_purchase(rec, cfg)
    result = PurchaseCycleResult(quote_ref=quote_ref, recommendation=rec, decision=dec)

    def _save_entry():
        lines = [{"sku": ln.matched_sku, "qty": ln.qty, "unit_price": ln.unit_price}
                 for ln in rec.lines if ln.matched_sku]
        return db.save_purchase_entry(rec.partner_id, lines, quote_ref=quote_ref,
                                      note=f"AI 추출 — 신뢰도 {rec.confidence:.0%}")

    if dec.action == "auto_execute":
        entry = _save_entry()
        flow.send_message(CHANNEL_PURCHASE, _fmt_purchase_auto(rec, entry))
        result.action_result = {"entry": entry}

    elif dec.action == "request_confirm":
        key = f"po-{quote_ref}"
        msg = flow.request_decision(
            CHANNEL_PURCHASE,
            decision_key=key,
            title="구매전표 confirm 요청",
            body=_fmt_purchase_confirm(rec, dec),
            options=[
                {"key": "approve", "label": "초안대로 등록"},
                {"key": "reject", "label": "등록 거절"},
                {"key": "manual", "label": "수동 처리로 전환"},
            ],
            mentions=["구매팀"],
        )
        response = flow.wait_for_user_response(key)
        result.action_result = {"flow_message": msg, "confirm_response": response}
        if response == "approve":
            entry = _save_entry()
            flow.send_message(CHANNEL_PURCHASE, _fmt_purchase_auto(rec, entry))
            result.action_result["entry"] = entry
        elif response == "timeout":
            flow.send_message(CHANNEL_PURCHASE, f"⏰ confirm timeout — 견적 {quote_ref} 미등록, 수동 검토로 이관")

    else:  # manual_review
        msg = flow.send_message(CHANNEL_PURCHASE, _fmt_purchase_manual(rec, dec))
        result.action_result = {"flow_message": msg}

    return result
