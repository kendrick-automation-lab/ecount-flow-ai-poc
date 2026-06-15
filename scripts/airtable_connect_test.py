"""Airtable 연결 자가진단 — 토큰 + base ID 가 실제로 되는지 + 테이블 목록 확인.

사용:
  .env 에  AIRTABLE_TOKEN=pat...  /  AIRTABLE_BASE_ID=app...  넣고
  python scripts/airtable_connect_test.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for stream in (sys.stdout, sys.stderr):
    rc = getattr(stream, "reconfigure", None)
    if callable(rc):
        rc(encoding="utf-8", errors="replace")

from dotenv import load_dotenv  # noqa: E402

from src.airtable_client import AirtableClient  # noqa: E402


def main() -> int:
    load_dotenv(ROOT / ".env")
    try:
        client = AirtableClient.from_env()
    except RuntimeError as e:
        print(f"[!] {e}", file=sys.stderr)
        return 2
    print(f"[i] Airtable 연결 시도 — base {client.base_id}", file=sys.stderr)
    try:
        tables = client.list_tables()
    except Exception as e:  # noqa: BLE001
        print(f"[!] 실패: {type(e).__name__}: {e}", file=sys.stderr)
        print("    토큰 풀값/스코프(schema.bases:read)/base 접근 권한 확인.", file=sys.stderr)
        return 1
    print(f"[+] 인증 성공! 테이블 {len(tables)}개:", file=sys.stderr)
    for t in tables:
        print(f"    - {t.get('name')} ({len(t.get('fields', []))} fields)", file=sys.stderr)
    if not any(t.get("name") in ("Partners", "Items", "Sales", "Payments") for t in tables):
        print("    (우리 테이블 없음 → python scripts/airtable_seed.py 로 시딩)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
