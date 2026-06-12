"""더미 이카운트 ERP 클라이언트.

실제 이카운트 OAPI 의 인증 흐름 (Zone → Login → SESSION_ID → 호출) 을 모방하되,
PoC 단계에서는 메모리 안 더미 데이터로 동작.

실제 운영 시 `_call` 만 `requests.post(...)` 로 바꾸면 됨.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Item:
    sku: str
    name: str
    category: str
    safety_stock: int
    stocks: list[dict]
    past_4w_outflow: list[int]
    suppliers: list[dict]
    recent_orders_30d: int

    @property
    def total_stock(self) -> int:
        return sum(s["qty"] for s in self.stocks)

    @property
    def below_safety(self) -> bool:
        return self.total_stock < self.safety_stock


@dataclass
class EcountClient:
    """더미 이카운트 클라이언트.

    실제 endpoint 매핑 (PoC 코드에 주석으로 보존):
    - Zone:           POST /OAPI/V2/Zone
    - Login:          POST /OAPI/V2/OAPILogin
    - 재고 조회:        POST /OAPI/V2/InventoryBalance/GetListInventoryBalanceStatusByLocation
    - 발주 등록:        POST /OAPI/V2/PurchasesOrder/SaveOrder
    """

    inventory_path: Path
    session_id: str = "mock-session-12345"
    _purchase_orders: list[dict] = field(default_factory=list)

    @classmethod
    def from_sample(cls, sample_path: str | Path) -> "EcountClient":
        return cls(inventory_path=Path(sample_path))

    def _load(self) -> dict:
        return json.loads(self.inventory_path.read_text(encoding="utf-8"))

    def list_items(self) -> list[Item]:
        data = self._load()
        return [Item(**it) for it in data["items"]]

    def list_safety_stock_breaches(self) -> list[Item]:
        """안전재고 미만 품목 조회.

        실제 운영: InventoryBalance/GetListInventoryBalanceStatusByLocation 후 클라이언트측 필터.
        (이카운트가 안전재고 미만 필터 endpoint 직접 제공한다면 그걸 사용)
        """
        return [it for it in self.list_items() if it.below_safety]

    def save_purchase_order(
        self,
        sku: str,
        qty: int,
        supplier: str,
        est_price: int,
        note: str = "",
    ) -> dict[str, Any]:
        """발주 등록 (더미).

        실제 endpoint: POST /OAPI/V2/PurchasesOrder/SaveOrder
        """
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
        return record

    @property
    def purchase_orders(self) -> list[dict]:
        return list(self._purchase_orders)
