"""시나리오 3 — 구매입력 자동화 (견적서 → 이카운트 구매전표 초안).

특화 에이전트 체인 (multi-agent pipeline):

  1. ExtractorAgent  — 비정형 견적서 → 정형 JSON          ← LLM 이 필요한 단계 (멀티모달)
  2. SkuMatcher      — 품명 ↔ 품목 DB fuzzy 매칭          ← LLM 불필요 — 일부러 안 씀 (결정적 알고리즘이 더 정확/저렴)
  3. ValidatorAgent  — 단가/수량/거래처 이상 검증 (Critic)  ← LLM 이 필요한 단계 (맥락 판단)
  4. 통합 + 분기      — deterministic 코드 (decision layer)

설계 근거 (Anthropic "Building Effective Agents" 권고와 일치):
- 가장 단순한 구조 우선. LLM 은 비정형 이해가 필요한 단계에만.
- 패턴 = prompt chaining (체인) + evaluator (검증자).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

from .payment import PartnerCandidate, fuzzy_match_partners

# ─────────────────────────────────────────────
# 데이터 구조
# ─────────────────────────────────────────────


@dataclass
class QuoteLine:
    raw_name: str          # 견적서에 적힌 품명 (그대로)
    qty: int
    unit_price: int        # 견적 단가
    matched_sku: str | None = None
    matched_name: str | None = None
    match_score: float = 0.0
    db_price: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_name": self.raw_name, "qty": self.qty, "unit_price": self.unit_price,
            "matched_sku": self.matched_sku, "matched_name": self.matched_name,
            "match_score": round(self.match_score, 1), "db_price": self.db_price,
        }


@dataclass
class ExtractedQuote:
    partner_name_raw: str
    quote_date: str
    lines: list[QuoteLine]
    quote_ref: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None


@dataclass
class PurchaseRecommendation:
    quote_ref: str
    partner_id: int | None
    partner_name: str | None
    partner_score: float
    lines: list[QuoteLine]
    total: int
    confidence: float
    reason: str
    risk_flags: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    duration_s: float = 0.0
    mock: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "quote_ref": self.quote_ref,
            "partner_id": self.partner_id, "partner_name": self.partner_name,
            "partner_score": round(self.partner_score, 1),
            "lines": [ln.to_dict() for ln in self.lines],
            "total": self.total, "confidence": round(self.confidence, 3),
            "reason": self.reason, "risk_flags": self.risk_flags,
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "duration_s": round(self.duration_s, 3), "mock": self.mock, "error": self.error,
        }


# ─────────────────────────────────────────────
# Agent 1 — Extractor (비정형 → 정형)
# ─────────────────────────────────────────────

EXTRACTOR_SYSTEM = """당신은 건축자재 회사의 견적서 추출 전문가다.
입력: 견적서 텍스트 (또는 이미지).
출력: JSON 한 객체. 다른 텍스트 X.

{
  "partner_name_raw": "거래처명 그대로",
  "quote_date": "YYYY-MM-DD",
  "lines": [{"raw_name": "품명 그대로", "qty": int, "unit_price": int}]
}

