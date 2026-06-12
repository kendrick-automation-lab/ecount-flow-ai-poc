"""SQLite 백엔드 — 사내 자동화 허브 (코드명 Jarvis) 더미 환경.

스키마 5 테이블:
  partners                — 거래처 50
  items                   — 품목 200
  inventory_movements     — 재고 입출고 히스토리 1000
  sales                   — 매출 청구서 100
  payments                — 입금 100 (일부 unmatched / partial / 표기 변형)

실제 이카운트 OAPI 의 응답 패턴 (`Data.Result` 배열) 을 모방하기 위해
조회 endpoint 는 dict list 를 반환하도록 통일.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS partners (
    partner_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    business_no    TEXT,
    payment_terms  TEXT,
    contact        TEXT,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    sku                  TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    category             TEXT,
    unit_price           INTEGER NOT NULL,
    safety_stock         INTEGER NOT NULL,
    default_supplier_id  INTEGER REFERENCES partners(partner_id),
    lead_days_avg        INTEGER,
    created_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inventory_movements (
    movement_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    sku             TEXT NOT NULL REFERENCES items(sku),
    warehouse_code  TEXT NOT NULL,
    movement_type   TEXT NOT NULL CHECK (movement_type IN ('in', 'out')),
    qty             INTEGER NOT NULL,
    ts              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sales (
    invoice_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    partner_id     INTEGER NOT NULL REFERENCES partners(partner_id),
    amount         INTEGER NOT NULL,
    invoice_date   TEXT NOT NULL,
    due_date       TEXT,
    status         TEXT NOT NULL CHECK (status IN ('pending', 'paid', 'partial'))
);

CREATE TABLE IF NOT EXISTS payments (
    payment_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    partner_name_as_received TEXT NOT NULL,
    amount                  INTEGER NOT NULL,
    received_at             TEXT NOT NULL,
    matched_invoice_ids     TEXT,
    match_status            TEXT NOT NULL CHECK (match_status IN ('unmatched', 'matched', 'partial', 'manual'))
);

CREATE INDEX IF NOT EXISTS idx_movements_sku        ON inventory_movements(sku);
CREATE INDEX IF NOT EXISTS idx_movements_ts         ON inventory_movements(ts);
CREATE INDEX IF NOT EXISTS idx_sales_partner        ON sales(partner_id);
CREATE INDEX IF NOT EXISTS idx_sales_status         ON sales(status);
CREATE INDEX IF NOT EXISTS idx_payments_status      ON payments(match_status);
"""


DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "ks_jarvis_demo.db"


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """SQLite 연결 + dict 형태 row + 외래키 활성화."""
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def connection(db_path: Path | str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)


def row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def rows_to_list(rows) -> list[dict]:
    return [row_to_dict(r) for r in rows]
