"""공유 도구 레이어 — Jarvis 에이전트들이 ERP(이카운트 더미)를 읽는 단일 창구.

이 모듈 하나가 세 가지 오케스트레이션 패턴 (Q&A 루프 / 자율 조사 / 멀티에이전트 브리핑)
의 공통 기반이다. "한 도구 레이어 위에, 작업에 맞는 오케스트레이션을 골라 얹는다"가 설계 핵심.

정직성 / 안전 설계:
- **전부 read-only (센서)**. 쓰기 도구는 여기 없다 → 조사/질의 에이전트는 구조적으로 행동 불가.
  (돈이 나가는 액션 = 시나리오 1-3 의 decision 레이어 + 사람 승인으로만. agent_tools 와 분리.)
- dispatch 결과는 JSON 직렬화 가능 dict. LLM tool_result 로 그대로 들어감.
- 리스트는 상한 (cap) 두고 truncated 표기 → 토큰 폭증 방지 (cost-economics 노트의 패턴).

실제 운영 전환: 각 함수 본문의 SQLite 조회만 이카운트 OAPI 호출로 바꾸면 됨
(예: list_safety_stock_breaches → InventoryBalance/GetListInventoryBalanceStatusByLocation).
"""

from __future__ import annotations

from typing import Any, Callable

from rapidfuzz import fuzz, process

# ─────────────────────────────────────────────
# 출력 정리 헬퍼
# ─────────────────────────────────────────────

_LIST_CAP = 10  # tool_result 안에 넣는 리스트 최대 길이 (토큰 가드)


def _won(n: int) -> str:
    return f"{n:,}원"


def _cap(rows: list[Any]) -> tuple[list[Any], int]:
    """리스트 상한 적용. (잘린 리스트, 잘려나간 개수) 반환."""
    if len(rows) <= _LIST_CAP:
        return rows, 0
    return rows[:_LIST_CAP], len(rows) - _LIST_CAP


# ─────────────────────────────────────────────
# 도구 구현 — 전부 EcountDBClient(db) 를 인자로 받음
# ─────────────────────────────────────────────


def _search_partner(db, query: str, limit: int = 5) -> dict[str, Any]:
    """거래처명 fuzzy 검색. 입금/미수 조회 전에 partner_id 를 찾는 진입 도구.

    표기 변형 ((주)현대건자재 / 현대건자재 / 현대 건자재) 대응 = rapidfuzz WRatio.
    """
    partners = db.list_partners()
    if not partners:
        return {"query": query, "matches": []}
    names = [p.name for p in partners]
    hits = process.extract(query, names, scorer=fuzz.WRatio, limit=limit)
    matches = []
    for _name, score, idx in hits:
        p = partners[idx]
        matches.append({
            "partner_id": p.partner_id,
            "name": p.name,
            "match_score": round(score, 1),
            "payment_terms": p.payment_terms,
        })
    return {"query": query, "matches": matches}


def _get_partner_receivables(db, partner_id: int) -> dict[str, Any]:
    """특정 거래처의 미수금 (미수 청구서 목록 + 총액)."""
    partner = db.get_partner(partner_id)
    if partner is None:
        return {"error": f"partner_id {partner_id} 없음"}
    invoices = db.list_outstanding_invoices(partner_id)
    total = sum(inv.amount for inv in invoices)
    shown, omitted = _cap(invoices)
    return {
        "partner_id": partner_id,
        "partner_name": partner.name,
        "payment_terms": partner.payment_terms,
        "outstanding_count": len(invoices),
        "outstanding_total": total,
        "outstanding_total_fmt": _won(total),
        "invoices": [
            {"invoice_id": inv.invoice_id, "amount": inv.amount,
             "invoice_date": inv.invoice_date[:10], "due_date": (inv.due_date or "")[:10],
             "status": inv.status}
            for inv in shown
        ],
        "invoices_omitted": omitted,
    }


def _list_safety_stock_breaches(db) -> dict[str, Any]:
    """누적 재고 < 안전재고 인 품목 전체 (개수 + 목록)."""
    breaches = db.list_safety_stock_breaches()
    shown, omitted = _cap(breaches)
    return {
        "breach_count": len(breaches),
        "items": [
            {"sku": it.sku, "name": it.name, "category": it.category,
             "total_stock": it.total_stock, "safety_stock": it.safety_stock,
             "shortage": it.safety_stock - it.total_stock}
            for it in shown
        ],
        "items_omitted": omitted,
    }


