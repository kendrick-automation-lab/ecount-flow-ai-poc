"""Slack 알림 — incoming webhook 으로 채널에 메시지 발송 (Flow 협업툴 대역).

왜 Slack: Flow OpenAPI 가 유료 '비즈니스 프로' 플랜 게이트라, 같은 '협업툴 알림' 패턴을
게이트 없는 Slack 으로 가시적 시연. (정직: Flow 가 아니라 Slack. 어댑터 교체로 동일.)
Slack 은 실제 팀 협업툴이라 '업무 채널에 운영 알림 발송' 이라는 회사 맥락이 그대로 산다.

env: SLACK_WEBHOOK_URL  (https://hooks.slack.com/services/...)
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx


@dataclass
class SlackNotifier:
    webhook_url: str
    timeout: float = 15.0

    @classmethod
    def from_env(cls) -> "SlackNotifier":
        url = os.getenv("SLACK_WEBHOOK_URL", "").strip()
        if not url:
            raise RuntimeError(".env 에 SLACK_WEBHOOK_URL 필요 (Slack incoming webhook URL).")
        return cls(webhook_url=url)

    def send(self, text: str) -> bool:
        """채널에 메시지 발송. 성공 시 True. (Slack webhook 은 성공 시 'ok' 200 반환)"""
        r = httpx.post(self.webhook_url, json={"text": text}, timeout=self.timeout)
        return r.status_code == 200
