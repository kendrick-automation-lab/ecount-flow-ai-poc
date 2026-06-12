"""시나리오 2 — 입금매칭 자동화.

입력:
  - 미수금 청구서 (sales 테이블, status != 'paid')
  - 입금 (payments 테이블, match_status = 'unmatched')

목적: 입금 ↔ 청구서 매칭 후보 + 신뢰도 점수 + 액션 분기

3 핵심 기법:
  1. RapidFuzz — 거래처명 표기 변형 fuzzy match
     (예: "(주)현대건자재" ↔ "현대건자재5" ↔ "현 대건자재5")
  2. Combinatorial — 분할 입금 (입금 = 청구서 2-3개 합)
  3. LLM (Claude) — 휴리스틱 후보 + 컨텍스트 → 최종 신뢰도 + case 분류

흐름:
  1. 입금 1건마다:
     a) 거래처명 정규화 + fuzzy match → top-3 partner 후보
     b) 각 후보의 outstanding invoices fetch
     c) 금액 매칭 시도: exact / split / partial
     d) LLM (또는 휴리스틱 mock) 으로 최종 ranking + 신뢰도
"""

from __future__ import annotations

import itertools
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

from .ecount_db import EcountDBClient, Invoice, Partner, Payment

# ─────────────────────────────────────────────
# Fuzzy match — 거래처명
# ─────────────────────────────────────────────

COMPANY_PREFIXES = ["(주)", "주식회사 ", "주식회사", "㈜", "(유)", "유한회사 "]


def _normalize_name(name: str) -> str:
    """거래처명 정규화 — 공백 제거 + 회사 prefix 제거 + 소문자."""
    out = name.lower()
    for prefix in COMPANY_PREFIXES:
        out = out.replace(prefix.lower(), "")
    return out.replace(" ", "").strip()


@dataclass
class PartnerCandidate:
    partner: Partner
    fuzz_score: float  # 0-100

    def to_dict(self) -> dict[str, Any]:
        return {
            "partner_id": self.partner.partner_id,
            "partner_name": self.partner.name,
            "fuzz_score": round(self.fuzz_score, 1),
        }


def fuzzy_match_partners(
    payment_name: str,
    partners: list[Partner],
    top_k: int = 3,
) -> list[PartnerCandidate]:
    """RapidFuzz 로 fuzzy match (없으면 단순 substring fallback)."""
    norm_payment = _normalize_name(payment_name)

    try:
        from rapidfuzz import fuzz

        scored: list[PartnerCandidate] = []
        for p in partners:
            norm_partner = _normalize_name(p.name)
            score = max(
                fuzz.token_sort_ratio(norm_payment, norm_partner),
                fuzz.partial_ratio(norm_payment, norm_partner),
                fuzz.ratio(norm_payment, norm_partner),
            )
            scored.append(PartnerCandidate(p, float(score)))
        return sorted(scored, key=lambda c: c.fuzz_score, reverse=True)[:top_k]
    except ImportError:
        # Fallback: substring match (rapidfuzz 미설치 시)
        out: list[PartnerCandidate] = []
        for p in partners:
            norm_partner = _normalize_name(p.name)
            if norm_payment == norm_partner:
                out.append(PartnerCandidate(p, 100.0))
            elif norm_payment in norm_partner or norm_partner in norm_payment:
                # 길이 비율 기반 점수
                ratio = min(len(norm_payment), len(norm_partner)) / max(
                    len(norm_payment), len(norm_partner), 1
                )
                out.append(PartnerCandidate(p, 60.0 + 30.0 * ratio))
        return sorted(out, key=lambda c: c.fuzz_score, reverse=True)[:top_k]


# ─────────────────────────────────────────────
# Amount match — exact / split / partial
# ─────────────────────────────────────────────


@dataclass
class AmountMatchResult:
    invoice_ids: list[int]
    total: int
    diff_ratio: float  # |payment - sum| / payment (exact/split) 또는 1 - payment/total (partial)
    case: str  # 'exact' | 'split' | 'partial'

    def to_dict(self) -> dict[str, Any]:
        return {
            "invoice_ids": self.invoice_ids,
            "total": self.total,
            "diff_ratio": round(self.diff_ratio, 4),
            "case": self.case,
        }