def _get_item_detail(db, sku: str) -> dict[str, Any]:
    """품목 1건 상세 — 재고/안전재고/최근 출고/거래처."""
    item = next((it for it in db.list_items() if it.sku == sku), None)
    if item is None:
        return {"error": f"SKU {sku} 없음"}
    return {
        "sku": item.sku,
        "name": item.name,
        "category": item.category,
        "total_stock": item.total_stock,
        "safety_stock": item.safety_stock,
        "below_safety": item.below_safety,
        "past_4w_outflow": item.past_4w_outflow,
        "suppliers": item.suppliers,
    }


def _payment_match_stats(db) -> dict[str, Any]:
    """미매칭 입금 현황 — 개수 + 총액 + 표본."""
    pending = db.list_pending_payments()
    total = sum(p.amount for p in pending)
    shown, omitted = _cap(pending)
    return {
        "unmatched_count": len(pending),
        "unmatched_total": total,
        "unmatched_total_fmt": _won(total),
        "samples": [
            {"payment_id": p.payment_id, "name_as_received": p.partner_name_as_received,
             "amount": p.amount, "received_at": p.received_at[:10]}
            for p in shown
        ],
        "samples_omitted": omitted,
    }


def _top_receivable_partners(db, limit: int = 5) -> dict[str, Any]:
    """미수금이 큰 거래처 Top N (미수 청구서를 거래처별로 집계). 회수 우선순위 판단용."""
    invoices = db.list_outstanding_invoices()  # partner_id=None → 전체 미수
    by_partner: dict[int, dict[str, Any]] = {}
    for inv in invoices:
        slot = by_partner.setdefault(inv.partner_id, {"count": 0, "total": 0})
        slot["count"] += 1
        slot["total"] += inv.amount
    ranked = sorted(by_partner.items(), key=lambda kv: -kv[1]["total"])[:limit]
    rows = []
    for pid, agg in ranked:
        p = db.get_partner(pid)
        rows.append({
            "partner_id": pid,
            "partner_name": p.name if p else f"#{pid}",
            "outstanding_count": agg["count"],
            "outstanding_total": agg["total"],
            "outstanding_total_fmt": _won(agg["total"]),
        })
    return {"partner_count_with_receivables": len(by_partner), "top": rows}


def _find_unmatched_payments_by_partner(db, query: str, limit: int = 8) -> dict[str, Any]:
    """입금자명(표기 변형 포함) 으로 미매칭 입금 fuzzy 검색.

    '현대건자재 입금 들어온 거 있어?' 같은 질의에 사용.
    """
    pending = db.list_pending_payments()
    if not pending:
        return {"query": query, "matches": []}
    names = [p.partner_name_as_received for p in pending]
    hits = process.extract(query, names, scorer=fuzz.WRatio, limit=limit)
    matches = []
    for _name, score, idx in hits:
        if score < 60:  # 너무 낮은 매칭은 버림
            continue
        p = pending[idx]
        matches.append({
            "payment_id": p.payment_id,
            "name_as_received": p.partner_name_as_received,
            "match_score": round(score, 1),
            "amount": p.amount,
            "received_at": p.received_at[:10],
        })
    return {"query": query, "matches": matches}


def _get_daily_briefing(db) -> dict[str, Any]:
    """오늘의 운영 브리핑 — 재고·재무·미수 3개 영역 현황 + '가장 급한 일' 우선순위.

    저장된 과거 이력이 아니라 호출 시점의 실시간 집계.
    ('아침 브리핑', '오늘 현황', '뭐가 제일 급해' 류 질문에 사용.)
    """
    breaches = db.list_safety_stock_breaches()
    pending = db.list_pending_payments()
    pending_total = sum(p.amount for p in pending)
    top = _top_receivable_partners(db, limit=3)

    worst_stock = max(breaches, key=lambda it: it.safety_stock - it.total_stock, default=None)
    biggest_pay = max(pending, key=lambda p: p.amount, default=None)

    priorities: list[dict[str, Any]] = []
    if biggest_pay:
        priorities.append({
            "area": "재무", "action": "미확인 입금 매칭",
            "detail": f"{biggest_pay.partner_name_as_received} {biggest_pay.amount:,}원 (미확인 입금 중 최대 건)",
        })
    if worst_stock:
        short = worst_stock.safety_stock - worst_stock.total_stock
        priorities.append({
            "area": "재고", "action": "안전재고 발주 검토",
            "detail": f"{worst_stock.sku} 현재고 {worst_stock.total_stock}/안전 {worst_stock.safety_stock} (부족 {short})",
        })
    if top.get("top"):
        t = top["top"][0]
        priorities.append({
            "area": "미수", "action": "미수금 회수 점검",
            "detail": f"{t['partner_name']} {t['outstanding_total_fmt']} ({t['outstanding_count']}건, 최다 미수)",
        })
    return {
        "note": "저장된 과거 이력이 아니라 호출 시점의 실시간 집계입니다.",
        "inventory": {"breach_count": len(breaches)},
        "finance": {"unmatched_count": len(pending), "unmatched_total_fmt": _won(pending_total)},
        "receivables": {
            "partner_count_with_receivables": top.get("partner_count_with_receivables", 0),
            "top": top.get("top", []),
        },
        "priorities": priorities,
    }


