"""Slack Bot 발송 테스트 — xoxb 봇 토큰으로 채널에 메시지 발송 (Tier 3 연결 확인).

사용: python scripts/slack_bot_test.py [#채널]   (기본 #jarvis)
성공하면 채널에 메시지가 뜬다.
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

from src.slack_bot import SlackBot  # noqa: E402


def main() -> int:
    load_dotenv(ROOT / ".env")
    channel = sys.argv[1] if len(sys.argv) > 1 else "#jarvis"
    try:
        bot = SlackBot.from_env()
    except RuntimeError as e:
        print(f"[!] {e}", file=sys.stderr)
        return 2
    ok = bot.post(channel, "🤖 *Jarvis 연결 테스트* — 봇 토큰 정상. 곧 `@Ksjarvis` 멘션으로 대화 가능합니다.")
    print(f"[{'+' if ok else '!'}] {channel} 발송 {'성공 — 채널 확인!' if ok else '실패 (위 오류/봇 채널초대 확인)'}",
          file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
