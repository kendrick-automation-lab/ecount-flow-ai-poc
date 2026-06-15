"""Slack Bot — chat.postMessage 로 여러 채널에 발송 (Tier 3 멀티채널).

Flow OpenAPI 가 유료 게이트라, 같은 '협업툴 멀티채널 발송' 패턴을 게이트 없는 Slack Bot 으로 시연.
incoming webhook(한 채널·한 방향)과 달리, bot 토큰은 봇이 초대된 어느 채널에든 발송 가능.

env: SLACK_BOT_TOKEN (xoxb-...)
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError


@dataclass
class SlackBot:
    token: str

    @classmethod
    def from_env(cls) -> "SlackBot":
        t = os.getenv("SLACK_BOT_TOKEN", "").strip()
        if not t:
            raise RuntimeError(".env 에 SLACK_BOT_TOKEN (xoxb-...) 필요 — OAuth & Permissions 에서 발급.")
        return cls(token=t)

    def post(self, channel: str, text: str) -> bool:
        """채널(#재고 등 이름 또는 채널 ID)에 메시지 발송. 봇이 그 채널에 초대돼 있어야 함."""
        client = WebClient(token=self.token)
        try:
            client.chat_postMessage(channel=channel, text=text)
            return True
        except SlackApiError as e:
            print(f"[!] Slack 발송 실패 ({channel}): {e.response.get('error')}")
            return False
