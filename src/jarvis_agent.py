"""Jarvis 에이전트 레이어 — tool-use 루프 기반 ①Q&A / ②자율 조사 에이전트.

기존 시나리오 1-3 (agent.py / payment.py / purchase.py) 은 **workflow** 다:
코드가 정해진 순서로 LLM 을 1회 호출. 예측 가능한 흐름이라 그게 맞다.

이 모듈은 **agent** 다: LLM 이 매 스텝 "다음에 어떤 도구를 쓸지" 스스로 결정하는
tool-use 루프. 질문/조사 경로를 미리 짤 수 없는 영역이라 agent 가 정당하다.
설계 출처: Anthropic "Building Effective Agents" (workflow vs agent 구분).

두 에이전트:
  ① QAAgent          — 실무자의 자연어 질문에 도구를 써서 답한다 (on-demand).
  ② InvestigationAgent — 이상 징후를 자율적으로 조사해 근본원인 가설 + 권고를 낸다.
                        ★ 절대 행동(발주/매칭/전표)하지 않는다 — 권고에서 멈춘다.
                        구조적 보장: agent_tools 에 쓰기 도구가 아예 없음.

관측(observability): AgentTrace 가 모든 스텝(도구 호출/결과/토큰/시간)을 기록 →
"됐다고 주장" 이 아니라 "어떤 경로로 답에 도달했는지" 를 보여준다.

mock 모드: LLM 판단을 휴리스틱으로 대체하되 **도구 호출은 진짜**(실 SQLite).
→ API 키 없이도 동일한 trace 구조로 데모 가능. 기존 agent.py 의 mock 패턴과 동일.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .agent_tools import TOOL_SCHEMAS, dispatch

DEFAULT_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
MAX_ITERS = 6          # 자율 루프 무한 방지 (안전 가드)
MAX_TOKENS = 800


# ─────────────────────────────────────────────
# 관측 레이어
# ─────────────────────────────────────────────

@dataclass
class AgentStep:
    kind: str                 # "tool_call" | "tool_result" | "answer" | "note"
    name: str = ""
    detail: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    duration_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "name": self.name, "detail": self.detail,
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "duration_s": round(self.duration_s, 3),
        }


@dataclass
class AgentTrace:
    """에이전트 1회 실행의 전체 발자취. 데모에서 '경로' 를 보여주는 핵심."""
    label: str
    model: str = ""
    mock: bool = False
    steps: list[AgentStep] = field(default_factory=list)
    error: str | None = None

    def add(self, kind: str, name: str = "", detail: str = "",
            input_tokens: int = 0, output_tokens: int = 0, duration_s: float = 0.0) -> None:
        self.steps.append(AgentStep(kind, name, detail, input_tokens, output_tokens, duration_s))

    @property
    def tool_calls(self) -> int:
        return sum(1 for s in self.steps if s.kind == "tool_call")

    @property
    def total_input_tokens(self) -> int:
        return sum(s.input_tokens for s in self.steps)

    @property
    def total_output_tokens(self) -> int:
        return sum(s.output_tokens for s in self.steps)

    @property
    def total_duration_s(self) -> float:
        return sum(s.duration_s for s in self.steps)

    def render(self) -> str:
        """사람이 읽는 trace (콘솔/데모용)."""
        icon = {"tool_call": "🔧", "tool_result": "📥", "answer": "💬", "note": "ⓘ"}
        lines = [f"┌─ trace: {self.label}  [{'MOCK' if self.mock else self.model}]"]
        for i, s in enumerate(self.steps, 1):
            tok = ""
            if s.input_tokens or s.output_tokens:
                tok = f"  (in {s.input_tokens} / out {s.output_tokens} tok, {s.duration_s:.2f}s)"
            head = f"│ [{i}] {icon.get(s.kind, '·')} {s.kind:11s}"
            if s.name:
                head += f" {s.name}"
            lines.append(head + tok)
            if s.detail:
                detail = s.detail if len(s.detail) <= 200 else s.detail[:200] + "…"
                lines.append(f"│      {detail}")
        lines.append(
            f"└─ 도구 {self.tool_calls}회 / 토큰 in {self.total_input_tokens} out {self.total_output_tokens}"
            f" / {self.total_duration_s:.2f}s"
            + (f" / ⚠ {self.error}" if self.error else "")
        )
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label, "model": self.model, "mock": self.mock,
            "tool_calls": self.tool_calls,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_duration_s": round(self.total_duration_s, 3),
            "error": self.error,
            "steps": [s.to_dict() for s in self.steps],
        }


def _short(result: dict[str, Any]) -> str:
    """tool_result 를 trace 에 넣을 1줄 요약 (전체 JSON 은 너무 김)."""
    if "error" in result:
        return f"오류: {result['error']}"
    if "matches" in result:
        ms = result["matches"]
        top = ms[0] if ms else None
        return f"{len(ms)}건 매칭" + (f" (top: {top.get('name', top.get('name_as_received', '?'))}, "
                                       f"score={top.get('match_score')})" if top else "")
    if "breach_count" in result:
        return f"안전재고 미만 {result['breach_count']}개 품목"
    if "outstanding_count" in result:
        return f"{result['partner_name']}: 미수 {result['outstanding_count']}건 / {result['outstanding_total_fmt']}"
    if "unmatched_count" in result:
        return f"미매칭 입금 {result['unmatched_count']}건 / {result['unmatched_total_fmt']}"
    if "sku" in result:
        return f"{result['sku']} 재고 {result.get('total_stock')}/{result.get('safety_stock')}"
    return json.dumps(result, ensure_ascii=False)[:160]


# ─────────────────────────────────────────────
# 공유 tool-use 루프 (real LLM)
# ─────────────────────────────────────────────

def _run_tool_loop(db, *, system: str, user: str, trace: AgentTrace,
                   model: str, max_iters: int = MAX_ITERS, max_tokens: int = MAX_TOKENS,
                   history: list[dict[str, Any]] | None = None) -> str:
    """수동 tool-use 루프. Claude 가 tool_use 를 멈출 때까지 도구를 실행하며 돈다.

    수동 루프인 이유: 매 스텝을 trace 에 기록하고(관측), max_iters 로 무한루프를 막기 위함.
    (SDK 의 자동 tool runner 를 쓰면 이 가시성/제어가 사라짐 — 관측·디버깅이 어려움.)
    """
    from anthropic import Anthropic

    client = Anthropic()
    # history = 이전 대화 턴(user/assistant 텍스트) → 멀티턴 컨텍스트 유지
    messages: list[dict[str, Any]] = list(history or []) + [{"role": "user", "content": user}]

    for _ in range(max_iters):
        t0 = time.perf_counter()
        resp = client.messages.create(
            model=model, max_tokens=max_tokens, system=system,
            tools=TOOL_SCHEMAS, messages=messages,
        )
        dt = time.perf_counter() - t0
        in_tok, out_tok = resp.usage.input_tokens, resp.usage.output_tokens

        if resp.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            first = True
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                # 이번 API 라운드의 토큰/시간은 그 라운드가 만든 첫 tool_call 에 귀속
                trace.add("tool_call", name=block.name,
                          detail=json.dumps(block.input, ensure_ascii=False),
                          input_tokens=in_tok if first else 0,
                          output_tokens=out_tok if first else 0,
                          duration_s=dt if first else 0.0)
                first = False
                result = dispatch(db, block.name, block.input)
                trace.add("tool_result", name=block.name, detail=_short(result))
                results.append({
                    "type": "tool_result", "tool_use_id": block.id,
                    "content": json.dumps(result, ensure_ascii=False),
                })
            messages.append({"role": "user", "content": results})
            continue

        # 최종 답변 (도구 호출 없음)
        answer = "".join(b.text for b in resp.content if b.type == "text")
        trace.add("answer", detail=answer, input_tokens=in_tok, output_tokens=out_tok, duration_s=dt)
        return answer

    trace.add("note", name="max_iters", detail=f"최대 반복 {max_iters} 도달 — 안전 중단")
    trace.error = "max_iters_reached"
    return ""


# ─────────────────────────────────────────────
# mock 브레인 헬퍼 — LLM 판단을 휴리스틱으로 대체 (도구 호출은 진짜)
# ─────────────────────────────────────────────

_STOPWORDS = re.compile(
    r"(미수금|미수|받을|외상|입금|얼마|총액|현황|건수|몇\s*건|몇\s*개|있어|있나|있었|들어온|들어왔|"
    r"알려줘|보여줘|줘|좀|현재|지금|확인|체크|어때|어떻게|왜|해줘|에서|에게|의|은|는|이|가|을|를|"
    r"야|뭐|뭔|인가|인지|니까|되나|\?|\.)"
)
_SKU_RE = re.compile(r"KS-[A-Z]+-\d{4}")


def _partner_query(question: str) -> str:
    """질문에서 거래처명 후보 추출 (mock 전용 — 불용어 제거 후 남은 한글 토큰)."""
    cleaned = _STOPWORDS.sub(" ", question)
    tokens = [t for t in cleaned.split() if t]
    return " ".join(tokens).strip() or question


def _mock_tool(db, trace: AgentTrace, name: str, **kwargs) -> dict[str, Any]:
    """mock 에서 도구 1회 호출 — trace 기록 + 진짜 dispatch. (토큰 0, 시간만 실측)"""
    t0 = time.perf_counter()
    trace.add("tool_call", name=name, detail=json.dumps(kwargs, ensure_ascii=False))
    result = dispatch(db, name, kwargs)
    trace.steps[-1].duration_s = time.perf_counter() - t0
    trace.add("tool_result", name=name, detail=_short(result))
    return result


# ─────────────────────────────────────────────
# ① Q&A 에이전트
# ─────────────────────────────────────────────

QA_SYSTEM = """당신은 건축자재 중소기업의 사내 운영 데이터 분석가 'Jarvis' 다.
실무자가 협업툴 채팅에서 던지는 자연어 질문에, 주어진 read-only 도구로 ERP 를 조회해 답한다.

