"""암묵지 룰 추출기 — Flow 대화에서 사내 판단 룰을 구조화.

AX 의 핵심 (그리고 제일 어려운) 파트 = "특정 직원 머릿속에만 있는 판단 기준을
데이터/프롬프트/자동화 흐름으로 전환" (암묵지의 구조화).

이 모듈이 하는 일:
  Flow 대화 로그 → "판단 룰" JSON 추출 → Jarvis 사이클 보고에 노출
  (시나리오 1 의 SKU 별 멘션 활용은 이미 있고, 이건 그걸 '전사 룰' 레벨로 일반화)

mock = 키워드 패턴 / LLM = Claude 가 대화에서 룰 추출.
⚠️ 추출된 룰은 '후보'다 — 실제 적용 전 담당자 확인 필수 (사람이 안 한 말이 룰이 되면 안 됨).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from .flow import FlowMessage

EXTRACT_SYSTEM = """당신은 사내 대화에서 '반복 적용되는 판단 룰'을 추출하는 전문가다.
입력: 협업툴 대화 로그.
출력: JSON 배열. 다른 텍스트 X.

[{"scope": "global|category|sku", "target": "대상 (전체면 null)",
  "condition": "조건", "action": "해야 할 일",
  "source": "발화자", "confidence": 0.0~1.0}]

규칙: 일회성 지시 말고 '반복 적용될 기준'만. 대화에 없는 룰을 지어내지 마라."""


@dataclass
class TacitRule:
    scope: str          # global / category / sku
    target: str | None  # 카테고리명 or SKU (global 이면 None)
    condition: str
    action: str
    source: str
    confidence: float = 0.7

    def to_dict(self) -> dict[str, Any]:
        return {"scope": self.scope, "target": self.target, "condition": self.condition,
                "action": self.action, "source": self.source, "confidence": self.confidence}

    def one_line(self) -> str:
        tgt = f"[{self.target}] " if self.target else "[전체] "
        return f"{tgt}{self.condition} → {self.action} (출처: {self.source})"


@dataclass
class TacitRuleExtractor:
    mock: bool = True
    model: str = ""

    def __post_init__(self) -> None:
        self.model = self.model or os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
        if not os.getenv("ANTHROPIC_API_KEY"):
            self.mock = True

    def extract(self, messages: list[FlowMessage]) -> list[TacitRule]:
        if self.mock:
            return self._mock_extract(messages)
        return self._llm_extract(messages)

    # mock — 키워드 패턴 (데모용. 실제 일반화는 LLM 모드가 담당)
    @staticmethod
    def _mock_extract(messages: list[FlowMessage]) -> list[TacitRule]:
        rules: list[TacitRule] = []
        for m in messages:
            t = m.text
            # 패턴 1: "단가 N% 이상 오르면 ... 보고"
            pm = re.search(r"(\d+)\s*%\s*이상\s*오르면", t)
            if pm and "보고" in t:
                rules.append(TacitRule(
                    scope="global", target=None,
                    condition=f"직전 단가 대비 {pm.group(1)}% 이상 인상 시",
                    action="발주 전 구매팀장 보고", source=m.author, confidence=0.85,
                ))
            # 패턴 2: "<카테고리> ... 안전재고를 ... N배"
            pm = re.search(r"(\S+재)\s*카테고리.*?([\d.]+)\s*배", t)
            if pm:
                rules.append(TacitRule(
                    scope="category", target=pm.group(1),
                    condition="시즌 대비 기간",
                    action=f"안전재고를 평소의 {pm.group(2)}배로 산정", source=m.author, confidence=0.8,
                ))
            # 패턴 3: SKU 보류 단서 (시나리오 1 의 per-SKU 멘션과 동일 원천 — 룰로도 승격)
            if "당분간 발주" in t or "단종 검토" in t:
                sku = next((w for w in t.split() if w.startswith("KS-")), None)
                rules.append(TacitRule(
                    scope="sku", target=sku,
                    condition="담당자 보류/단종 멘션 존재",
                    action="자동 발주 보류 + 담당자 확인", source=m.author, confidence=0.75,
                ))
        return rules

    def _llm_extract(self, messages: list[FlowMessage]) -> list[TacitRule]:
        try:
            from anthropic import Anthropic

            log = "\n".join(f"[{m.ts[:10]} {m.author}] {m.text}" for m in messages)
            client = Anthropic()
            resp = client.messages.create(
                model=self.model, max_tokens=700, system=EXTRACT_SYSTEM,
                messages=[{"role": "user", "content": f"[대화 로그]\n{log}"}],
            )
            raw = "".join(b.text for b in resp.content if b.type == "text")
            data = _parse_json_array(raw)
            return [TacitRule(**{**d, "target": d.get("target")}) for d in data]
        except Exception:
            return self._mock_extract(messages)  # LLM 실패 시 mock 폴백


def load_all_messages(flow) -> list[FlowMessage]:
    """FlowClient 의 샘플 전체 메시지 로드 (실제 운영: Flow API 최근 N일 fetch)."""
    data = flow._load()
    return [FlowMessage(**m) for m in data.get("recent_messages", [])]


def _parse_json_array(raw: str) -> list[dict]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return []
