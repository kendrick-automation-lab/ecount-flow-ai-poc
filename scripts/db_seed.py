"""SQLite 더미 데이터 seed — 건축자재 중소기업 가설 환경.

규모: 거래처 50 / 품목 200 / 재고 movement 1000 / 매출 100 / 입금 100.

의도적 패턴 (시나리오 검증용):
- 거래처명 표기 변형 다수 ((주)현대건자재 / 현대건자재 / 현대 건자재) → fuzzy match 테스트
- 일부 SKU 를 안전재고 미만으로 → 시나리오 1 검증
- 일부 payment 단일 매칭 / 분할 / 부분 / unmatched 섞기 → 시나리오 2 검증
- 단가 변동 (price_history) 일부 → 시나리오 1 의 PRICE_VOLATILE flag 검증

사용:
  python -m scripts.db_seed                              # 기본 DB 경로
  python -m scripts.db_seed --db data/custom.db          # 커스텀 경로
  python -m scripts.db_seed --reset                      # 기존 DB 비우고 새로
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

# 직접 실행 시 (python scripts/db_seed.py) sys.path 보정
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.db import DEFAULT_DB_PATH, connection, init_schema  # noqa: E402

try:
    from faker import Faker  # type: ignore
except ImportError:  # 의존성 설치 전 대비 안내
    print("[!] faker 가 필요합니다. 'pip install -r requirements.txt' 실행 후 다시 시도.", file=sys.stderr)
    raise

fake = Faker("ko_KR")
random.seed(42)
Faker.seed(42)

# 도메인 — 건축자재 카테고리 + 단가 범위
CATEGORIES = {
    "벽재": (8_000, 25_000),
    "바닥재": (30_000, 60_000),
    "장식재": (15_000, 45_000),
    "도장재": (60_000, 120_000),
    "단열재": (20_000, 80_000),
    "타일": (12_000, 35_000),
    "마감재": (10_000, 30_000),
}

WAREHOUSES = ["WH-JEJU-01", "WH-JEJU-02", "WH-SEOUL-01"]

PAYMENT_TERMS = ["선결제", "NET 15", "NET 30", "NET 45", "월말 정산"]


def _gen_partner_name() -> str:
    """건축자재 도메인 거래처명. 일부러 표기 변형 다양화."""
    base_names = [
        "현대건자재", "한솔산업", "동신건설", "남도자재", "제주건축",
        "삼성건자재", "대림산업", "동양건설", "한양철강", "서해목재",
        "한라건자재", "백두자재", "송산건설", "보광산업", "태양자재",
    ]
    base = random.choice(base_names) + str(random.randint(1, 20))
    # 표기 변형 (fuzzy match 테스트용)
    style = random.choice(["paren", "plain", "spaced", "prefix"])
    if style == "paren":
        return f"(주){base}"
    if style == "plain":
        return base
    if style == "spaced":
        # 중간 공백 추가 "현대 건자재5"
        if len(base) > 4:
            return base[:2] + " " + base[2:]
        return base
    # prefix
    return f"주식회사 {base}"


def seed_partners(conn: sqlite3.Connection, n: int = 50) -> list[int]:
    """거래처 seed — 표기 변형 4 스타일 (paren/plain/spaced/prefix) 분산."""
    ids: list[int] = []
    for _ in range(n):
        name = _gen_partner_name()
        business_no = f"{random.randint(100, 999)}-{random.randint(10, 99)}-{random.randint(10000, 99999)}"
        terms = random.choice(PAYMENT_TERMS)
        contact = f"010-{random.randint(1000, 9999)}-{random.randint(1000, 9999)}"
        created = (datetime(2025, 1, 1) + timedelta(days=random.randint(0, 365))).isoformat()
        cur = conn.execute(
            "INSERT INTO partners (name, business_no, payment_terms, contact, created_at) VALUES (?, ?, ?, ?, ?)",
            (name, business_no, terms, contact, created),
        )
        ids.append(cur.lastrowid)
    return ids


def seed_items(conn, partner_ids: list[int], n: int = 200) -> list[str]:
    skus: list[str] = []
    for i in range(n):
        cat = random.choice(list(CATEGORIES.keys()))
        cat_code = {"벽재": "WALL", "바닥재": "FLOOR", "장식재": "DECO", "도장재": "PAINT",
                    "단열재": "INSU", "타일": "TILE", "마감재": "FINISH"}[cat]
        sku = f"KS-{cat_code}-{i + 100:04d}"
        name = f"{cat} {fake.color_name()} {random.choice(['A', 'B', 'C', 'D'])}{random.randint(100, 999)}"
        low, high = CATEGORIES[cat]
        price = random.randint(low, high)
        safety = random.choice([10, 15, 20, 25, 30, 40, 50])
        # default supplier (시나리오 1 의 추천 거래처 후보)
        supplier_id = random.choice(partner_ids) if partner_ids else None
        # 리드타임 — 카테고리 따라 분포 (도장재 / 단열재 = 길음)
        lead_days = random.choice([3, 5, 7]) if cat in ("벽재", "타일", "마감재") else random.choice([5, 7, 10, 14])
        created = (datetime(2025, 1, 1) + timedelta(days=random.randint(0, 365))).isoformat()
        conn.execute(
            "INSERT INTO items (sku, name, category, unit_price, safety_stock, default_supplier_id, lead_days_avg, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (sku, name, cat, price, safety, supplier_id, lead_days, created),
        )
        skus.append(sku)
    return skus


def seed_movements(conn: sqlite3.Connection, skus: list[str], n: int = 1000) -> None:
    """일부 SKU 는 의도적으로 안전재고 미만으로 만든다 (시나리오 1 검증)."""
    base_date = datetime(2026, 1, 1)
    breach_skus = set(random.sample(skus, k=min(15, len(skus))))  # 15개 SKU 의도적 breach

    # 기수 재고 (opening stock) — 이게 없으면 movement 1000건 / 200 SKU = SKU 당 ~5건이라
    # 거의 모든 SKU 가 안전재고 미만이 되는 비현실 분포가 됨 (2026-06-12 점검에서 발견).
    # breach 대상은 안전재고 부근에서 시작 → out 가중치(80%)로 자연스럽게 미만 진입.
    for r in conn.execute("SELECT sku, safety_stock FROM items").fetchall():
        if r["sku"] in breach_skus:
            qty0 = max(int(r["safety_stock"] * random.uniform(0.8, 1.2)), 1)
        else:
            qty0 = int(r["safety_stock"] * random.uniform(2.0, 4.0))
        conn.execute(
            "INSERT INTO inventory_movements (sku, warehouse_code, movement_type, qty, ts) "
            "VALUES (?, ?, 'in', ?, ?)",
            (r["sku"], random.choice(WAREHOUSES), qty0, base_date.isoformat()),
        )

    for _ in range(n):
        sku = random.choice(skus)
        warehouse = random.choice(WAREHOUSES)
        # breach 대상 SKU 는 out 이 많고 in 적음
        if sku in breach_skus:
            mtype = random.choices(["in", "out"], weights=[20, 80])[0]
        else:
            mtype = random.choices(["in", "out"], weights=[55, 45])[0]
        qty = random.randint(1, 30)
        ts = (base_date + timedelta(days=random.randint(0, 150), hours=random.randint(0, 23))).isoformat()
        conn.execute(
            "INSERT INTO inventory_movements (sku, warehouse_code, movement_type, qty, ts) VALUES (?, ?, ?, ?, ?)",
            (sku, warehouse, mtype, qty, ts),
        )


def seed_sales(conn: sqlite3.Connection, partner_ids: list[int], n: int = 100) -> list[tuple[int, int, int, str]]:
    """반환: (invoice_id, partner_id, amount, status) — 입금 매칭 케이스 만들 때 사용."""
    out: list[tuple[int, int, int, str]] = []
    base = datetime(2026, 1, 1)
    for _ in range(n):
        partner_id = random.choice(partner_ids)
        amount = random.randint(100_000, 5_000_000)
        invoice_date = base + timedelta(days=random.randint(0, 150))
        due = invoice_date + timedelta(days=random.choice([15, 30, 45]))
        status = random.choices(["pending", "paid", "partial"], weights=[60, 30, 10])[0]
        cur = conn.execute(
            "INSERT INTO sales (partner_id, amount, invoice_date, due_date, status) VALUES (?, ?, ?, ?, ?)",
            (partner_id, amount, invoice_date.isoformat(), due.isoformat(), status),
        )
        out.append((cur.lastrowid, partner_id, amount, status))
    return out


def seed_payments(
    conn: sqlite3.Connection,
    partner_ids: list[int],
    sales: list[tuple[int, int, int, str]],
    n: int = 100,
) -> None:
    """매칭 케이스 분포:
       - 30% 단일 청구서 정확 매칭 (자동 가능)
       - 20% 거래처명 표기 변형 (fuzzy 필요)
       - 15% 분할 입금 (큰 금액 = 청구서 2-3개 조합)
       - 15% 부분 입금 (청구서 일부만)
       - 20% 거래처 매칭 안 됨 (unmatched, 사람 검토)
    """
    base = datetime(2026, 2, 1)

    # 'paid' 청구서는 매칭 케이스 생성에서 제외 — 안 그러면 이미 수금된 청구서로
    # 입금을 만들어 의도한 30% exact 분포가 깨짐 (2026-06-12 점검에서 발견).
    open_sales = [s for s in sales if s[3] != "paid"]
    if not open_sales:
        return

    for _ in range(n):
        case = random.choices(
            ["exact_match", "fuzzy_name", "split", "partial", "unmatched"],
            weights=[30, 20, 15, 15, 20],
        )[0]
        received_at = (base + timedelta(days=random.randint(0, 100))).isoformat()

        if case == "exact_match":
            _, partner_id, amount, _ = random.choice(open_sales)
            partner_name = conn.execute(
                "SELECT name FROM partners WHERE partner_id = ?", (partner_id,)
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO payments (partner_name_as_received, amount, received_at, matched_invoice_ids, match_status) VALUES (?, ?, ?, ?, 'unmatched')",
                (partner_name, amount, received_at, None),
            )

        elif case == "fuzzy_name":
            _, partner_id, amount, _ = random.choice(open_sales)
            partner_name = conn.execute(
                "SELECT name FROM partners WHERE partner_id = ?", (partner_id,)
            ).fetchone()[0]
            # 표기 일부러 깎음
            distorted = partner_name.replace("(주)", "").replace("주식회사 ", "").strip()
            if " " in distorted:
                distorted = distorted.replace(" ", "")
            conn.execute(
                "INSERT INTO payments (partner_name_as_received, amount, received_at, match_status) VALUES (?, ?, ?, 'unmatched')",
                (distorted, amount, received_at),
            )

        elif case == "split":
            # 같은 거래처의 미수 청구서 2-3개 합산
            partner_id = random.choice(partner_ids)
            partner_invoices = [s for s in open_sales if s[1] == partner_id]
            if len(partner_invoices) >= 2:
                k = min(random.randint(2, 3), len(partner_invoices))
                chosen = random.sample(partner_invoices, k=k)
                total = sum(c[2] for c in chosen)
                partner_name = conn.execute(
                    "SELECT name FROM partners WHERE partner_id = ?", (partner_id,)
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO payments (partner_name_as_received, amount, received_at, match_status) VALUES (?, ?, ?, 'unmatched')",
                    (partner_name, total, received_at),
                )
            else:
                # fallback unmatched
                conn.execute(
                    "INSERT INTO payments (partner_name_as_received, amount, received_at, match_status) VALUES (?, ?, ?, 'unmatched')",
                    (fake.company(), random.randint(100_000, 2_000_000), received_at),
                )

        elif case == "partial":
            _, partner_id, amount, _ = random.choice(open_sales)
            partial_amount = int(amount * random.uniform(0.3, 0.7))
            partner_name = conn.execute(
                "SELECT name FROM partners WHERE partner_id = ?", (partner_id,)
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO payments (partner_name_as_received, amount, received_at, match_status) VALUES (?, ?, ?, 'unmatched')",
                (partner_name, partial_amount, received_at),
            )

        else:  # unmatched — 거래처명 자체가 DB 에 없음
            conn.execute(
                "INSERT INTO payments (partner_name_as_received, amount, received_at, match_status) VALUES (?, ?, ?, 'unmatched')",
                (fake.company(), random.randint(100_000, 2_000_000), received_at),
            )


def reset_tables(conn) -> None:
    for tbl in ("payments", "sales", "inventory_movements", "items", "partners"):
        conn.execute(f"DELETE FROM {tbl}")
    # AUTOINCREMENT 카운터 리셋
    try:
        conn.execute("DELETE FROM sqlite_sequence")
    except sqlite3.OperationalError:
        pass


def main() -> int:
    p = argparse.ArgumentParser(description="KS Jarvis 더미 데이터 seed")
    p.add_argument("--db", default=None, help=f"DB 경로 (기본: {DEFAULT_DB_PATH})")
    p.add_argument("--reset", action="store_true", help="기존 테이블 비우고 새로 생성")
    p.add_argument("--partners", type=int, default=50)
    p.add_argument("--items", type=int, default=200)
    p.add_argument("--movements", type=int, default=1000)
    p.add_argument("--sales", type=int, default=100)
    p.add_argument("--payments", type=int, default=100)
    args = p.parse_args()

    with connection(args.db) as conn:
        init_schema(conn)
        if args.reset:
            reset_tables(conn)
            print("[i] 기존 테이블 비움.", file=sys.stderr)

        partner_ids = seed_partners(conn, args.partners)
        print(f"[i] partners {len(partner_ids)}", file=sys.stderr)

        skus = seed_items(conn, partner_ids, args.items)
        print(f"[i] items {len(skus)}", file=sys.stderr)

        seed_movements(conn, skus, args.movements)
        print(f"[i] inventory_movements {args.movements}", file=sys.stderr)

        sales = seed_sales(conn, partner_ids, args.sales)
        print(f"[i] sales {len(sales)}", file=sys.stderr)

        seed_payments(conn, partner_ids, sales, args.payments)
        print(f"[i] payments {args.payments}", file=sys.stderr)

    print("[+] seed 완료.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
