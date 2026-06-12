"""더미 Flow 협업툴 클라이언트.

실제 Flow Open API 사양 확인 전이라, PoC 는:
- 메시지 발송 = stdout print + 인메모리 기록
- 메시지 수신 (사람 confirm / 옵션 선택) = 시뮬레이션 (사전 큐잉)
- 채널 검색 = sample JSON 에서 키워드 필터

확장 (Step 3):
- 채널 enum 명시 — 시나리오별 발송 채널 명확화
- generic `queue_user_response(key, response)` — sku / payment_id / quote_ref 등 모든 key 지원
- `request_decision()` — interactive button mock (옵션 + 멘션 + audit log)

실제 운영 시 `send_message` / `fetch_recent_mentions` / `request_decision` 를 Flow API 호출로 교체.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# ─────────────────────────────────────────────
# 채널 enum — 시나리오별 발송 채널
# ─────────────────────────────────────────────

CHANNEL_PURCHASE = "C-PURCHASE"      # 구매팀 (안전재고 발주 / 구매 입력)
CHANNEL_FINANCE = "C-FINANCE"        # 재무팀 (입금 매칭)
CHANNEL_INVENTORY = "C-INVENTORY"    # 재고관리 (안전재고 수동 검토 / 보류)
CHANNEL_OPS = "C-OPS"                # 운영 / audit log

ALL_CHANNELS = (CHANNEL_PURCHASE, CHANNEL_FINANCE, CHANNEL_INVENTORY, CHANNEL_OPS)


def mention(name: str) -> str:
    """담당자 / 팀 mention helper. 실제 Flow API 에선 user_id 기반이지만 PoC 는 텍스트."""
    return f"@{name}"


@dataclass
class FlowMessage:
    channel: str
    author: str
    ts: str
    text: str


@dataclass
class FlowClient:
    messages_path: Path
    _sent: list[dict] = field(default_factory=list)
    _user_responses: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_sample(cls, sample_path: str | Path) -> "FlowClient":
        return cls(messages_path=Path(sample_path))

    def _load(self) -> dict:
        if not self.messages_path.exists():
            return {"channels": [], "recent_messages": []}
        return json.loads(self.messages_path.read_text(encoding="utf-8"))

    def fetch_recent_mentions(self, keyword: str, days: int = 30) -> list[FlowMessage]:
        """keyword 가 등장한 최근 메시지 (sku / partner 이름 등 무엇이든 OK).

        실제 운영: Flow 검색 API (있으면) 또는 메시지 전체 fetch 후 필터.
        """
        data = self._load()
        out: list[FlowMessage] = []
        for msg in data.get("recent_messages", []):
            if keyword.lower() in msg["text"].lower():
                out.append(FlowMessage(**msg))
        return out

    def send_message(
        self,
        channel: str,
        text: str,
        actions: list[str] | None = None,
    ) -> dict[str, Any]:
        """채널에 메시지 발송 (더미: stdout + 인메모리).

        실제 운영: Flow REST API 의 메시지 발송 endpoint.
        """
        record = {
            "channel": channel,
            "text": text,
            "actions": actions or [],
            "ts": datetime.now().isoformat(),
        }
        self._sent.append(record)
        actions_str = " ".join(f"[{a}]" for a in record["actions"]) if record["actions"] else ""
        print(f"\n[Flow → {channel}]")
        print(text)
        if actions_str:
            print(f"actions: {actions_str}")
        return record

    def request_decision(
        self,
        channel: str,
        decision_key: str,
        title: str,
        body: str,
        options: list[dict[str, str]],
        mentions: list[str] | None = None,
    ) -> dict[str, Any]:
        """interactive button mock — 옵션 + 멘션 캡슐화.

        options: [{"key": "match-1", "label": "후보 #1 (신뢰도 92%)"}, ...]
        반환된 메시지는 wait_for_user_response(decision_key) 로 응답 대기 가능.
        """
        mentions_str = " ".join(mention(m) for m in (mentions or []))
        head = f"*{title}*"
        if mentions_str:
            head = f"{mentions_str} {head}"
        full_text = f"{head}\n\n{body}".strip()
        action_labels = [f"{o['key']}: {o['label']}" for o in options]
        record = self.send_message(channel, full_text, actions=action_labels)
        record["decision_key"] = decision_key
        record["options"] = options
        return record

    # ─────────────────────────────────────────────
    # 사용자 응답 시뮬레이션 (generic key 기반)
    # ─────────────────────────────────────────────

    def queue_user_response(self, key: str, response: str) -> None:
        """테스트용 — 사람 응답 미리 큐잉.

        key: sku / payment_id / quote_ref / decision_key 등 시나리오별 식별자.
        response: 'approve' / 'reject' / 'modify' / 'match-1' / 'manual' / 'timeout' 등.
        """
        self._user_responses[key] = response

    def wait_for_user_response(
        self,
        key: str,
        timeout_seconds: int = 1800,  # noqa: ARG002 (PoC 더미)
    ) -> str:
        """응답 대기 (더미: 큐잉된 응답 즉시 반환 / 없으면 'timeout')."""
        return self._user_responses.get(key, "timeout")

    # ─────────────────────────────────────────────
    # 시나리오 1 호환 alias (기존 orchestrator 코드 변경 없이 동작)
    # ─────────────────────────────────────────────

    def queue_confirm_response(self, sku: str, response: str) -> None:
        self.queue_user_response(sku, response)

    def wait_for_confirm(self, sku: str, timeout_seconds: int = 1800) -> str:
        return self.wait_for_user_response(sku, timeout_seconds)

    @property
    def sent_messages(self) -> list[dict]:
        return list(self._sent)