def find_amount_matches(
    payment_amount: int,
    invoices: list[Invoice],
    exact_tolerance: float = 0.02,
    partial_min: float = 0.30,
    partial_max: float = 0.90,
    max_split_size: int = 3,
) -> list[AmountMatchResult]:
    """금액 매칭 후보 — exact / split / partial. 정확도 순 정렬."""
    matches: list[AmountMatchResult] = []

    # (1) exact 단일
    for inv in invoices:
        diff = abs(inv.amount - payment_amount) / max(payment_amount, 1)
        if diff <= exact_tolerance:
            matches.append(AmountMatchResult([inv.invoice_id], inv.amount, diff, "exact"))

    # (2) split — 2개부터 max_split_size 개까지 조합
    for k in range(2, max_split_size + 1):
        if len(invoices) < k:
            continue
        for combo in itertools.combinations(invoices, k):
            total = sum(c.amount for c in combo)
            diff = abs(total - payment_amount) / max(payment_amount, 1)
            if diff <= exact_tolerance:
                ids = [c.invoice_id for c in combo]
                matches.append(AmountMatchResult(ids, total, diff, "split"))

    # (3) partial — 청구서 amount 의 partial_min ~ partial_max
    for inv in invoices:
        if inv.amount > 0 and partial_min * inv.amount <= payment_amount <= partial_max * inv.amount:
            ratio = 1.0 - (payment_amount / inv.amount)
            matches.append(AmountMatchResult([inv.invoice_id], inv.amount, ratio, "partial"))

    return sorted(matches, key=lambda m: m.diff_ratio)


# ─────────────────────────────────────────────
# Payment Match Agent (LLM + 휴리스틱)
# ─────────────────────────────────────────────

SYSTEM_PROMPT = """당신은 건축자재 중소기업의 입금매칭 전문가다.

입력:
- 입금: 거래처명 표기 (변형 가능) + 금액
- top-3 거래처 후보 (fuzzy match score 0-100)
- 후보별 미수금 청구서 + 금액 매칭 결과 (exact / split / partial)

규칙:
1. fuzz_score >= 85 + exact 단일 매칭 = "matched" (자동 가능 신호)
2. fuzz_score 60-85 OR split / partial = "needs_confirm"
3. fuzz_score < 60 OR 매칭 후보 0 = "manual"
4. case 가 split / partial 이면 risk_flags 에 추가
5. 출력은 JSON 한 객체. 다른 텍스트 X.

JSON 스키마:
{
  "selected_partner_id": int | null,
  "selected_invoice_ids": [int, ...],
  "case": "matched" | "needs_confirm" | "manual",
  "confidence": 0.0~1.0,
  "reason": "한 문장",
  "risk_flags": ["LOW_FUZZ_SCORE" | "SPLIT_PAYMENT" | "PARTIAL_PAYMENT" | "NO_MATCH" | "AMBIGUOUS"]
}"""


@dataclass
class PaymentRecommendation:
    payment_id: int
    selected_partner_id: int | None
    selected_invoice_ids: list[int]
    case: str  # 'matched' / 'needs_confirm' / 'manual'
    confidence: float
    reason: str
    risk_flags: list[str] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)  # debug — top-3 partner + matches
    input_tokens: int = 0
    output_tokens: int = 0
    duration_s: float = 0.0
    mock: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "payment_id": self.payment_id,
            "selected_partner_id": self.selected_partner_id,
            "selected_invoice_ids": self.selected_invoice_ids,
            "case": self.case,
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
            "risk_flags": self.risk_flags,
            "candidates": self.candidates,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "duration_s": round(self.duration_s, 3),
            "mock": self.mock,
            "error": self.error,
        }