답변 스타일 (협업툴 채팅이라 짧게):
- **핵심 답을 1~2문장으로 먼저.** 표나 긴 설명은 쓰지 않는다 (사용자가 명시적으로 요청할 때만).
- 핵심 수치는 *굵게*, 숫자엔 단위(원/건/개).
- 거래처명이 여러 건 매칭되면, 후보를 **한 줄**로만 보여주고 "어느 곳인가요?" 라고 짧게 되묻는다 (추측 금지).
  단 후보 중 한 곳만 의미 있는 값이면 그곳으로 답하고 한 줄로 부연한다.

규칙:
1. 모르면 추측 X — 도구로 조회한 사실만.
2. 거래처 질문은 search_partner 로 후보 확인 후 상세 도구 사용.
3. 조회만 한다 (발주/매칭/전표 등 행동 X).
4. 도구로도 모르면 '데이터로 확인 불가 — 담당자 확인 필요' 라고 솔직히."""


@dataclass
class AgentResult:
    question: str
    answer: str
    trace: AgentTrace
    mock: bool

    def to_dict(self) -> dict[str, Any]:
        return {"question": self.question, "answer": self.answer,
                "mock": self.mock, "trace": self.trace.to_dict()}


class QAAgent:
    def __init__(self, model: str | None = None, mock: bool = False) -> None:
        self.model = model or DEFAULT_MODEL
        self.mock = mock or not os.getenv("ANTHROPIC_API_KEY")

    def ask(self, db, question: str, history: list[dict] | None = None) -> AgentResult:
        trace = AgentTrace(label=f"Q&A: {question}", model=self.model, mock=self.mock)
        if self.mock:
            answer = self._mock_ask(db, question, trace)  # mock 은 무상태 (history 미사용)
        else:
            try:
                answer = _run_tool_loop(db, system=QA_SYSTEM, user=question, trace=trace,
                                        model=self.model, history=history)
            except Exception as e:  # noqa: BLE001
                trace.error = f"{type(e).__name__}: {e}"
                answer = ""
        return AgentResult(question=question, answer=answer, trace=trace, mock=self.mock)

    # ---- mock 브레인 — LLM 판단을 휴리스틱으로 대체 (도구 호출은 진짜) ----
    def _mock_ask(self, db, question: str, trace: AgentTrace) -> str:
        q = question.lower()
        # 1) 안전재고 / 발주
        if any(k in question for k in ("안전재고", "재고 부족", "발주", "모자란", "부족한 품목")):
            r = _mock_tool(db, trace, "list_safety_stock_breaches")
            n = r["breach_count"]
            top = r["items"][:3]
            tops = ", ".join(f"{it['sku']}({it['total_stock']}/{it['safety_stock']})" for it in top)
            ans = f"현재 안전재고 미만 품목은 {n}개입니다." + (f" 부족 큰 순: {tops}." if top else "")
            trace.add("answer", detail=ans)
            return ans
        # 2) 특정 SKU
        m = _SKU_RE.search(question)
        if m:
            r = _mock_tool(db, trace, "get_item_detail", sku=m.group(0))
            if "error" in r:
                ans = f"{m.group(0)} 은 데이터에 없습니다."
            else:
                ans = (f"{r['sku']} ({r['name']}): 현재고 {r['total_stock']} / 안전재고 {r['safety_stock']}"
                       f" — {'안전재고 미만' if r['below_safety'] else '정상'}.")
            trace.add("answer", detail=ans)
            return ans
        # 3) 미매칭 입금 현황 (특정 거래처 언급 없을 때)
        if any(k in question for k in ("미매칭", "매칭 안", "매칭안", "안 된 입금", "입금 현황", "미확인 입금")):
            r = _mock_tool(db, trace, "payment_match_stats")
            ans = f"아직 매칭되지 않은 입금은 {r['unmatched_count']}건, 합계 {r['unmatched_total_fmt']} 입니다."
            trace.add("answer", detail=ans)
            return ans
        # 4) 거래처 기반 (입금 도착 / 미수금)
        pq = _partner_query(question)
        sp = _mock_tool(db, trace, "search_partner", query=pq)
        if sp["matches"] and sp["matches"][0]["match_score"] >= 70:
            best = sp["matches"][0]
            if any(k in question for k in ("입금", "받았", "들어온", "송금")):
                r = _mock_tool(db, trace, "find_unmatched_payments_by_partner", query=best["name"])
                if r["matches"]:
                    tot = sum(x["amount"] for x in r["matches"])
                    ans = (f"'{best['name']}' 로 보이는 미매칭 입금 {len(r['matches'])}건"
                           f" (합계 {tot:,}원) 이 있습니다. 입금매칭 확인이 필요합니다.")
                else:
                    ans = f"'{best['name']}' 명의로 매칭 대기 중인 입금은 없습니다."
            else:
                r = _mock_tool(db, trace, "get_partner_receivables", partner_id=best["partner_id"])
                ans = (f"{r['partner_name']} 의 미수금은 {r['outstanding_count']}건, "
                       f"총 {r['outstanding_total_fmt']} 입니다 (결제조건 {r['payment_terms']}).")
            trace.add("answer", detail=ans)
            return ans
        # 5) fallback
        ans = "데이터로 확인하기 어려운 질문입니다 — 담당자 확인이 필요합니다."
        trace.add("answer", detail=ans)
        return ans


# ─────────────────────────────────────────────
# ② 자율 조사 에이전트 (LLM 이 조사 경로 결정 / 행동 X)
# ─────────────────────────────────────────────

INVESTIGATE_SYSTEM = """당신은 건축자재 중소기업의 운영 데이터를 조사하는 자율 분석가 'Jarvis' 다.
주어진 '조사 주제' 에 대해, 어떤 도구를 어떤 순서로 쓸지 스스로 판단해 근본 원인 가설과 권고를 만든다.

