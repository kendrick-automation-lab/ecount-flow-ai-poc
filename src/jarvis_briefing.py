"""③ 멀티에이전트 아침 브리핑 — orchestrator-workers 패턴.

왜 멀티에이전트가 정당한가 (면접 방어):
- 세 분석(재고 건강 / 미매칭 입금 / 미수 집중 거래처)은 서로 **독립**이고 **병렬** 가능하다.
- 각 워커는 자기 도메인 도구에만 집중한다 (좁은 컨텍스트 = 더 정확).
- orchestrator 는 세 결과를 모아 하나의 브리핑으로 종합한다.
→ 이것이 Anthropic 이 말하는 orchestrator-workers. 한 에이전트로 쪼갤 일을 억지로
  나눈 게 아니라, 원래 독립인 일을 병렬화한 것.

대비 (정직):
- 시나리오 1-3 / Q&A / 조사 와 **같은 read-only 도구 레이어**(agent_tools)를 공유한다.
- 워커도 조회만 한다 (행동 X). 브리핑은 '오늘 사람이 볼 요약' 이지 자동 실행이 아니다.
- 병렬성은 jarvis_core 와 동일하게 asyncio.gather + to_thread (CPU-bound 아닌 I/O 대기).
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .agent_tools import TOOL_SCHEMAS
from .flow import CHANNEL_OPS, FlowClient
from .jarvis_agent import DEFAULT_MODEL, AgentTrace, _mock_tool, _run_tool_loop

# Slack 멀티채널 발송 매핑 (워커 → 업무 채널). 봇이 각 채널에 초대돼 있어야 함.
WORKER_CHANNEL = {"inventory": "#재고", "finance": "#재무", "receivables": "#구매"}
JARVIS_CHANNEL = "#jarvis"


# ─────────────────────────────────────────────
# 워커 정의 — 각자 도메인 + 집중 도구 + mock 섹션 작성기
# ─────────────────────────────────────────────

@dataclass
class WorkerSpec:
    key: str
    title: str                 # 표시명 (예: "재고 분석가")
    system: str                # real 모드 system prompt
    user: str                  # real 모드 조사 지시
    mock_fn: Callable          # (db, trace) -> str (섹션 텍스트)


def _mock_inventory(db, trace: AgentTrace) -> str:
    r = _mock_tool(db, trace, "list_safety_stock_breaches")
    n = r["breach_count"]
    top = r["items"][:3]
    tops = ", ".join(f"{it['sku']}(부족 {it['shortage']})" for it in top if it["shortage"] > 0)
    line = f"안전재고 미만 {n}개 품목."
    if tops:
        line += f" 부족 큰 순: {tops}."
    line += " → 발주 검토 후보 (실행 전 출고이력·중복발주 확인 필요)."
    return line


def _mock_finance(db, trace: AgentTrace) -> str:
    r = _mock_tool(db, trace, "payment_match_stats")
    return (f"미매칭 입금 {r['unmatched_count']}건 / 합계 {r['unmatched_total_fmt']}. "
            f"→ 입금매칭 시나리오로 1차 정리 권장 (장부 미수 과대 계상 방지).")


def _mock_receivables(db, trace: AgentTrace) -> str:
    r = _mock_tool(db, trace, "top_receivable_partners", limit=3)
    rows = r["top"]
    if not rows:
        return "미수금 거래처 없음."
    tops = " / ".join(f"{x['partner_name']} {x['outstanding_total_fmt']}({x['outstanding_count']}건)" for x in rows)
    return (f"미수 집중 거래처 Top{len(rows)}: {tops}. "
            f"(전체 미수 보유 거래처 {r['partner_count_with_receivables']}곳) → 회수 우선순위 참고.")


WORKERS: list[WorkerSpec] = [
    WorkerSpec(
        key="inventory", title="재고 분석가",
        system="너는 재고 담당 분석가다. read-only 도구로 안전재고 미만 품목을 파악해, "
               "오늘 브리핑에 넣을 2-3문장 한국어 요약을 만든다. 발주는 '검토 후보' 로만 제시(실행 X). 숫자 근거 필수.",
        user="오늘 재고 건강 상태를 점검해 브리핑 섹션을 써라.",
        mock_fn=_mock_inventory,
    ),
    WorkerSpec(
        key="finance", title="재무 분석가",
        system="너는 재무 담당 분석가다. read-only 도구로 미매칭 입금 현황을 파악해, "
               "오늘 브리핑에 넣을 2-3문장 한국어 요약을 만든다. 행동은 권고만(실행 X). 숫자 근거 필수.",
        user="오늘 미매칭 입금 현황을 점검해 브리핑 섹션을 써라.",
        mock_fn=_mock_finance,
    ),
    WorkerSpec(
        key="receivables", title="채권 분석가",
        system="너는 채권 담당 분석가다. read-only 도구로 미수금이 큰 거래처를 파악해, "
               "오늘 브리핑에 넣을 2-3문장 한국어 요약을 만든다. 회수 우선순위 참고용으로만 제시. 숫자 근거 필수.",
        user="오늘 미수 집중 거래처를 점검해 브리핑 섹션을 써라.",
        mock_fn=_mock_receivables,
    ),
]


# ─────────────────────────────────────────────
# 결과 컨테이너
# ─────────────────────────────────────────────

@dataclass
class WorkerResult:
    key: str
    title: str
    section: str
    trace: AgentTrace
    mock: bool

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "title": self.title, "section": self.section,
                "mock": self.mock, "trace": self.trace.to_dict()}


@dataclass
class BriefingReport:
    workers: list[WorkerResult] = field(default_factory=list)
    briefing: str = ""
    duration_s: float = 0.0
    mock: bool = True
    model: str = ""
    synth_trace: AgentTrace | None = None

    @property
    def total_input_tokens(self) -> int:
        t = sum(w.trace.total_input_tokens for w in self.workers)
        return t + (self.synth_trace.total_input_tokens if self.synth_trace else 0)

    @property
    def total_output_tokens(self) -> int:
        t = sum(w.trace.total_output_tokens for w in self.workers)
        return t + (self.synth_trace.total_output_tokens if self.synth_trace else 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": "briefing", "mock": self.mock, "model": self.model,
            "duration_s": round(self.duration_s, 3),
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "briefing": self.briefing,
            "workers": [w.to_dict() for w in self.workers],
            "synth_trace": self.synth_trace.to_dict() if self.synth_trace else None,
        }


# ─────────────────────────────────────────────
# orchestrator
# ─────────────────────────────────────────────

class BriefingOrchestrator:
    def __init__(self, model: str | None = None, mock: bool = False) -> None:
        self.model = model or DEFAULT_MODEL
        self.mock = mock or not os.getenv("ANTHROPIC_API_KEY")

    def _run_worker(self, db, spec: WorkerSpec) -> WorkerResult:
        trace = AgentTrace(label=f"worker:{spec.key}", model=self.model, mock=self.mock)
        if self.mock:
            section = spec.mock_fn(db, trace)
            trace.add("answer", detail=section)
        else:
            try:
                section = _run_tool_loop(db, system=spec.system, user=spec.user,
                                         trace=trace, model=self.model, max_iters=4, max_tokens=500)
            except Exception as e:  # noqa: BLE001
                trace.error = f"{type(e).__name__}: {e}"
                section = f"({spec.title} 분석 실패: {trace.error})"
        return WorkerResult(key=spec.key, title=spec.title, section=section, trace=trace, mock=self.mock)

    def _synthesize(self, results: list[WorkerResult]) -> tuple[str, AgentTrace | None]:
        """세 워커 섹션 → 하나의 아침 브리핑. mock=템플릿 / real=종합 LLM 1콜."""
        sections = "\n".join(f"[{w.title}] {w.section}" for w in results)
        if self.mock:
            body = "\n".join(f"{i}. *{w.title}* — {w.section}" for i, w in enumerate(results, 1))
            briefing = (
                "🌅 *Jarvis 아침 브리핑* (멀티에이전트 종합)\n"
                f"{body}\n"
                "— 위 항목은 read-only 분석 결과입니다. 실제 발주/매칭/추심은 담당자 승인 후 진행됩니다."
            )
            return briefing, None
        # real: 종합 LLM 콜 (도구 없음)
        trace = AgentTrace(label="synthesize", model=self.model, mock=False)
        try:
            from anthropic import Anthropic

            t0 = time.perf_counter()
            client = Anthropic()
            resp = client.messages.create(
                model=self.model, max_tokens=600,
                system="너는 중소기업 운영 비서 Jarvis 다. 세 분석가의 섹션을 받아 사장/실무자가 "
                       "아침에 30초에 읽을 브리핑으로 종합한다. 한국어. 과장 없이 수치 중심. "
                       "마지막에 '실행은 담당자 승인 후' 한 줄.",
                messages=[{"role": "user", "content": f"세 분석가 섹션:\n{sections}\n\n아침 브리핑으로 종합해줘."}],
            )
            trace.add("answer", detail="(종합)",
                      input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens,
                      duration_s=time.perf_counter() - t0)
            briefing = "🌅 *Jarvis 아침 브리핑*\n" + "".join(b.text for b in resp.content if b.type == "text")
            return briefing, trace
        except Exception as e:  # noqa: BLE001
            trace.error = f"{type(e).__name__}: {e}"
            return "🌅 *Jarvis 아침 브리핑* (종합 실패 — 워커 섹션 원문)\n" + sections, trace

    async def run(self, db, flow: FlowClient) -> BriefingReport:
        start = time.perf_counter()
        # 세 워커 병렬 실행 (서로 독립 → 동시) — jarvis_core 와 동일 패턴
        results = await asyncio.gather(
            *(asyncio.to_thread(self._run_worker, db, spec) for spec in WORKERS)
        )
        briefing, synth_trace = self._synthesize(list(results))
        flow.send_message(CHANNEL_OPS, briefing)
        return BriefingReport(
            workers=list(results), briefing=briefing,
            duration_s=time.perf_counter() - start, mock=self.mock,
            model=self.model, synth_trace=synth_trace,
        )


# ─────────────────────────────────────────────
# CLI 진입 (main.py 가 호출)
# ─────────────────────────────────────────────

def run_briefing_cli(args, open_db) -> int:
    import json
    import sys
    from pathlib import Path

    db = open_db(args)
    if db is None:
        return 2
    flow = FlowClient.from_sample(args.messages)
    orch = BriefingOrchestrator(mock=args.mock)
    print(f"[i] 멀티에이전트 브리핑 — 모드: {'MOCK' if orch.mock else f'LLM ({orch.model})'}"
          f"  / 워커 {len(WORKERS)}개 병렬\n", file=sys.stderr)

    report = asyncio.run(orch.run(db, flow))

    if not getattr(args, "no_trace", False):
        for w in report.workers:
            print(w.trace.render())
    print("\n" + "=" * 60, file=sys.stderr)
    print(f"[Summary — 멀티에이전트 브리핑] 워커 {len(report.workers)}개 / {report.duration_s:.2f}s"
          f" / 토큰 in {report.total_input_tokens} out {report.total_output_tokens}", file=sys.stderr)

    if getattr(args, "notify", "none") == "slack":
        try:
            from .slack_bot import SlackBot
            bot = SlackBot.from_env()
            sent = []
            for w in report.workers:  # 각 도메인 요약 → 해당 업무 채널
                ch = WORKER_CHANNEL.get(w.key, JARVIS_CHANNEL)
                if bot.post(ch, f"*[{w.title}]*\n{w.section}"):
                    sent.append(ch)
            if bot.post(JARVIS_CHANNEL, report.briefing):  # 종합 → #jarvis
                sent.append(JARVIS_CHANNEL)
            print(f"[i] Slack 멀티채널 발송: {', '.join(sent) or '실패'}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"[!] Slack 발송 실패: {e}", file=sys.stderr)

    if args.out:
        Path(args.out).write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[i] 리포트 저장: {args.out}", file=sys.stderr)
    return 0