# ─────────────────────────────────────────────
# dispatch 테이블 + Anthropic tool 스키마
# ─────────────────────────────────────────────

# name → (구현 함수, 기대 인자 키들)
_DISPATCH: dict[str, Callable[..., dict[str, Any]]] = {
    "search_partner": _search_partner,
    "get_partner_receivables": _get_partner_receivables,
    "list_safety_stock_breaches": _list_safety_stock_breaches,
    "get_item_detail": _get_item_detail,
    "payment_match_stats": _payment_match_stats,
    "find_unmatched_payments_by_partner": _find_unmatched_payments_by_partner,
    "top_receivable_partners": _top_receivable_partners,
    "get_daily_briefing": _get_daily_briefing,
}


def dispatch(db, name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    """tool_use 블록 → 실제 도구 실행. 알 수 없는 도구/오류는 dict 로 감싼다 (루프가 안 죽게)."""
    fn = _DISPATCH.get(name)
    if fn is None:
        return {"error": f"알 수 없는 도구: {name}"}
    try:
        return fn(db, **tool_input)
    except TypeError as e:
        return {"error": f"인자 오류 ({name}): {e}"}
    except Exception as e:  # noqa: BLE001 — PoC: 어떤 도구 오류든 LLM 에게 돌려줘 회복 유도
        return {"error": f"{type(e).__name__}: {e}"}


# Anthropic Messages API `tools` 파라미터용 스키마 (read-only 센서 6종)
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "search_partner",
        "description": "거래처명으로 거래처를 fuzzy 검색해 partner_id 를 찾는다. "
                       "표기 변형((주)/공백/주식회사)을 견딘다. 미수금/입금 조회 전 먼저 이걸로 id 확보.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "거래처명 (부분/변형 가능)"}},
            "required": ["query"],
        },
    },
    {
        "name": "get_partner_receivables",
        "description": "특정 거래처(partner_id)의 미수금 청구서 목록과 총액을 조회한다.",
        "input_schema": {
            "type": "object",
            "properties": {"partner_id": {"type": "integer"}},
            "required": ["partner_id"],
        },
    },
    {
        "name": "list_safety_stock_breaches",
        "description": "누적 재고가 안전재고 미만인 모든 품목과 그 개수를 조회한다. 인자 없음.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_item_detail",
        "description": "품목 SKU 1건의 상세(현재고/안전재고/최근4주 출고/거래처)를 조회한다.",
        "input_schema": {
            "type": "object",
            "properties": {"sku": {"type": "string"}},
            "required": ["sku"],
        },
    },
    {
        "name": "payment_match_stats",
        "description": "아직 매칭되지 않은(미수금에 연결 안 된) 입금의 건수/총액/표본을 조회한다. 인자 없음.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "find_unmatched_payments_by_partner",
        "description": "입금자 표기명으로 미매칭 입금을 fuzzy 검색한다. 특정 거래처의 입금 도착 여부 확인용.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "입금자명/거래처명"}},
            "required": ["query"],
        },
    },
    {
        "name": "top_receivable_partners",
        "description": "미수금이 큰 거래처 상위 N곳을 거래처별 집계로 조회한다. 회수 우선순위/리스크 판단용.",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "상위 몇 곳 (기본 5)"}},
        },
    },
    {
        "name": "get_daily_briefing",
        "description": "오늘의 운영 브리핑 — 재고/재무/미수 3개 영역 현황과 '가장 급한 일' 우선순위를 한 번에 집계한다. "
                       "'아침 브리핑', '오늘 현황 정리', '뭐가 제일 급해' 류 질문에 사용. 인자 없음.",
        "input_schema": {"type": "object", "properties": {}},
    },
]