규칙: 없는 항목을 지어내지 마라. 못 읽으면 lines 를 비워라."""


class ExtractorAgent:
    """견적서 → 정형 JSON. mock = 규칙 파서 / LLM = Claude (텍스트, 이미지는 base64 확장 가능)."""

    def __init__(self, model: str | None = None, mock: bool = False) -> None:
        self.model = model or os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
        self.mock = mock or not os.getenv("ANTHROPIC_API_KEY")

    def extract(self, quote_text: str, quote_ref: str = "") -> ExtractedQuote:
        if self.mock:
            return self._mock_extract(quote_text, quote_ref)
        return self._llm_extract(quote_text, quote_ref)

    @staticmethod
    def _mock_extract(quote_text: str, quote_ref: str) -> ExtractedQuote:
        """규칙 파서 — '거래처:' / '날짜:' 헤더 + '품명 | 수량 | 단가' 라인."""
        partner, date = "", ""
        lines: list[QuoteLine] = []
        in_table = False
        for raw in quote_text.splitlines():
            s = raw.strip()
            if s.startswith("거래처:"):
                partner = s.split(":", 1)[1].strip()
            elif s.startswith("날짜:"):
                date = s.split(":", 1)[1].strip()
            elif s.startswith("----"):
                in_table = True
            elif in_table and "|" in s:
                parts = [p.strip() for p in s.split("|")]
                if len(parts) >= 3:
                    try:
                        lines.append(QuoteLine(raw_name=parts[0], qty=int(parts[1]), unit_price=int(parts[2])))
                    except ValueError:
                        continue
        return ExtractedQuote(partner_name_raw=partner, quote_date=date, lines=lines, quote_ref=quote_ref)

    def _llm_extract(self, quote_text: str, quote_ref: str) -> ExtractedQuote:
        out = ExtractedQuote(partner_name_raw="", quote_date="", lines=[], quote_ref=quote_ref)
        try:
            from anthropic import Anthropic

            client = Anthropic()
            resp = client.messages.create(
                model=self.model, max_tokens=800, system=EXTRACTOR_SYSTEM,
                messages=[{"role": "user", "content": f"[견적서]\n{quote_text}"}],
            )
            raw = "".join(b.text for b in resp.content if b.type == "text")
            out.input_tokens = resp.usage.input_tokens
            out.output_tokens = resp.usage.output_tokens
            data = _parse_json(raw)
            out.partner_name_raw = data.get("partner_name_raw", "")
            out.quote_date = data.get("quote_date", "")
            out.lines = [QuoteLine(raw_name=l["raw_name"], qty=int(l["qty"]), unit_price=int(l["unit_price"]))
                         for l in data.get("lines", [])]
        except Exception as e:
            out.error = f"{type(e).__name__}: {e}"
        return out


# ─────────────────────────────────────────────
# Step 2 — SkuMatcher (결정적 알고리즘 — LLM 일부러 안 씀)
# ─────────────────────────────────────────────


class SkuMatcher:
    """견적 품명 ↔ 품목 카탈로그 fuzzy 매칭. 임계: ≥75 매칭 / <75 미매칭."""

    MATCH_THRESHOLD = 75.0

    @staticmethod
    def _score(a: str, b: str) -> float:
        try:
            from rapidfuzz import fuzz
            return float(max(fuzz.token_sort_ratio(a, b), fuzz.partial_ratio(a, b)))
        except ImportError:
            na, nb = a.replace(" ", ""), b.replace(" ", "")
            if na == nb:
                return 100.0
            if na in nb or nb in na:
                return 80.0
            return 0.0

    def match_lines(self, lines: list[QuoteLine], catalog: list[dict]) -> list[QuoteLine]:
        for ln in lines:
            best_score, best = 0.0, None
            for item in catalog:
                score = self._score(ln.raw_name, item["name"])
                if score > best_score:
                    best_score, best = score, item
            if best and best_score >= self.MATCH_THRESHOLD:
                ln.matched_sku = best["sku"]
                ln.matched_name = best["name"]
                ln.match_score = best_score
                ln.db_price = best["unit_price"]
            else:
                ln.match_score = best_score
        return lines


# ─────────────────────────────────────────────
# Agent 3 — Validator (Critic / Evaluator)
# ─────────────────────────────────────────────

VALIDATOR_SYSTEM = """당신은 건축자재 회사의 구매전표 검증 전문가다.
입력: 추출된 견적 + SKU 매칭 결과 + 거래처 후보.
출력: JSON 한 객체. 다른 텍스트 X.

{
  "confidence": 0.0~1.0,
  "risk_flags": ["PRICE_DEVIATION" | "QTY_OUTLIER" | "UNMATCHED_LINE" | "PARTNER_UNKNOWN" | "TOTAL_MISMATCH"],
  "reason": "한 문장"
}