class PaymentMatchAgent:
    def __init__(self, model: str | None = None, mock: bool = False) -> None:
        self.model = model or os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
        self.mock = mock or not os.getenv("ANTHROPIC_API_KEY")

    def recommend(
        self,
        payment: Payment,
        partners: list[Partner],
        db: EcountDBClient,
    ) -> PaymentRecommendation:
        # 1. fuzzy match
        candidates = fuzzy_match_partners(payment.partner_name_as_received, partners, top_k=3)

        # 2. 각 candidate 의 outstanding invoices + 금액 매칭
        ranked: list[dict[str, Any]] = []
        for cand in candidates:
            invoices = db.list_outstanding_invoices(partner_id=cand.partner.partner_id)
            matches = find_amount_matches(payment.amount, invoices)
            ranked.append({
                "candidate": cand.to_dict(),
                "matches": [m.to_dict() for m in matches[:3]],
                "outstanding_count": len(invoices),
            })

        # 3. LLM or mock
        if self.mock:
            return self._mock_recommend(payment, candidates, ranked)
        return self._llm_recommend(payment, candidates, ranked)

    # ─────────────────────────────────────────────
    # Mock (휴리스틱) — API 키 없을 때
    # ─────────────────────────────────────────────

    def _mock_recommend(
        self,
        payment: Payment,
        candidates: list[PartnerCandidate],
        ranked: list[dict],
    ) -> PaymentRecommendation:
        start = time.perf_counter()
        rec = PaymentRecommendation(
            payment_id=payment.payment_id,
            selected_partner_id=None,
            selected_invoice_ids=[],
            case="manual",
            confidence=0.0,
            reason="",
            candidates=ranked,
            mock=True,
        )

        if not candidates:
            rec.case = "manual"
            rec.reason = "거래처 후보 없음 (DB 에 매칭 가능 partner X)"
            rec.risk_flags.append("NO_MATCH")
            rec.duration_s = time.perf_counter() - start
            return rec

        top = candidates[0]
        top_ranked = ranked[0]
        best_match = top_ranked["matches"][0] if top_ranked["matches"] else None

        if top.fuzz_score >= 85 and best_match and best_match["case"] == "exact":
            rec.selected_partner_id = top.partner.partner_id
            rec.selected_invoice_ids = best_match["invoice_ids"]
            rec.case = "matched"
            rec.confidence = 0.95
            rec.reason = f"fuzz {top.fuzz_score:.0f} + exact 단일 매칭 ({best_match['total']:,}원)"
        elif top.fuzz_score >= 60 and best_match:
            rec.selected_partner_id = top.partner.partner_id
            rec.selected_invoice_ids = best_match["invoice_ids"]
            rec.case = "needs_confirm"
            rec.confidence = 0.70 + (top.fuzz_score - 60) * 0.01
            rec.reason = f"fuzz {top.fuzz_score:.0f} + {best_match['case']} 매칭"
            if best_match["case"] == "split":
                rec.risk_flags.append("SPLIT_PAYMENT")
            if best_match["case"] == "partial":
                rec.risk_flags.append("PARTIAL_PAYMENT")
        elif not best_match:
            rec.selected_partner_id = top.partner.partner_id if top.fuzz_score >= 70 else None
            rec.case = "manual"
            rec.confidence = 0.4
            rec.reason = f"fuzz {top.fuzz_score:.0f}, 금액 매칭 후보 없음"
            rec.risk_flags.append("NO_MATCH")
        else:
            rec.case = "manual"
            rec.confidence = 0.3
            rec.reason = f"fuzz {top.fuzz_score:.0f} 낮음"
            rec.risk_flags.append("LOW_FUZZ_SCORE")

        rec.duration_s = time.perf_counter() - start
        return rec

    # ─────────────────────────────────────────────
    # LLM (Claude) — 실제 호출
    # ─────────────────────────────────────────────

    def _llm_recommend(
        self,
        payment: Payment,
        candidates: list[PartnerCandidate],
        ranked: list[dict],
    ) -> PaymentRecommendation:
        rec = PaymentRecommendation(
            payment_id=payment.payment_id,
            selected_partner_id=None,
            selected_invoice_ids=[],
            case="manual",
            confidence=0.0,
            reason="",
            candidates=ranked,
            mock=False,
        )
        start = time.perf_counter()
        try:
            from anthropic import Anthropic

            client = Anthropic()
            user_msg = self._build_user_message(payment, ranked)
            resp = client.messages.create(
                model=self.model,
                max_tokens=500,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
            raw = "".join(b.text for b in resp.content if b.type == "text")
            rec.input_tokens = resp.usage.input_tokens
            rec.output_tokens = resp.usage.output_tokens
            data = _parse_json(raw)
            rec.selected_partner_id = data.get("selected_partner_id")
            rec.selected_invoice_ids = list(data.get("selected_invoice_ids", []))
            rec.case = data.get("case", "manual")
            rec.confidence = float(data.get("confidence", 0.0))
            rec.reason = data.get("reason", "")
            rec.risk_flags = list(data.get("risk_flags", []))
        except Exception as e:
            rec.error = f"{type(e).__name__}: {e}"
        finally:
            rec.duration_s = time.perf_counter() - start
        return rec

    @staticmethod
    def _build_user_message(payment: Payment, ranked: list[dict]) -> str:
        return (
            f"입금: '{payment.partner_name_as_received}' / {payment.amount:,}원 / 수신 {payment.received_at[:10]}\n\n"
            f"후보:\n{json.dumps(ranked, ensure_ascii=False, indent=2)}\n\n"
            "위 정보로 매칭 추천 JSON 한 객체를 출력해줘."
        )


def _parse_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return {}
    return json.loads(text[start : end + 1])
