"""Inventory Analyst Agent — 안전재고 → 발주 추천 LLM agent.

실제 LLM 호출 (Anthropic Claude) + mock 모드 (API 키 없을 때 휴리스틱 사용).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

from .ecount import Item
from .flow import FlowMessage

SYSTEM_PROMPT = """당신은 건축자재 중소기업의 발주 추천 전문가다.

입력: 품목 정보 (SKU / 현재재고 / 안전재고 / 최근 4주 출고 / 거래처별 단가 / 리드타임), Flow 최근 대화 요약.

규칙:
1. 재고 부족 = 즉시 발주 라는 단순 결론 X. Flow 대화에 '이미 발주 계획' / '단종 검토' / '행사 종료' 단서 있으면 recommended_qty 를 줄이거나 0 으로.
2. 거래처 선택은 단가 + 리드타임 균형. 단가 변동성 있으면 표시.
3. 출력은 반드시 JSON 한 객체. 다른 텍스트 X.
4. confidence: 데이터 근거 강하면 0.85+, 컨텍스트 단서 모호하면 0.6~0.8, 충돌 신호 있으면 0.5 이하.
5. risk_flags: LOW_CONFIDENCE / PRICE_VOLATILE / LEAD_TIME_LONG / ALREADY_PLANNED / NO_DEMAND / STALE_DATA 중 해당 항목 배열.
6. 최근 4주 출고 데이터가 비어 있으면 = ERP 입력 누락 가능성. STALE_DATA 플래그 + confidence 0.6 이하 (데이터 신선도 의심 시 자동 발주 금지).