규칙:
1. 견적 단가가 DB 단가 대비 ±30% 벗어나면 PRICE_DEVIATION
2. 수량 > 500 이면 QTY_OUTLIER
3. SKU 미매칭 라인 있으면 UNMATCHED_LINE
4. 거래처 fuzzy < 60 이면 PARTNER_UNKNOWN"""

PRICE_DEVIATION_RATIO = 0.30
QTY_OUTLIER = 500


class ValidatorAgent:
    """단가/수량/거래처 이상 검증. mock = 룰 / LLM = Claude (맥락 판단 추가)."""

    def __init__(self, model: str | None = None, mock: bool = False) -> None:
        self.model = model or os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
        self.mock = mock or not os.getenv("ANTHROPIC_API_KEY")

    def validate(
        self,
        extracted: ExtractedQuote,
        partner_cands: list[PartnerCandidate],
    ) -> PurchaseRecommendation:
        start = time.perf_counter()
        top = partner_cands[0] if partner_cands else None
        rec = PurchaseRecommendation(
            quote_ref=extracted.quote_ref,
            partner_id=top.partner.partner_id if top and top.fuzz_score >= 60 else None,
            partner_name=top.partner.name if top else None,
            partner_score=top.fuzz_score if top else 0.0,
            lines=extracted.lines,
            total=sum(ln.qty * ln.unit_price for ln in extracted.lines),
            confidence=0.0, reason="", mock=self.mock,
            input_tokens=extracted.input_tokens, output_tokens=extracted.output_tokens,
        )

        # 룰 기반 플래그 (mock/LLM 공통 — LLM 모드여도 룰 안전망은 유지)
        flags: list[str] = []
        for ln in extracted.lines:
            if ln.matched_sku is None:
                if "UNMATCHED_LINE" not in flags:
                    flags.append("UNMATCHED_LINE")
            elif ln.db_price and abs(ln.unit_price - ln.db_price) / max(ln.db_price, 1) > PRICE_DEVIATION_RATIO:
                if "PRICE_DEVIATION" not in flags:
                    flags.append("PRICE_DEVIATION")
            if ln.qty > QTY_OUTLIER and "QTY_OUTLIER" not in flags:
                flags.append("QTY_OUTLIER")
        if rec.partner_id is None:
            flags.append("PARTNER_UNKNOWN")
        rec.risk_flags = flags

        if self.mock:
            rec.confidence = max(0.2, 0.95 - 0.15 * len(flags))
            rec.reason = "룰 기반 검증 — " + (", ".join(flags) if flags else "이상 없음")
            rec.duration_s = time.perf_counter() - start
            return rec

        # LLM 모드 — 룰 플래그 위에 맥락 판단 추가
        try:
            from anthropic import Anthropic

            client = Anthropic()
            payload = {
                "extracted": {"partner": extracted.partner_name_raw, "date": extracted.quote_date,
                              "lines": [ln.to_dict() for ln in extracted.lines]},
                "partner_candidates": [c.to_dict() for c in partner_cands],
                "rule_flags": flags,
            }
            resp = client.messages.create(
                model=self.model, max_tokens=400, system=VALIDATOR_SYSTEM,
                messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            )
            raw = "".join(b.text for b in resp.content if b.type == "text")
            rec.input_tokens += resp.usage.input_tokens
            rec.output_tokens += resp.usage.output_tokens
            data = _parse_json(raw)
            rec.confidence = float(data.get("confidence", 0.5))
            llm_flags = list(data.get("risk_flags", []))
            rec.risk_flags = sorted(set(flags) | set(llm_flags))  # 룰 ∪ LLM (안전망 우선)
            rec.reason = data.get("reason", "")
        except Exception as e:
            rec.error = f"{type(e).__name__}: {e}"
            rec.confidence = max(0.2, 0.95 - 0.15 * len(flags))
            rec.reason = "LLM 검증 실패 — 룰 결과만 사용"
        finally:
            rec.duration_s = time.perf_counter() - start
        return rec


# ─────────────────────────────────────────────
# 파이프라인 (체인 실행)
# ─────────────────────────────────────────────


def run_purchase_pipeline(
    quote_text: str,
    quote_ref: str,
    db,
    extractor: ExtractorAgent | None = None,
    validator: ValidatorAgent | None = None,
    mock: bool = False,
) -> PurchaseRecommendation:
    """Extractor → SkuMatcher → Validator 체인 한 바퀴."""
    extractor = extractor or ExtractorAgent(mock=mock)
    validator = validator or ValidatorAgent(mock=mock)

    extracted = extractor.extract(quote_text, quote_ref)
    if extracted.error or not extracted.lines:
        return PurchaseRecommendation(
            quote_ref=quote_ref, partner_id=None, partner_name=None, partner_score=0.0,
            lines=[], total=0, confidence=0.0,
            reason=f"추출 실패: {extracted.error or '라인 없음'}",
            risk_flags=["EXTRACT_FAILED"], mock=extractor.mock,
        )

    SkuMatcher().match_lines(extracted.lines, db.list_item_catalog())
    partner_cands = fuzzy_match_partners(extracted.partner_name_raw, db.list_partners(), top_k=3)
    return validator.validate(extracted, partner_cands)


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
