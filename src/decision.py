"""Decision Layer — 룰 기반 임계값 분기.

LLM 추천 + 회사 룰 → 어떤 액션 갈지 결정.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .agent import Recommendation

Action = Literal["auto_execute", "request_confirm", "manual_review", "skip"]


@dataclass
class DecisionConfig:
    auto_confidence_min: float = 0.95
    auto_amount_max: int = 100_000
    confirm_confidence_min: float = 0.80
    confirm_amount_max: int = 1_000_000


@dataclass
class Decision:
    action: Action
    reason: str


def decide(rec: Recommendation, cfg: DecisionConfig | None = None) -> Decision:
    cfg = cfg or DecisionConfig()
    if rec.error:
        return Decision("manual_review", f"agent error: {rec.error}")
    if rec.recommended_qty <= 0 or "NO_DEMAND" in rec.risk_flags:
        return Decision("skip", f"발주 보류 ({rec.reason})")

    amount = rec.recommended_qty * rec.est_price

    if rec.confidence >= cfg.auto_confidence_min and amount <= cfg.auto_amount_max and not rec.risk_flags:
        return Decision("auto_execute", f"신뢰도 {rec.confidence:.0%} + 금액 {amount:,} ≤ {cfg.auto_amount_max:,} + 위험 플래그 없음")

    if rec.confidence >= cfg.confirm_confidence_min and amount <= cfg.confirm_amount_max:
        flags = ", ".join(rec.risk_flags) if rec.risk_flags else "없음"
        return Decision("request_confirm", f"신뢰도 {rec.confidence:.0%} + 금액 {amount:,} + 위험: {flags}")

    return Decision("manual_review", f"신뢰도 {rec.confidence:.0%} 또는 금액 {amount:,} 가 임계값 초과 — 사람 검토 필요")


# ─────────────────────────────────────────────
# 시나리오 2 — 입금매칭 decision
# ─────────────────────────────────────────────


@dataclass
class PaymentDecisionConfig:
    auto_confidence_min: float = 0.95   # 이상 + 위험 플래그 없음 = 자동 매칭 등록
    confirm_confidence_min: float = 0.60  # 이상 = Flow confirm / 미만 = 사람 수동


def decide_payment(rec, cfg: PaymentDecisionConfig | None = None) -> Decision:
    """PaymentRecommendation → 액션 분기 (룰 안전망).

    auto_execute    : matched + 신뢰도 ≥ 95% + 위험 플래그 없음
    request_confirm : 신뢰도 ≥ 60% (분할/부분 입금 포함 — 플래그와 함께 사람에게)
    manual_review   : 그 외 (후보 없음 / 신뢰도 낮음 / agent 오류)
    """
    cfg = cfg or PaymentDecisionConfig()
    if rec.error:
        return Decision("manual_review", f"agent error: {rec.error}")
    if rec.case == "manual" or not rec.selected_invoice_ids:
        return Decision("manual_review", rec.reason or "매칭 후보 부족")
    if rec.case == "matched" and rec.confidence >= cfg.auto_confidence_min and not rec.risk_flags:
        return Decision("auto_execute", f"신뢰도 {rec.confidence:.0%} + 단일 정확 매칭 + 위험 플래그 없음")
    if rec.confidence >= cfg.confirm_confidence_min:
        flags = ", ".join(rec.risk_flags) if rec.risk_flags else "없음"
        return Decision("request_confirm", f"신뢰도 {rec.confidence:.0%} / 위험: {flags}")
    return Decision("manual_review", f"신뢰도 {rec.confidence:.0%} 낮음 — 사람 검토 필요")


# ─────────────────────────────────────────────
# 시나리오 3 — 구매입력 decision
# ─────────────────────────────────────────────


@dataclass
class PurchaseDecisionConfig:
    auto_total_max: int = 500_000        # 이하 + 무플래그 + 거래처 확실 = 자동 등록
    confirm_total_max: int = 5_000_000   # 이하 = Flow confirm / 초과 = 사람 검토
    partner_score_min: float = 85.0      # 자동 등록에 필요한 거래처 일치율


def decide_purchase(rec, cfg: PurchaseDecisionConfig | None = None) -> Decision:
    """PurchaseRecommendation → 액션 분기.

    구매전표는 돈이 나가는 입력이라 기본 보수적:
    manual  : 추출 실패 / SKU 미매칭 / 거래처 불명
    auto    : 플래그 0 + 총액 ≤ 50만 + 거래처 일치 ≥ 85
    confirm : 그 외 총액 ≤ 500만
    """
    cfg = cfg or PurchaseDecisionConfig()
    if rec.error and not rec.lines:
        return Decision("manual_review", f"pipeline error: {rec.error}")
    hard_flags = {"EXTRACT_FAILED", "UNMATCHED_LINE", "PARTNER_UNKNOWN"}
    if hard_flags & set(rec.risk_flags):
        bad = ", ".join(sorted(hard_flags & set(rec.risk_flags)))
        return Decision("manual_review", f"수동 필수 플래그: {bad}")
    if not rec.risk_flags and rec.total <= cfg.auto_total_max and rec.partner_score >= cfg.partner_score_min:
        return Decision("auto_execute", f"무플래그 + 총액 {rec.total:,} ≤ {cfg.auto_total_max:,} + 거래처 일치 {rec.partner_score:.0f}")
    if rec.total <= cfg.confirm_total_max:
        flags = ", ".join(rec.risk_flags) if rec.risk_flags else "없음"
        return Decision("request_confirm", f"총액 {rec.total:,} / 위험: {flags}")
    return Decision("manual_review", f"총액 {rec.total:,} > {cfg.confirm_total_max:,} — 고액 수동 검토")
