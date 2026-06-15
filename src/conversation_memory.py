"""대화 메모리 — 채널별 대화 history 를 파일에 영속 저장.

RAM 변수는 리스너가 꺼지면 날아간다. 이건 디스크(JSON)에 저장해서
**리스너 재시작·세션을 넘어도 대화 흐름/맥락이 이어지게** 한다.
JD 'Context Engineering' 매핑: 대화 상태를 보존해 흐름이 끊기지 않게.

저장 위치: data/jarvis_memory.json (사람이 열어볼 수 있음 = 가시적).
채널별로 최근 max_per_channel 개 메시지를 유지 (무한 증가 방지).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any


class ConversationMemory:
    def __init__(self, path: Path | str, max_per_channel: int = 40) -> None:
        self.path = Path(path)
        self.max = max_per_channel
        self._lock = threading.Lock()
        self._data: dict[str, list[dict[str, Any]]] = self._read()

    def _read(self) -> dict[str, list[dict[str, Any]]]:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")

    def load(self, channel: str) -> list[dict[str, Any]]:
        """채널의 전체 저장 history (복사본)."""
        return list(self._data.get(channel, []))

    def recent(self, channel: str, n: int = 8) -> list[dict[str, Any]]:
        """최근 n개 메시지 (에이전트에 넘길 맥락 — 토큰 가드)."""
        return self.load(channel)[-n:]

    def append(self, channel: str, role: str, content: str) -> None:
        """한 메시지를 저장하고 즉시 파일에 영속화 (채널별 상한 유지)."""
        with self._lock:
            turns = self._data.setdefault(channel, [])
            turns.append({"role": role, "content": content})
            if len(turns) > self.max:
                del turns[: len(turns) - self.max]
            self._write()
