"""이카운트 OAPI 실연동 자가진단 — 인증(Zone→Login→SESSION_ID)이 진짜 되는지 확인.

사용:
  1) 이카운트 무료 데모체험 가입 → 테스트용 회사 생성
  2) Self-Customizing → 정보관리 → API 인증키발급 (Test Key)
  3) .env 에 추가:
       ECOUNT_COM_CODE=회사코드
       ECOUNT_USER_ID=사용자ID
       ECOUNT_API_KEY=발급받은_Test_Key
       ECOUNT_TEST=true
  4) python scripts/ecount_connect_test.py

성공하면 "더미가 아니라 실제 이카운트 테스트존 연동" 이 증명됨.
실패해도 raw 응답을 출력하니, 그걸 보고 필드/호스트를 바로 보정한다.
"""

from __future__ import annotations

import json
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

from src.ecount_oapi import EcountOAPIClient  # noqa: E402


def main() -> int:
    load_dotenv(ROOT / ".env")
    try:
        client = EcountOAPIClient.from_env()
    except RuntimeError as e:
        print(f"[!] {e}\n", file=sys.stderr)
        print("먼저 이카운트 무료 데모체험 가입 → API 인증키발급 후 .env 에 키를 넣어줘.", file=sys.stderr)
        print("(이 파일 상단 주석에 단계별 안내 있음)", file=sys.stderr)
        return 2

    mode = "테스트존(sboapi)" if client.test else "실서버(oapi)"
    print(f"[i] 이카운트 연결 시도 — {mode} / 회사코드 {client.com_code}\n", file=sys.stderr)

    result = client.self_check()
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if result["ok"]:
        print(f"\n[+] 인증 성공! ZONE={result['zone']} / SESSION 발급됨 → 실제 이카운트 연동 확인.", file=sys.stderr)
        print("    다음: 테스트 회사에 창고·품목·재고를 조금 넣고 데이터 조회 메서드 확장.", file=sys.stderr)
        return 0
    print(f"\n[!] {result['stage']} 단계 실패 — 위 raw 응답을 보고 호스트/필드 보정 필요.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