JSON 스키마:
{
  "recommended_qty": int,
  "supplier": "거래처명 또는 null",
  "est_price": int,
  "confidence": 0.0~1.0,
  "reason": "한 문장",
  "risk_flags": ["..."]
}"""


@dataclass
class Recommendation:
    sku: str
    recommended_qty: int
    supplier: str | None
    est_price: int
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
            "sku": self.sku,
            "recommended_qty": self.recommended_qty,
            "supplier": self.supplier,
            "est_price": self.est_price,
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
            "risk_flags": self.risk_flags,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "duration_s": round(self.duration_s, 3),
            "mock": self.mock,
            "error": self.error,
        }


class InventoryAnalystAgent:
    def __init__(self, model: str | None = None, mock: bool = False) -> None:
        self.model = model or os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
        self.mock = mock or not os.getenv("ANTHROPIC_API_KEY")

    def recommend(self, item: Item, flow_mentions: list[FlowMessage]) -> Recommendation:
        if self.mock:
            return self._mock_recommend(item, flow_mentions)
        return self._llm_recommend(item, flow_mentions)

    def _build_user_message(self, item: Item, flow_mentions: list[FlowMessage]) -> str:
        flow_summary = (
            "\n".join(f"- [{m.ts[:10]} {m.author}] {m.text}" for m in flow_mentions)
            if flow_mentions
            else "(관련 Flow 대화 없음)"
        )
        return (
            f"품목: {item.sku} ({item.name}, {item.category})\n"
            f"현재 총 재고: {item.total_stock} / 안전재고: {item.safety_stock}\n"
            f"창고별: {item.stocks}\n"
            f"최근 4주 출고: {item.past_4w_outflow}\n"
            f"거래처: {item.suppliers}\n"
            f"최근 30일 발주 횟수: {item.recent_orders_30d}\n\n"
            f"Flow 최근 멘션:\n{flow_summary}\n\n"
            "위 정보로 발주 추천 JSON 한 객체를 출력해줘."
        )

    def _llm_recommend(self, item: Item, flow_mentions: list[FlowMessage]) -> Recommendation:
        rec = Recommendation(
            sku=item.sku,
            recommended_qty=0,
            supplier=None,
            est_price=0,
            confidence=0.0,
            reason="",
            mock=False,
        )
        start = time.perf_counter()
        try:
            from anthropic import Anthropic

            client = Anthropic()
            resp = client.messages.create(
                model=self.model,
                max_tokens=600,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": self._build_user_message(item, flow_mentions)}],
            )
            raw = "".join(b.text for b in resp.content if b.type == "text")
            rec.input_tokens = resp.usage.input_tokens
            rec.output_tokens = resp.usage.output_tokens
            data = _parse_json(raw)
            rec.recommended_qty = int(data.get("recommended_qty", 0))
            rec.supplier = data.get("supplier")
            rec.est_price = int(data.get("est_price", 0))
            rec.confidence = float(data.get("confidence", 0.0))
            rec.reason = data.get("reason", "")
            rec.risk_flags = list(data.get("risk_flags", []))
        except Exception as e:
            rec.error = f"{type(e).__name__}: {e}"
        finally:
            rec.duration_s = time.perf_counter() - start
        return rec

    def _mock_recommend(self, item: Item, flow_mentions: list[FlowMessage]) -> Recommendation:
        """API 키 없을 때 휴리스틱 추천 (데모용).

        규칙:
        - "이미 발주 계획" / "행사 종료" / "단종 검토" 멘션 있으면 qty=0 + flag
        - 그 외엔 안전재고 - 현재재고 + 1주 출고 평균 만큼 추천
        - 단가 변동 ±5% 초과면 PRICE_VOLATILE
        - 리드타임 7일 초과면 LEAD_TIME_LONG
        """
        start = time.perf_counter()
        flow_text = " ".join(m.text for m in flow_mentions)
        flags: list[str] = []
        qty = 0
        supplier_pick = None
        price = 0
        confidence = 0.85
        reason = ""

        if "행사 종료" in flow_text or "출고 거의 멈춤" in flow_text or "당분간 발주 X" in flow_text:
            flags.append("NO_DEMAND")
            qty = 0
            confidence = 0.92
            reason = "Flow 멘션에 행사 종료/출고 정체 단서. 발주 보류 추천."
        elif "단종 검토" in flow_text:
            flags.append("ALREADY_PLANNED")
            qty = 0
            confidence = 0.88
            reason = "Flow 멘션에 단종 검토 단서. 발주 보류."
        elif "발주 계획" in flow_text or "발주 검토" in flow_text:
            flags.append("ALREADY_PLANNED")
            qty = 0
            confidence = 0.7
            reason = "Flow 멘션에 사전 발주 계획 단서. 중복 발주 방지."
        elif not item.past_4w_outflow:
            # 데이터 신선도 가드 — 출고 이력이 비어 있으면 ERP 입력 누락 가능성
            # (업계 도입 실패 요인 1위 = "ERP 입력 누락 → AI 분석이 현실과 괴리")
            flags.append("STALE_DATA")
            qty = max(item.safety_stock - item.total_stock, 0)
            confidence = 0.55
            reason = "최근 4주 출고 이력 없음 — ERP 입력 누락 의심. 수요 판단 불가, 담당자 확인 필요."
        else:
            avg_weekly = sum(item.past_4w_outflow) / 4
            qty = max(int(item.safety_stock - item.total_stock + avg_weekly), int(avg_weekly))
            best = min(item.suppliers, key=lambda s: (s["last_price"], s["avg_lead_days"])) if item.suppliers else None
            supplier_pick = best["name"] if best else None
            price = best["last_price"] if best else 0
            reason = f"안전재고 - 현재재고 + 주평균출고({avg_weekly:.1f}) 기준. 최저단가 거래처 선택."
            if best:
                hist = best.get("price_history_30d", [])
                if hist and (max(hist) - min(hist)) / max(min(hist), 1) > 0.05:
                    flags.append("PRICE_VOLATILE")
                if best["avg_lead_days"] > 7:
                    flags.append("LEAD_TIME_LONG")

        rec = Recommendation(
            sku=item.sku,
            recommended_qty=qty,
            supplier=supplier_pick,
            est_price=price,
            confidence=confidence,
            reason=reason,
            risk_flags=flags,
            mock=True,
            duration_s=time.perf_counter() - start,
        )
        return rec


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
