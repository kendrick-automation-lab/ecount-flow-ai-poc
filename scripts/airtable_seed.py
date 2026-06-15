"""SQLite 더미 → Airtable 시딩 — 테이블 생성(Meta API) + 레코드 push.

목적: Airtable 그리드에 '실데이터'가 보이게 만들어, 에이전트가 실제 Airtable API 로 라이브 조회하는
가시적 데모를 가능케 함. (이카운트/Flow 게이트 우회 — 같은 어댑터 패턴을 실 API 로 증명.)

사용:
  python scripts/db_seed.py --reset            # (선행) SQLite 더미 생성
  .env 에 AIRTABLE_TOKEN / AIRTABLE_BASE_ID 넣고:
  python scripts/airtable_seed.py              # 테이블 생성 + 레코드 push

주의: 재실행 시 레코드 중복 생성됨 (데모용 1회 시딩 가정).
무료 플랜 레코드 한도(베이스당 ~1000) 고려해 데이터 테이블은 CAP 으로 제한.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for stream in (sys.stdout, sys.stderr):
    rc = getattr(stream, "reconfigure", None)
    if callable(rc):
        rc(encoding="utf-8", errors="replace")

import httpx  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from src.airtable_client import AirtableClient  # noqa: E402
from src.db import connect  # noqa: E402

CAP = 250  # 전체 커버 (품목200·매출100·입금100·거래처50) — Airtable 무료 1000 한도 내

NUM = {"type": "number", "options": {"precision": 0}}
TXT = {"type": "singleLineText"}

SCHEMAS = {
    "Partners": [
        {"name": "name", **TXT}, {"name": "partner_id", **NUM}, {"name": "business_no", **TXT},
        {"name": "payment_terms", **TXT}, {"name": "contact", **TXT}, {"name": "created_at", **TXT},
    ],
    "Items": [
        {"name": "sku", **TXT}, {"name": "name", **TXT}, {"name": "category", **TXT},
        {"name": "unit_price", **NUM}, {"name": "safety_stock", **NUM}, {"name": "current_stock", **NUM},
    ],
    "Sales": [
        {"name": "ref", **TXT}, {"name": "invoice_id", **NUM}, {"name": "partner_id", **NUM},
        {"name": "amount", **NUM}, {"name": "invoice_date", **TXT}, {"name": "due_date", **TXT},
        {"name": "status", **TXT},
    ],
    "Payments": [
        {"name": "ref", **TXT}, {"name": "payment_id", **NUM}, {"name": "partner_name_as_received", **TXT},
        {"name": "amount", **NUM}, {"name": "received_at", **TXT}, {"name": "match_status", **TXT},
    ],
}


def _rows_from_sqlite() -> dict[str, list[dict]]:
    conn = connect()  # row_factory = sqlite3.Row (src.db.connect 가 설정)
    cur = conn.cursor()

    partners = [dict(r) for r in cur.execute("SELECT * FROM partners ORDER BY partner_id")]
    items = [dict(r) for r in cur.execute(
        "SELECT i.sku, i.name, i.category, i.unit_price, i.safety_stock, "
        "COALESCE((SELECT SUM(CASE movement_type WHEN 'in' THEN qty ELSE -qty END) "
        "          FROM inventory_movements m WHERE m.sku=i.sku),0) AS current_stock "
        f"FROM items i LIMIT {CAP}")]
    sales = [dict(r) for r in cur.execute(f"SELECT * FROM sales ORDER BY invoice_id LIMIT {CAP}")]
    payments = [dict(r) for r in cur.execute(f"SELECT * FROM payments ORDER BY payment_id LIMIT {CAP}")]
    conn.close()

    return {
        "Partners": [{"name": p["name"], "partner_id": p["partner_id"], "business_no": p["business_no"],
                      "payment_terms": p["payment_terms"], "contact": p["contact"],
                      "created_at": (p["created_at"] or "")[:10]} for p in partners],
        "Items": [{"sku": it["sku"], "name": it["name"], "category": it["category"],
                   "unit_price": it["unit_price"], "safety_stock": it["safety_stock"],
                   "current_stock": it["current_stock"]} for it in items],
        "Sales": [{"ref": f"INV-{s['invoice_id']}", "invoice_id": s["invoice_id"], "partner_id": s["partner_id"],
                   "amount": s["amount"], "invoice_date": (s["invoice_date"] or "")[:10],
                   "due_date": (s["due_date"] or "")[:10], "status": s["status"]} for s in sales],
        "Payments": [{"ref": f"PAY-{p['payment_id']}", "payment_id": p["payment_id"],
                      "partner_name_as_received": p["partner_name_as_received"], "amount": p["amount"],
                      "received_at": (p["received_at"] or "")[:10], "match_status": p["match_status"]}
                     for p in payments],
    }


def _ensure_tables(client: AirtableClient, existing: set[str]) -> None:
    url = f"https://api.airtable.com/v0/meta/bases/{client.base_id}/tables"
    headers = {**client._headers(), "Content-Type": "application/json"}
    for name, fields in SCHEMAS.items():
        if name in existing:
            print(f"[i] '{name}' 이미 존재 — 생성 건너뜀", file=sys.stderr)
            continue
        r = httpx.post(url, headers=headers, json={"name": name, "fields": fields}, timeout=30.0)
        if r.status_code >= 300:
            print(f"[!] '{name}' 생성 실패 {r.status_code}: {r.text[:300]}", file=sys.stderr)
        else:
            print(f"[+] '{name}' 테이블 생성", file=sys.stderr)


def _push(client: AirtableClient, table: str, rows: list[dict]) -> int:
    url = f"https://api.airtable.com/v0/{client.base_id}/{table}"
    headers = {**client._headers(), "Content-Type": "application/json"}
    pushed = 0
    for i in range(0, len(rows), 10):  # Airtable: 최대 10 레코드/요청
        chunk = [{"fields": row} for row in rows[i:i + 10]]
        r = httpx.post(url, headers=headers, json={"records": chunk, "typecast": True}, timeout=30.0)
        if r.status_code >= 300:
            print(f"[!] '{table}' push 실패 {r.status_code}: {r.text[:300]}", file=sys.stderr)
            break
        pushed += len(chunk)
        time.sleep(0.25)  # rate limit 여유 (Airtable 5 req/s)
    return pushed


def _clear_table(client: AirtableClient, table: str) -> int:
    """테이블 기존 레코드 전체 삭제 (재시딩 시 중복 방지)."""
    headers = client._headers()
    ids = [r["_id"] for r in client._records(table) if r.get("_id")]
    url = f"https://api.airtable.com/v0/{client.base_id}/{table}"
    for i in range(0, len(ids), 10):
        params = [("records[]", rid) for rid in ids[i:i + 10]]
        httpx.delete(url, headers=headers, params=params, timeout=30.0)
        time.sleep(0.25)
    return len(ids)


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="더미 → Airtable 시딩")
    ap.add_argument("--reset", action="store_true", help="우리 테이블 기존 레코드 삭제 후 재시딩")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    try:
        client = AirtableClient.from_env()
    except RuntimeError as e:
        print(f"[!] {e}", file=sys.stderr)
        return 2

    print("[i] SQLite 더미 읽는 중...", file=sys.stderr)
    data = _rows_from_sqlite()
    for k, v in data.items():
        print(f"    {k}: {len(v)} rows", file=sys.stderr)

    print("[i] 테이블 확인/생성...", file=sys.stderr)
    existing = {t.get("name") for t in client.list_tables()}
    if args.reset:
        for table in data:
            if table in existing:
                n = _clear_table(client, table)
                print(f"    {table}: 기존 {n}건 삭제", file=sys.stderr)
    _ensure_tables(client, existing)

    print("[i] 레코드 push...", file=sys.stderr)
    total = 0
    for table, rows in data.items():
        n = _push(client, table, rows)
        total += n
        print(f"    {table}: {n} pushed", file=sys.stderr)

    print(f"[+] 완료 — 총 {total} 레코드. Airtable 그리드에서 확인 후, 에이전트를 Airtable 백엔드로 실행.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
