"""#jarvis 채널에서 @Jarvis 멘션 → Q&A 에이전트가 ERP 조회해 답변 (Tier 3 대화).

JD 정면 매칭: 주요업무1(KS Quantum Jarvis = 대화형 중앙 AI) + 필수2(에이전트화) +
필수7(API + Webhook **받기** — Slack 이벤트 수신). "webhook 보내기만" 약점을 실제 '받기' 로 업그레이드.

Socket Mode = 공개 서버 없이도 Slack 이벤트를 실시간 수신 (로컬/서버에서 상시 연결).
24/7 안 켜도 됨 — 데모할 때만 실행해서 시연하면 완전한 증명.

env (.env):
  SLACK_BOT_TOKEN=xoxb-...   (OAuth & Permissions)
  SLACK_APP_TOKEN=xapp-...   (Socket Mode)
  (옵션) ANTHROPIC_API_KEY → 실제 Claude tool-use / 없으면 mock
  (옵션) AIRTABLE_* + --backend airtable → 실 Airtable 조회 / 기본 SQLite 더미

사용:
  python scripts/slack_jarvis_listen.py            # SQLite 더미 백엔드
  python scripts/slack_jarvis_listen.py --backend airtable
  → #jarvis 에서:  @Jarvis 현대건자재 미수금 얼마야?   (Ctrl+C 로 종료)
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402


def _open_db(backend: str):
    if backend == "airtable":
        from src.airtable_client import AirtableClient
        return AirtableClient.from_env()
    from src.db import DEFAULT_DB_PATH
    from src.ecount_db import EcountDBClient
    return EcountDBClient.from_db(DEFAULT_DB_PATH)


def main() -> int:
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["sqlite", "airtable"], default="sqlite")
    args = ap.parse_args()

    bot_token = os.getenv("SLACK_BOT_TOKEN", "").strip()
    app_token = os.getenv("SLACK_APP_TOKEN", "").strip()
    if not bot_token or not app_token:
        print("[!] .env 에 SLACK_BOT_TOKEN(xoxb-) / SLACK_APP_TOKEN(xapp-) 둘 다 필요.", file=sys.stderr)
        return 2

    try:
        from slack_bolt import App
        from slack_bolt.adapter.socket_mode import SocketModeHandler
    except ImportError:
        print("[!] slack_bolt 미설치 — pip install -r requirements.txt", file=sys.stderr)
        return 2

    from src.jarvis_agent import QAAgent

    db = _open_db(args.backend)
    agent = QAAgent()  # ANTHROPIC_API_KEY 있으면 실제 Claude tool-use, 없으면 mock
    mode = "MOCK" if agent.mock else f"LLM({agent.model})"
    print(f"[i] Jarvis 리스너 시작 — 백엔드 {args.backend} / 에이전트 {mode}", file=sys.stderr)
    print("    #jarvis 채널에서  @봇  멘션 + 질문.  (Ctrl+C 종료)", file=sys.stderr)

    from src.conversation_memory import ConversationMemory
    mem = ConversationMemory(ROOT / "data" / "jarvis_memory.json")  # 디스크 영속 메모리
    print(f"[i] 대화 메모리: {mem.path} (재시작/세션 넘어도 맥락 유지)", file=sys.stderr)

    app = App(token=bot_token)

    # 채널 알림(ALERT) — 사람이 "보내줘/발송/넣어" 명시 요청 시 리스너가 브리핑을 업무 채널에 발송.
    # 에이전트는 read-only 유지. 알림은 ERP 변경이 아니라 '보고' 라 안전.
    ALERT_CHANNELS = {"재고": "#재고", "재무": "#재무", "미수": "#재무"}

    def _try_send_alert(question: str):
        q = question.lower()
        is_alert = (("alert" in q or "알림" in question or "알럿" in question or "얼럿" in question)
                    and any(x in question for x in ("보내", "발송", "줘", "채널", "공유", "넣어")))
        if not is_alert:
            return None
        from src.agent_tools import dispatch
        b = dispatch(db, "get_daily_briefing", {})
        sent = []
        for p in b.get("priorities", []):
            ch = ALERT_CHANNELS.get(p.get("area"))
            if not ch:
                continue
            try:
                app.client.chat_postMessage(
                    channel=ch,
                    text=(f":rotating_light: *[{p['area']} 알림]* {p['action']}\n{p['detail']}\n"
                          "_Jarvis 분석 결과입니다. 발주·매칭 실행은 담당자가 승인하세요._"),
                )
                sent.append(ch)
            except Exception as e:  # noqa: BLE001
                print(f"[!] alert {ch} 실패: {e}", file=sys.stderr)
        if not sent:
            return ":warning: 알림 채널 발송에 실패했어요. 봇이 #재고·#재무·#구매 채널에 초대돼 있는지 확인해 주세요."
        return (f"필요한 항목을 채널에 알림으로 보냈어요: {', '.join(sent)}.\n"
                "저는 ERP 데이터를 직접 바꾸진 못하고, 분석 결과를 알림으로 띄우는 것까지 합니다. "
                "발주·입금 매칭 같은 실행은 담당자가 승인하세요.")

    @app.event("app_mention")
    def on_mention(event, say):  # noqa: ANN001
        raw = event.get("text", "")
        question = re.sub(r"<@[^>]+>", "", raw).strip()  # @봇 멘션 토큰 제거
        channel = event.get("channel", "default")
        if not question:
            say("질문을 같이 적어줘 — 예: `@Jarvis 현대건자재 미수금 얼마야?`")
            return
        alert = _try_send_alert(question)
        if alert is not None:
            mem.append(channel, "user", question)
            mem.append(channel, "assistant", alert)
            say(alert)
            return
        say(f":hourglass_flowing_sand: 조회 중… _{question}_")
        try:
            res = agent.ask(db, question, history=mem.recent(channel, 8))  # 영속 메모리에서 최근 맥락
            ans = res.answer or "데이터로 확인하기 어려운 질문이라 담당자 확인이 필요해요."
            mem.append(channel, "user", question)        # 디스크에 즉시 영속 저장
            mem.append(channel, "assistant", ans)
            tools = res.trace.tool_calls
            say(f"{ans}\n\n_(도구 {tools}회 · {'mock' if res.mock else agent.model})_")
        except Exception as e:  # noqa: BLE001
            say(f":warning: 처리 중 오류: {type(e).__name__}")

    @app.event("message")
    def on_dm(event, say):  # noqa: ANN001
        # DM(1:1)만 처리 — 멘션 없이 사람처럼 대화. 봇 자신/수정/시스템 메시지는 무시.
        if event.get("channel_type") != "im" or event.get("bot_id") or event.get("subtype"):
            return
        question = (event.get("text") or "").strip()
        channel = event.get("channel", "dm")
        if not question:
            return
        alert = _try_send_alert(question)
        if alert is not None:
            mem.append(channel, "user", question)
            mem.append(channel, "assistant", alert)
            say(alert)
            return
        say(f":hourglass_flowing_sand: 조회 중… _{question}_")
        try:
            res = agent.ask(db, question, history=mem.recent(channel, 8))
            ans = res.answer or "데이터로 확인하기 어려운 질문이라 담당자 확인이 필요해요."
            mem.append(channel, "user", question)
            mem.append(channel, "assistant", ans)
            say(f"{ans}\n\n_(도구 {res.trace.tool_calls}회 · {'mock' if res.mock else agent.model})_")
        except Exception as e:  # noqa: BLE001
            say(f":warning: 처리 중 오류: {type(e).__name__}")

    SocketModeHandler(app, app_token).start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
