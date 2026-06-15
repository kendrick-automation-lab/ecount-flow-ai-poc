"""Slack 발송 테스트 — webhook 으로 샘플 Jarvis 알림을 채널에 보냄.

사용:
  .env 에 SLACK_WEBHOOK_URL=https://hooks.slack.com/services/... 넣고
  python scripts/slack_test.py
성공하면 Slack 채널에 메시지가 뜬다 (가시적 증거).
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

from src.slack_client import SlackNotifier  # noqa: E402

SAMPLE = (
    "🌅 *Jarvis 아침 브리핑* (테스트 발송)\n"
    "1. *재고* — 안전재고 미만 32개 품목 (발주 검토 후보)\n"
    "2. *재무* — 미매칭 입금 100건 / 2.25억 (정리 권장)\n"
    "3. *채권* — 미수 집중 Top: 한솔산업8 1,514만원\n"
    "— read-only 분석. 실행은 담당자 승인 후."
)


def main() -> int:
    load_dotenv(ROOT / ".env")
    try:
        notifier = SlackNotifier.from_env()
    except RuntimeError as e:
        print(f"[!] {e}", file=sys.stderr)
        return 2
    ok = notifier.send(SAMPLE)
    print("[+] Slack 발송 성공 — 채널 확인!" if ok else "[!] 발송 실패 (webhook URL 확인)", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
