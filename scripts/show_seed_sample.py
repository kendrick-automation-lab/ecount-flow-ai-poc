"""더미 데이터 샘플 출력 — '무슨 데이터로 데모했나' 를 눈으로 확인용.

사용: python scripts/show_seed_sample.py
(db_seed.py 로 생성된 data/ks_jarvis_demo.db 를 읽어 각 테이블 요약 + 샘플 3행)
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

# Windows cp949 콘솔에서 한글 안전
for stream in (sys.stdout, sys.stderr):
    rc = getattr(stream, "reconfigure", None)
    if callable(rc):
        rc(encoding="utf-8", errors="replace")

DB = Path(__file__).resolve().parent.parent / "data" / "ks_jarvis_demo.db"

TABLES = {
    "partners": "거래처 (이름 표기 4변형: (주)/공백/주식회사/평문 — fuzzy 매칭 테스트용)",
    "items": "품목 (건축자재 7카테고리, 일부 안전재고 미만으로 의도 설계)",
    "inventory_movements": "재고 입출고 이력 (breach SKU 는 출고 가중치 80%)",
    "sales": "매출 청구서 (pending/paid/partial)",
    "payments": "입금 (전부 unmatched 시작 — 정확/표기변형/분할/부분/미매칭 5케이스)",
}


def main() -> int:
    if not DB.exists():
        print(f"[!] DB 없음: {DB}\n    먼저: python scripts/db_seed.py --reset", file=sys.stderr)
        return 2
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    for tbl, desc in TABLES.items():
        n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        print(f"\n[{tbl}] 총 {n}행 — {desc}")
        for r in conn.execute(f"SELECT * FROM {tbl} LIMIT 3").fetchall():
            print("   ", dict(r))
    conn.close()
    print("\n※ 전부 합성 데이터 (faker, seed=42 결정론적). 실제 KS 데이터 아님.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
