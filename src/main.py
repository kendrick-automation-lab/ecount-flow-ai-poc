"""Entry point — ERP × 협업툴 AI 자동화 PoC 시나리오 실행.

사용:
  # 시나리오 1 — 안전재고 자동 발주 추천 (JSON 샘플 백엔드)
  python -m src.main --scenario inventory --mock --confirm-approve

  # 시나리오 2 — 입금매칭 (SQLite 더미 DB 백엔드)
  python scripts/db_seed.py --reset            # 최초 1회 DB 생성
  python -m src.main --scenario payment --mock --approve-all --out samples/payment_report.json

ANTHROPIC_API_KEY 가 .env 에 있으면 --mock 빼고 실제 LLM 모드.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

# Windows cp949 환경에서 한글/이모지 출력 안전: UTF-8 강제
for stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv

from .agent import InventoryAnalystAgent
from .decision import DecisionConfig, PaymentDecisionConfig
from .ecount import EcountClient
from .ecount_db import EcountDBClient
from .flow import FlowClient
from .orchestrator import run_cycle, run_payment_cycle
from .payment import PaymentMatchAgent

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="이카운트 + Flow 자동화 PoC (더미 환경 데모)")
    p.add_argument("--scenario",
                   choices=["inventory", "payment", "purchase", "all", "ask", "investigate", "briefing", "eval"],
                   default="inventory",
                   help="시나리오 1-3 (workflow) / all (Jarvis Core 병렬) / "
                        "ask·investigate·briefing (agent 레이어) / eval (평가 하네스)")
    p.add_argument("--mock", action="store_true", help="LLM mock 모드 강제 (API 키 있어도)")
    p.add_argument("--out", default=None, help="결과 JSON 저장 경로")
    # 시나리오 1 (안전재고)
    p.add_argument("--inventory", default=str(SAMPLES / "inventory.json"))
    p.add_argument("--messages", default=str(SAMPLES / "flow_messages.json"))
    p.add_argument("--confirm-approve", action="store_true", help="(시나리오 1) confirm 요청 SKU 자동 승인")
    p.add_argument("--auto-conf", type=float, default=0.95, help="(시나리오 1) 자동 실행 신뢰도 임계")
    p.add_argument("--auto-amount", type=int, default=100_000, help="(시나리오 1) 자동 실행 최대 금액")
    # 시나리오 2 (입금매칭)
    p.add_argument("--db", default=None, help="(시나리오 2/3) SQLite DB 경로 (기본: data/ks_jarvis_demo.db)")
    p.add_argument("--limit", type=int, default=None, help="(시나리오 2) 처리할 입금 건수 제한")
    p.add_argument("--approve-all", action="store_true", help="(시나리오 2/3) confirm 요청 자동 승인 (테스트)")
    # 시나리오 3 (구매입력)
    p.add_argument("--quote", default=str(SAMPLES / "sample_quote.txt"), help="(시나리오 3) 견적서 텍스트 파일 경로")
    # agent 레이어 (ask / investigate)
    p.add_argument("--question", default=None, help="(ask) 실무자 자연어 질문")
    p.add_argument("--topic", default=None, help="(investigate) 자율 조사 주제")
    p.add_argument("--no-trace", action="store_true", help="(ask/investigate) trace 출력 생략")
    p.add_argument("--backend", choices=["sqlite", "airtable"], default="sqlite",
                   help="에이전트 데이터 백엔드 (sqlite=더미 / airtable=실 API 연동)")
    p.add_argument("--notify", choices=["none", "slack"], default="none",
                   help="(briefing) 결과를 협업툴로 발송 (slack=실 webhook)")
    return p.parse_args()


def run_inventory(args: argparse.Namespace) -> int:
    ecount = EcountClient.from_sample(args.inventory)
    flow = FlowClient.from_sample(args.messages)
    agent = InventoryAnalystAgent(mock=args.mock)
    cfg = DecisionConfig(auto_confidence_min=args.auto_conf, auto_amount_max=args.auto_amount)

    breaches = ecount.list_safety_stock_breaches()
    print(f"[i] 안전재고 미만 SKU: {len(breaches)} / 전체: {len(ecount.list_items())}", file=sys.stderr)
    if not breaches:
        print("[i] 발주 추천 필요 없음.", file=sys.stderr)
        return 0
    for it in breaches:
        print(f"  - {it.sku} {it.name} : 재고 {it.total_stock} / 안전 {it.safety_stock}", file=sys.stderr)

    if args.confirm_approve:
        for it in breaches:
            flow.queue_user_response(it.sku, "approve")

    print(f"\n[i] Agent 모드: {'MOCK (휴리스틱)' if agent.mock else 'LLM (Claude)'}", file=sys.stderr)
    print(f"[i] Decision config: 자동 신뢰도≥{cfg.auto_confidence_min} & 금액≤{cfg.auto_amount_max:,}\n", file=sys.stderr)

    results = run_cycle(ecount, flow, agent, cfg)

    print("\n" + "=" * 60, file=sys.stderr)
    print("[Summary — 시나리오 1 안전재고]", file=sys.stderr)
    counts = Counter(r.decision.action for r in results)
    total_in = sum(r.recommendation.input_tokens for r in results)
    total_out = sum(r.recommendation.output_tokens for r in results)
    for action in ("auto_execute", "request_confirm", "manual_review", "skip"):
        print(f"  {action:18s}: {counts.get(action, 0)}", file=sys.stderr)
    print(f"  registered orders : {len(ecount.purchase_orders)}", file=sys.stderr)
    print(f"  tokens (in/out)   : {total_in} / {total_out}", file=sys.stderr)

    if args.out:
        report = {
            "scenario": "inventory",
            "summary": {**{k: counts.get(k, 0) for k in ("auto_execute", "request_confirm", "manual_review", "skip")},
                        "registered_orders": len(ecount.purchase_orders),
                        "total_input_tokens": total_in, "total_output_tokens": total_out},
            "cycles": [r.to_dict() for r in results],
            "purchase_orders": ecount.purchase_orders,
            "flow_messages": flow.sent_messages,
        }
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[i] 리포트 저장: {args.out}", file=sys.stderr)
    return 0


def run_payment(args: argparse.Namespace) -> int:
    from .db import DEFAULT_DB_PATH

    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
    if not db_path.exists():
        print(f"[!] DB 없음: {db_path}", file=sys.stderr)
        print("    먼저 실행: python scripts/db_seed.py --reset", file=sys.stderr)
        return 2

    db = EcountDBClient.from_db(db_path)
    flow = FlowClient.from_sample(args.messages)
    agent = PaymentMatchAgent(mock=args.mock)
    cfg = PaymentDecisionConfig()

    pending_all = db.list_pending_payments()
    pending = db.list_pending_payments(limit=args.limit)
    print(f"[i] 미매칭 입금: 전체 {len(pending_all)}건 중 {len(pending)}건 처리", file=sys.stderr)

    if args.approve_all:
        for pm in pending:
            flow.queue_user_response(f"pay-{pm.payment_id}", "approve")

    print(f"[i] Agent 모드: {'MOCK (휴리스틱)' if agent.mock else 'LLM (Claude)'}", file=sys.stderr)
    print(f"[i] Decision config: 자동 신뢰도≥{cfg.auto_confidence_min} / confirm≥{cfg.confirm_confidence_min}\n", file=sys.stderr)

    results = run_payment_cycle(db, flow, agent, cfg, limit=args.limit)

    print("\n" + "=" * 60, file=sys.stderr)
    print("[Summary — 시나리오 2 입금매칭]", file=sys.stderr)
    counts = Counter(r.decision.action for r in results)
    saved = sum(1 for r in results if r.action_result.get("match"))
    total_in = sum(r.recommendation.input_tokens for r in results)
    total_out = sum(r.recommendation.output_tokens for r in results)
    for action in ("auto_execute", "request_confirm", "manual_review"):
        print(f"  {action:18s}: {counts.get(action, 0)}", file=sys.stderr)
    print(f"  matches saved      : {saved}", file=sys.stderr)
    print(f"  남은 미매칭         : {len(db.list_pending_payments())}", file=sys.stderr)
    print(f"  tokens (in/out)    : {total_in} / {total_out}", file=sys.stderr)

    if args.out:
        report = {
            "scenario": "payment",
            "summary": {**{k: counts.get(k, 0) for k in ("auto_execute", "request_confirm", "manual_review")},
                        "matches_saved": saved, "remaining_unmatched": len(db.list_pending_payments()),
                        "total_input_tokens": total_in, "total_output_tokens": total_out},
            "cycles": [r.to_dict() for r in results],
        }
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[i] 리포트 저장: {args.out}", file=sys.stderr)
    return 0


def run_purchase(args: argparse.Namespace) -> int:
    from .db import DEFAULT_DB_PATH
    from .orchestrator import run_purchase_cycle

    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
    if not db_path.exists():
        print(f"[!] DB 없음: {db_path} — 먼저: python scripts/db_seed.py --reset", file=sys.stderr)
        return 2
    quote_path = Path(args.quote)
    if not quote_path.exists():
        print(f"[!] 견적서 파일 없음: {quote_path}", file=sys.stderr)
        return 2

    db = EcountDBClient.from_db(db_path)
    flow = FlowClient.from_sample(args.messages)
    quote_text = quote_path.read_text(encoding="utf-8")
    quote_ref = quote_path.stem

    if args.approve_all:
        flow.queue_user_response(f"po-{quote_ref}", "approve")

    print(f"[i] 견적서: {quote_path.name} / Agent 모드: {'MOCK' if (args.mock or not __import__('os').getenv('ANTHROPIC_API_KEY')) else 'LLM'}", file=sys.stderr)
    result = run_purchase_cycle(db, flow, quote_text, quote_ref, mock=args.mock)

    rec, dec = result.recommendation, result.decision
    print("\n" + "=" * 60, file=sys.stderr)
    print("[Summary — 시나리오 3 구매입력]", file=sys.stderr)
    print(f"  decision        : {dec.action} ({dec.reason})", file=sys.stderr)
    print(f"  거래처           : {rec.partner_name} (일치 {rec.partner_score:.0f})", file=sys.stderr)
    print(f"  라인 매칭        : {sum(1 for ln in rec.lines if ln.matched_sku)}/{len(rec.lines)}", file=sys.stderr)
    print(f"  총액 / 신뢰도    : {rec.total:,}원 / {rec.confidence:.0%}", file=sys.stderr)
    print(f"  entry 등록      : {result.action_result.get('entry', {}).get('entry_id', '-')}", file=sys.stderr)
    print(f"  tokens (in/out) : {rec.input_tokens} / {rec.output_tokens}", file=sys.stderr)

    if args.out:
        Path(args.out).write_text(json.dumps({"scenario": "purchase", "result": result.to_dict()},
                                             ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[i] 리포트 저장: {args.out}", file=sys.stderr)
    return 0


DEMO_QUESTIONS = [
    "지금 안전재고 미만인 품목 몇 개야?",
    "현대건자재 미수금 얼마야?",
    "미매칭 입금 현황 알려줘",
    "한솔산업 입금 들어온 거 있어?",
]
DEMO_TOPICS = [
    "지난달 미수금이 왜 이렇게 늘었는지 조사해줘",
    "재고 부족 품목 상황 점검해줘",
]


def _open_db(args: argparse.Namespace):
    from .db import DEFAULT_DB_PATH

    if getattr(args, "backend", "sqlite") == "airtable":
        from .airtable_client import AirtableClient
        try:
            client = AirtableClient.from_env()
            print(f"[i] 백엔드: Airtable (실 API) — base {client.base_id}", file=sys.stderr)
            return client
        except RuntimeError as e:
            print(f"[!] {e}", file=sys.stderr)
            return None

    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
    if not db_path.exists():
        print(f"[!] DB 없음: {db_path} — 먼저: python scripts/db_seed.py --reset", file=sys.stderr)
        return None
    return EcountDBClient.from_db(db_path)


def run_ask(args: argparse.Namespace) -> int:
    """① Q&A 에이전트 — 자연어 질문 → tool-use 루프 → 답변."""
    from .jarvis_agent import QAAgent

    db = _open_db(args)
    if db is None:
        return 2
    agent = QAAgent(mock=args.mock)
    questions = [args.question] if args.question else DEMO_QUESTIONS
    print(f"[i] Q&A 에이전트 — 모드: {'MOCK (휴리스틱 브레인 + 진짜 도구)' if agent.mock else f'LLM ({agent.model})'}\n",
          file=sys.stderr)

    results = []
    for q in questions:
        res = agent.ask(db, q)
        print(f"\nQ. {q}")
        print(f"A. {res.answer}")
        if not args.no_trace:
            print(res.trace.render())
        results.append(res.to_dict())

    if args.out:
        Path(args.out).write_text(json.dumps({"scenario": "ask", "results": results},
                                             ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[i] 리포트 저장: {args.out}", file=sys.stderr)
    return 0


def run_investigate(args: argparse.Namespace) -> int:
    """② 자율 조사 에이전트 — 주제 → 자율 도구 체인 → 가설+권고 (행동 안 함)."""
    from .jarvis_agent import InvestigationAgent

    db = _open_db(args)
    if db is None:
        return 2
    agent = InvestigationAgent(mock=args.mock)
    topics = [args.topic] if args.topic else DEMO_TOPICS
    print(f"[i] 자율 조사 에이전트 — 모드: {'MOCK' if agent.mock else f'LLM ({agent.model})'}"
          f"  (read-only 도구만 → 구조적으로 행동 불가)\n", file=sys.stderr)

    results = []
    for t in topics:
        res = agent.investigate(db, t)
        print(f"\n■ 조사 주제: {t}")
        print(res.render())
        if not args.no_trace:
            print(res.trace.render())
        results.append(res.to_dict())

    if args.out:
        Path(args.out).write_text(json.dumps({"scenario": "investigate", "results": results},
                                             ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[i] 리포트 저장: {args.out}", file=sys.stderr)
    return 0


def run_jarvis(args: argparse.Namespace) -> int:
    import asyncio as _asyncio

    from .db import DEFAULT_DB_PATH
    from .jarvis_core import run_jarvis_cycle

    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
    if not db_path.exists():
        print(f"[!] DB 없음: {db_path} — 먼저: python scripts/db_seed.py --reset", file=sys.stderr)
        return 2

    db = EcountDBClient.from_db(db_path)
    flow = FlowClient.from_sample(args.messages)
    quotes = sorted(SAMPLES.glob("quote_*.txt"))

    if args.approve_all:
        for it in db.list_safety_stock_breaches():
            flow.queue_user_response(it.sku, "approve")
        for pm in db.list_pending_payments(limit=args.limit):
            flow.queue_user_response(f"pay-{pm.payment_id}", "approve")
        for q in quotes:
            flow.queue_user_response(f"po-{q.stem}", "approve")

    print(f"[i] Jarvis Core 사이클 시작 — 견적서 {len(quotes)}건, 입금 limit={args.limit or '전체'}, mock={args.mock}", file=sys.stderr)
    report = _asyncio.run(run_jarvis_cycle(db, flow, quotes=quotes, mock=args.mock, payment_limit=args.limit))

    print("\n" + "=" * 60, file=sys.stderr)
    print("[Summary — Jarvis Core (3 시나리오 병렬)]", file=sys.stderr)
    for k, v in report.summary().items():
        print(f"  {k:10s}: {v}", file=sys.stderr)

    if args.out:
        payload = {
            "scenario": "all",
            "summary": report.summary(),
            "rules": [r.to_dict() for r in report.rules],
            "inventory": [r.to_dict() for r in report.inventory],
            "payments": [r.to_dict() for r in report.payments],
            "purchases": [r.to_dict() for r in report.purchases],
        }
        Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[i] 리포트 저장: {args.out}", file=sys.stderr)
    return 0


def main() -> int:
    load_dotenv()
    args = parse_args()
    if args.scenario == "payment":
        return run_payment(args)
    if args.scenario == "purchase":
        return run_purchase(args)
    if args.scenario == "all":
        return run_jarvis(args)
    if args.scenario == "ask":
        return run_ask(args)
    if args.scenario == "investigate":
        return run_investigate(args)
    if args.scenario == "briefing":
        from .jarvis_briefing import run_briefing_cli
        return run_briefing_cli(args, _open_db)
    if args.scenario == "eval":
        from .agent_eval import run_eval_cli
        return run_eval_cli(args, _open_db)
    return run_inventory(args)


if __name__ == "__main__":
    raise SystemExit(main())