행동 원칙 (절대 위반 금지):
- 너는 조회(read-only)만 한다. 발주/입금매칭/전표 등 데이터를 바꾸는 행동은 절대 하지 않는다.
- 너의 결과물은 '가설 + 근거 수치 + 권고' 다. 실행은 사람이 승인한다.
- 근거 없는 단정 금지. 본 수치만 근거로 한다.

조사 절차:
1. 주제와 관련된 도구를 골라 사실을 모은다 (필요하면 여러 번, 결과를 보고 다음 도구를 정한다).
2. 충분하다고 판단되면 멈추고 아래 형식으로 답한다.

출력 형식 (한국어):
[가설] 한 문장
[근거] 조회로 확인한 수치 bullet 2-4개
[권고] 사람이 할 다음 액션 (예: '입금매칭 시나리오 실행 후 재집계')
마지막 줄에 정확히: CONFIDENCE: 0.NN"""


@dataclass
class InvestigationResult:
    topic: str
    hypothesis: str
    evidence: list[str]
    recommendation: str
    confidence: float
    trace: AgentTrace
    mock: bool
    full_text: str = ""   # 자유서술(real LLM 풍부한 리포트) 원문 보존
    acted: bool = False   # 항상 False — 조사 에이전트는 행동하지 않는다

    def render(self) -> str:
        acted_str = "실행함" if self.acted else "실행 안 함(권고에서 멈춤)"
        conf_str = f"{self.confidence:.0%}" if self.confidence >= 0 else "미표기"
        if self.evidence:  # 구조화됨 (mock 또는 파싱 성공)
            ev = "\n".join(f"  - {e}" for e in self.evidence)
            return (f"[가설] {self.hypothesis}\n[근거]\n{ev}\n[권고] {self.recommendation}\n"
                    f"신뢰도 {conf_str} · 행동 여부: {acted_str}")
        # 자유서술 — real LLM 의 풍부한 마크다운 리포트 원문 + 행동 여부 명시
        return f"{self.full_text}\n\n· 신뢰도 {conf_str} · 행동 여부: {acted_str}"

    def to_dict(self) -> dict[str, Any]:
        return {"topic": self.topic, "hypothesis": self.hypothesis, "evidence": self.evidence,
                "recommendation": self.recommendation,
                "confidence": round(self.confidence, 3) if self.confidence >= 0 else None,
                "full_text": self.full_text,
                "acted": self.acted, "mock": self.mock, "trace": self.trace.to_dict()}


class InvestigationAgent:
    def __init__(self, model: str | None = None, mock: bool = False) -> None:
        self.model = model or DEFAULT_MODEL
        self.mock = mock or not os.getenv("ANTHROPIC_API_KEY")

    def investigate(self, db, topic: str) -> InvestigationResult:
        trace = AgentTrace(label=f"조사: {topic}", model=self.model, mock=self.mock)
        if self.mock:
            return self._mock_investigate(db, topic, trace)
        try:
            text = _run_tool_loop(db, system=INVESTIGATE_SYSTEM, user=f"조사 주제: {topic}",
                                  trace=trace, model=self.model, max_iters=MAX_ITERS, max_tokens=1000)
        except Exception as e:  # noqa: BLE001
            trace.error = f"{type(e).__name__}: {e}"
            text = ""
        return self._parse_investigation(topic, text, trace)

    def _parse_investigation(self, topic: str, text: str, trace: AgentTrace) -> InvestigationResult:
        conf = -1.0  # 미표기 sentinel (real LLM 이 CONFIDENCE 형식 안 지킬 수 있음)
        m = re.search(r"CONFIDENCE:\s*([0-9.]+)", text)
        if m:
            try:
                conf = float(m.group(1))
            except ValueError:
                conf = -1.0
        hm = re.search(r"\[?\s*가설\s*\]?\s*[:\-]?\s*(.+)", text)
        hyp = hm.group(1).strip() if hm else ""
        rm = re.search(r"\[?\s*권고\s*\]?\s*[:\-]?\s*(.+)", text)
        rec = rm.group(1).strip() if rm else ""
        # real LLM 은 자유 마크다운으로 답함 → 억지 구조화 X. evidence 비우고 full_text 로 원문 보존.
        # (render 가 evidence 없으면 full_text 를 그대로 출력). 행동 여부(acted=False)는 항상 유지.
        return InvestigationResult(
            topic=topic, hypothesis=hyp or "(자유 서술 — 전문 참조)",
            evidence=[], recommendation=rec or text,
            confidence=conf, trace=trace, mock=self.mock, full_text=text, acted=False,
        )

    # ---- mock 브레인 — 주제별 자율 조사 경로 시뮬레이션 ----
    def _mock_investigate(self, db, topic: str, trace: AgentTrace) -> InvestigationResult:
        if any(k in topic for k in ("미수", "받을", "외상", "입금", "매칭")):
            stats = _mock_tool(db, trace, "payment_match_stats")
            n, tot = stats["unmatched_count"], stats["unmatched_total_fmt"]
            big = sorted(stats["samples"], key=lambda x: -x["amount"])[:2]
            ev = [f"미매칭 입금 {n}건 / 합계 {tot}",
                  "표본 중 거래처 표기 변형으로 자동 연결 안 된 건 다수 (입금자명 ≠ 장부 거래처명)"]
            if big:
                ev.append("큰 미매칭 예: " + ", ".join(f"{b['name_as_received']} {b['amount']:,}원" for b in big))
            res = InvestigationResult(
                topic=topic,
                hypothesis="장부상 미수금이 커 보이는 주원인은 '실제 미입금' 보다 '입금-청구 미매칭 누적' 일 가능성이 높다.",
                evidence=ev,
                recommendation="입금매칭 시나리오(②)를 먼저 돌려 미매칭을 해소한 뒤 미수금을 재집계. 그래도 남는 건만 추심 대상.",
                confidence=0.72, trace=trace, mock=True,
            )
        elif any(k in topic for k in ("재고", "발주", "품절", "부족")):
            br = _mock_tool(db, trace, "list_safety_stock_breaches")
            worst = br["items"][:1]
            ev = [f"안전재고 미만 {br['breach_count']}개 품목"]
            if worst:
                w = worst[0]
                detail = _mock_tool(db, trace, "get_item_detail", sku=w["sku"])
                of = detail.get("past_4w_outflow", [])
                ev.append(f"최다 부족 {w['sku']}: 현재고 {w['total_stock']}/안전 {w['safety_stock']}, 최근4주 출고 {of}")
                ev.append("출고 이력 유무가 품목마다 갈림 → 일부는 실수요, 일부는 ERP 입력 누락 의심")
            res = InvestigationResult(
                topic=topic,
                hypothesis="안전재고 미만 품목 중 일부는 실제 수요, 일부는 출고 입력 누락으로 '가짜 부족' 일 수 있다.",
                evidence=ev,
                recommendation="출고 이력 있는 품목만 발주 검토 대상으로, 이력 없는 품목은 ERP 입력 확인 요청(자동 발주 금지).",
                confidence=0.68, trace=trace, mock=True,
            )
        else:
            stats = _mock_tool(db, trace, "payment_match_stats")
            br = _mock_tool(db, trace, "list_safety_stock_breaches")
            res = InvestigationResult(
                topic=topic,
                hypothesis="운영 리스크는 재무(미매칭 입금)와 재고(안전재고 미만) 양쪽에 분산돼 있다.",
                evidence=[f"미매칭 입금 {stats['unmatched_count']}건 / {stats['unmatched_total_fmt']}",
                          f"안전재고 미만 {br['breach_count']}개 품목"],
                recommendation="재무·재고 각각 담당 시나리오로 1차 정리 후, 남는 예외만 사람 검토.",
                confidence=0.6, trace=trace, mock=True,
            )
        trace.add("answer", detail=res.render())
        return res
