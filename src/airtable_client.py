"""Airtable 실연동 어댑터 — Strategy 패턴으로 EcountDBClient 자리 교체 (drop-in).

왜 Airtable: 이카운트(사업자 게이트)·Flow(유료 플랜 게이트)가 개인 PoC 로 막혀,
'실제 외부 API 에 인증해서 실데이터를 라이브 조회' 하는 능력을 **게이트 없이 가시적으로** 시연.
★ 정직: 이건 이카운트가 아니라 Airtable. "같은 어댑터 패턴을 실 API 로 증명" 이라고만 표기.

Airtable REST API:
  base URL : https://api.airtable.com/v0/{BASE_ID}/{TABLE}
  auth     : Authorization: Bearer {PAT}
  list     : GET  ...?pageSize=100[&offset=...]  → {"records":[{"id","fields":{...}}], "offset"}
  meta     : GET  https://api.airtable.com/v0/meta/bases/{BASE_ID}/tables

env: AIRTABLE_TOKEN / AIRTABLE_BASE_ID

EcountDBClient 와 동일한 read 메서드 제공 → agent_tools / 에이전트가 그대로 동작.
SQLite 의 inventory_movements 대신 Items 테이블에 current_stock 을 비정규화 저장.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

from .ecount import Item
from .ecount_db import Invoice, Partner, Payment

# Airtable 테이블명 (seed 와 일치해야 함)
T_PARTNERS = "Partners"
T_ITEMS = "Items"
T_SALES = "Sales"
T_PAYMENTS = "Payments"


@dataclass
class AirtableClient:
    token: str
    base_id: str
    timeout: float = 30.0

    @classmethod
    def from_env(cls) -> "AirtableClient":
        tok = os.getenv("AIRTABLE_TOKEN", "").strip()
        base = os.getenv("AIRTABLE_BASE_ID", "").strip()
        miss = [n for n, v in (("AIRTABLE_TOKEN", tok), ("AIRTABLE_BASE_ID", base)) if not v]
        if miss:
            raise RuntimeError(f".env 에 {', '.join(miss)} 필요 (Airtable PAT + base ID).")
        return cls(token=tok, base_id=base)

    # ── 저수준 ──
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def list_tables(self) -> list[dict[str, Any]]:
        """Meta API — base 의 테이블 목록 (연결 확인용)."""
        url = f"https://api.airtable.com/v0/meta/bases/{self.base_id}/tables"
        r = httpx.get(url, headers=self._headers(), timeout=self.timeout)
        r.raise_for_status()
        return r.json().get("tables", [])

    def _records(self, table: str) -> list[dict[str, Any]]:
        """테이블 전체 레코드 → [{**fields, _id}] (페이지네이션 처리)."""
        url = f"https://api.airtable.com/v0/{self.base_id}/{table}"
        out: list[dict[str, Any]] = []
        offset: str | None = None
        while True:
            params = {"pageSize": 100}
            if offset:
                params["offset"] = offset
            r = httpx.get(url, headers=self._headers(), params=params, timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
            for rec in data.get("records", []):
                row = dict(rec.get("fields", {}))
                row["_id"] = rec.get("id")
                out.append(row)
            offset = data.get("offset")
            if not offset:
                break
        return out

    @staticmethod
    def _int(v: Any, default: int = 0) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    # ── 공통 (partners) ──
    def list_partners(self) -> list[Partner]:
        rows = self._records(T_PARTNERS)
        return [
            Partner(
                partner_id=self._int(r.get("partner_id")),
                name=r.get("name", ""),
                business_no=r.get("business_no", ""),
                payment_terms=r.get("payment_terms", ""),
                contact=r.get("contact", ""),
                created_at=r.get("created_at", ""),
            )
            for r in rows
        ]

    def get_partner(self, partner_id: int) -> Partner | None:
        return next((p for p in self.list_partners() if p.partner_id == partner_id), None)

    # ── 시나리오 1 (items) — current_stock 비정규화 ──
    def list_items(self) -> list[Item]:
        rows = self._records(T_ITEMS)
        items: list[Item] = []
        for r in rows:
            cur = self._int(r.get("current_stock"))
            items.append(
                Item(
                    sku=r.get("sku", ""),
                    name=r.get("name", ""),
                    category=r.get("category", ""),
                    safety_stock=self._int(r.get("safety_stock")),
                    stocks=[{"warehouse": "AIRTABLE", "qty": cur}],
                    past_4w_outflow=[],
                    suppliers=[],
                    recent_orders_30d=0,
                )
            )
        return items

    def list_safety_stock_breaches(self) -> list[Item]:
        return [it for it in self.list_items() if it.below_safety]

    def list_item_catalog(self) -> list[dict]:
        return [{"sku": r.get("sku"), "name": r.get("name"), "unit_price": self._int(r.get("unit_price"))}
                for r in self._records(T_ITEMS)]

    # ── 시나리오 2 (sales / payments) ──
    def list_outstanding_invoices(self, partner_id: int | None = None) -> list[Invoice]:
        rows = self._records(T_SALES)
        out: list[Invoice] = []
        for r in rows:
            if r.get("status") not in ("pending", "partial"):
                continue
            pid = self._int(r.get("partner_id"))
            if partner_id is not None and pid != partner_id:
                continue
            out.append(Invoice(
                invoice_id=self._int(r.get("invoice_id")),
                partner_id=pid,
                amount=self._int(r.get("amount")),
                invoice_date=r.get("invoice_date", ""),
                due_date=r.get("due_date", ""),
                status=r.get("status", "pending"),
            ))
        return out

    def list_pending_payments(self, limit: int | None = None) -> list[Payment]:
        rows = [r for r in self._records(T_PAYMENTS) if r.get("match_status", "unmatched") == "unmatched"]
        if limit:
            rows = rows[: int(limit)]
        return [
            Payment(
                payment_id=self._int(r.get("payment_id")),
                partner_name_as_received=r.get("partner_name_as_received", ""),
                amount=self._int(r.get("amount")),
                received_at=r.get("received_at", ""),
                matched_invoice_ids=[],
                match_status=r.get("match_status", "unmatched"),
            )
            for r in rows
        ]

    # ── 쓰기: Airtable 데모는 read-only (에이전트 레이어가 읽기 전용이라 미구현) ──
    def save_purchase_order(self, *a, **k):  # noqa: D401
        raise NotImplementedError("Airtable 데모는 read-only — 발주 쓰기 미지원")

    def save_payment_match(self, *a, **k):
        raise NotImplementedError("Airtable 데모는 read-only — 매칭 쓰기 미지원")

    def save_purchase_entry(self, *a, **k):
        raise NotImplementedError("Airtable 데모는 read-only — 전표 쓰기 미지원")
