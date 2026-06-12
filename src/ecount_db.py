"""Ecount SQLite Backend — 시나리오 1-3 통합 클라이언트.

기존 `ecount.EcountClient` (JSON backend, 시나리오 1 Day 1) 와 동일 인터페이스
+ 시나리오 2 (입금매칭) / 시나리오 3 (구매입력) 신규 endpoint.

Strategy 패턴: orchestrator 는 둘 다 받을 수 있도록 duck typing.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .db import DEFAULT_DB_PATH, connect, rows_to_list
from .ecount import Item


@dataclass
class Partner:
    partner_id: int
    name: str
    business_no: str
    payment_terms: str
    contact: str
    created_at: str = ""


@dataclass
class Invoice:
    invoice_id: int
    partner_id: int
    amount: int
    invoice_date: str
    due_date: str
    status: str

    @property
    def is_outstanding(self) -> bool:
        return self.status in ("pending", "partial")


@dataclass
class Payment:
    payment_id: int
    partner_name_as_received: str
    amount: int
    received_at: str
    matched_invoice_ids: list[int]
    match_status: str


@dataclass
class EcountDBClient:
    """SQLite 백엔드. orchestrator 와 duck typing 호환 (`list_safety_stock_breaches`, `save_purchase_order` 동일 시그니처)."""

    db_path: Path | str | None = None
    _purchase_orders: list[dict] = field(default_factory=list)
    _purchase_entries: list[dict] = field(default_factory=list)
    _payment_matches: list[dict] = field(default_factory=list)

    @classmethod
    def from_db(cls, db_path: Path | str | None = None) -> "EcountDBClient":
        return cls(db_path=db_path)

    def _conn(self) -> sqlite3.Connection:
        return connect(self.db_path or DEFAULT_DB_PATH)

    # ─────────────────────────────────────────────
    # 공통 — partners / items
    # ─────────────────────────────────────────────

    def list_partners(self) -> list[Partner]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM partners ORDER BY partner_id").fetchall()
        return [Partner(**dict(r)) for r in rows]

    def get_partner(self, partner_id: int) -> Partner | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM partners WHERE partner_id = ?", (partner_id,)).fetchone()
        return Partner(**dict(row)) if row else None

    # ─────────────────────────────────────────────
    # 시나리오 1 — 안전재고 자동 발주 (기존 EcountClient 와 호환)
    # ─────────────────────────────────────────────

    def list_items(self) -> list[Item]:
        """모든 품목 + 누적 재고 + 거래처 정보 반환 (Item 인터페이스 호환)."""
        return self._build_items(where_clause=None)

    def list_safety_stock_breaches(self) -> list[Item]:
        """누적 재고 < 안전재고 품목만."""
        return [it for it in self._build_items(where_clause=None) if it.below_safety]

    def _build_items(self, where_clause: str | None = None) -> list[Item]:
        """SQL 로 품목 + 창고별 재고 + 출고 이력 + 거래처 묶어서 Item 만듦.

        이 함수가 시나리오 1 호환의 핵심.
        """
        sql_items = "SELECT * FROM items"
        if where_clause:
            sql_items += " WHERE " + where_clause
        out: list[Item] = []
        with self._conn() as conn:
            items_rows = conn.execute(sql_items).fetchall()
            for ir in items_rows:
                sku = ir["sku"]
                # 창고별 누적 재고
                stocks_rows = conn.execute(
                    """
                    SELECT warehouse_code,
                           SUM(CASE movement_type WHEN 'in' THEN qty ELSE -qty END) AS qty
                    FROM inventory_movements
                    WHERE sku = ?
                    GROUP BY warehouse_code
                    """,
                    (sku,),
                ).fetchall()
                stocks = [{"warehouse": s["warehouse_code"], "qty": s["qty"] or 0} for s in stocks_rows]

                # 최근 4주 출고 (각 주 합산)
                outflow_rows = conn.execute(
                    """
                    SELECT strftime('%Y-%W', ts) AS yw, SUM(qty) AS out_qty
                    FROM inventory_movements
                    WHERE sku = ? AND movement_type = 'out'
                    GROUP BY yw
                    ORDER BY yw DESC LIMIT 4
                    """,
                    (sku,),
                ).fetchall()
                past_4w_outflow = [r["out_qty"] for r in outflow_rows][::-1]

                # 거래처 정보 (default + 카테고리 동일 거래처들)
                suppliers: list[dict[str, Any]] = []
                if ir["default_supplier_id"]:
                    p = conn.execute(
                        "SELECT * FROM partners WHERE partner_id = ?", (ir["default_supplier_id"],)
                    ).fetchone()
                    if p:
                        suppliers.append({
                            "name": p["name"],
                            "last_price": ir["unit_price"],
                            "avg_lead_days": ir["lead_days_avg"] or 5,
                            "price_history_30d": [
                                int(ir["unit_price"] * 0.95),
                                ir["unit_price"],
                                int(ir["unit_price"] * 1.02),
                            ],
                        })

                # 최근 30일 발주 횟수 (DB 에 발주 테이블 없으니 0 — production 에선 이카운트 OAPI)
                recent_orders_30d = 0

                out.append(
                    Item(
                        sku=sku,
                        name=ir["name"],
                        category=ir["category"],
                        safety_stock=ir["safety_stock"],
                        stocks=stocks,
                        past_4w_outflow=past_4w_outflow,
                        suppliers=suppliers,
                        recent_orders_30d=recent_orders_30d,
                    )
                )
        return out

    def save_purchase_order(
        self,
        sku: str,
        qty: int,
        supplier: str,
        est_price: int,
        note: str = "",
    ) -> dict[str, Any]:
        """발주 등록 (in-memory + 재고 movement 추가)."""
        order_id = f"PO-{len(self._purchase_orders) + 1:05d}"
        record = {
            "order_id": order_id,
            "sku": sku,
            "qty": qty,
            "supplier": supplier,
            "est_price": est_price,
            "total": qty * est_price,
            "note": note,
            "status": "REGISTERED",
        }
        self._purchase_orders.append(record)
        # 실제 이카운트라면 InventoryMovement 도 등록되지만, PoC 는 발주 = 입고 예정 표기만
        return record

    @property
    def purchase_orders(self) -> list[dict]:
        return list(self._purchase_orders)

    # ─────────────────────────────────────────────
    # 시나리오 2 — 입금 매칭 (신규)
    # ─────────────────────────────────────────────

    def list_outstanding_invoices(self, partner_id: int | None = None) -> list[Invoice]:
        """미수금 청구서 (status != 'paid')."""
        sql = "SELECT * FROM sales WHERE status IN ('pending', 'partial')"
        params: tuple[Any, ...] = ()
        if partner_id is not None:
            sql += " AND partner_id = ?"
            params = (partner_id,)
        sql += " ORDER BY invoice_date"
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [Invoice(**dict(r)) for r in rows]

    def list_pending_payments(self, limit: int | None = None) -> list[Payment]:
        """매칭 미완료 입금 (status = 'unmatched')."""
        sql = "SELECT * FROM payments WHERE match_status = 'unmatched' ORDER BY received_at"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self._conn() as conn:
            rows = conn.execute(sql).fetchall()
        out: list[Payment] = []
        for r in rows:
            d = dict(r)
            ids = json.loads(d["matched_invoice_ids"]) if d["matched_invoice_ids"] else []
            d["matched_invoice_ids"] = ids
            out.append(Payment(**d))
        return out

    def save_payment_match(
        self,
        payment_id: int,
        invoice_ids: list[int],
        status: str,
        note: str = "",
    ) -> dict[str, Any]:
        """입금 매칭 결과 저장 + sales status 업데이트.

        status: 'matched' (자동 / 100%) / 'partial' (일부) / 'manual' (사람 확정).
        """
        assert status in ("matched", "partial", "manual"), f"unknown status: {status}"
        with self._conn() as conn:
            conn.execute(
                "UPDATE payments SET matched_invoice_ids = ?, match_status = ? WHERE payment_id = ?",
                (json.dumps(invoice_ids), status, payment_id),
            )
            # 매칭된 청구서 status 업데이트
            for inv_id in invoice_ids:
                if status == "matched":
                    conn.execute("UPDATE sales SET status = 'paid' WHERE invoice_id = ?", (inv_id,))
                elif status == "partial":
                    conn.execute("UPDATE sales SET status = 'partial' WHERE invoice_id = ?", (inv_id,))
            conn.commit()
        record = {
            "payment_id": payment_id,
            "invoice_ids": invoice_ids,
            "status": status,
            "note": note,
        }
        self._payment_matches.append(record)
        return record

    @property
    def payment_matches(self) -> list[dict]:
        return list(self._payment_matches)

    # ─────────────────────────────────────────────
    # 시나리오 3 — 구매 입력 (신규)
    # ─────────────────────────────────────────────

    def list_item_catalog(self) -> list[dict]:
        """전체 품목 카탈로그 (sku/name/unit_price 만 — 가볍게). SKU fuzzy 매칭용."""
        with self._conn() as conn:
            rows = conn.execute("SELECT sku, name, unit_price FROM items").fetchall()
        return rows_to_list(rows)

    def list_items_by_sku_prefix(self, prefix: str | None = None, limit: int = 50) -> list[dict]:
        """SKU prefix 로 품목 검색. 구매전표 작성 시 SKU 매칭에 사용."""
        sql = "SELECT sku, name, category, unit_price FROM items"
        params: tuple[Any, ...] = ()
        if prefix:
            sql += " WHERE sku LIKE ?"
            params = (f"{prefix}%",)
        sql += f" LIMIT {int(limit)}"
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return rows_to_list(rows)

    def save_purchase_entry(
        self,
        partner_id: int,
        quote_lines: list[dict[str, Any]],
        quote_ref: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        """구매전표 등록 (in-memory).

        quote_lines: [{"sku": ..., "qty": ..., "unit_price": ...}, ...]
        """
        entry_id = f"PE-{len(self._purchase_entries) + 1:05d}"
        total = sum(line["qty"] * line["unit_price"] for line in quote_lines)
        record = {
            "entry_id": entry_id,
            "partner_id": partner_id,
            "quote_ref": quote_ref,
            "lines": quote_lines,
            "total": total,
            "note": note,
            "status": "REGISTERED",
        }
        self._purchase_entries.append(record)
        return record

    @property
    def purchase_entries(self) -> list[dict]:
        return list(self._purchase_entries)
