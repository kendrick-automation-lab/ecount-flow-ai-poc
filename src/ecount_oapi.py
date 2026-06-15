"""실제 이카운트 OAPI 연동 어댑터 (Strategy 패턴 — 더미 EcountDBClient 자리 교체용).

★ 1단계 목표: 인증(Zone → Login → SESSION_ID)이 **실제 sboapi 테스트존**에서 되는지 증명.
   → 성공하면 "더미가 아니라 진짜 이카운트 연동" 이 증명됨 (포트폴리오 핵심 카드).
   데이터 조회 메서드는 실응답을 받아 필드명을 확정한 뒤 확장한다 (지금은 best-effort + 방어적 파싱).

인증 흐름 (공식 문서 기준):
  1) Zone 조회 : POST {PREFIX}.ecount.com/OAPI/V2/Zone           (COM_CODE) → ZONE
  2) 로그인    : POST {PREFIX}{ZONE}.ecount.com/OAPI/V2/OAPILogin (COM_CODE, USER_ID, API_CERT_KEY, ZONE) → SESSION_ID
  3) 이후 호출 : {PREFIX}{ZONE}.ecount.com/OAPI/V2/<endpoint>?SESSION_ID=...  (POST)

PREFIX = 테스트 'sboapi' / 실서버 'oapi'.
(문서: "Test Key로 1회 이상 정상 호출하면 이후에는 실 API Key 사용")

방어적 설계: 실제 응답 JSON 구조(키 중첩)가 문서와 다를 수 있어, ZONE/SESSION_ID 를
재귀 탐색으로 찾고, 실패 시 **raw 응답을 그대로 반환**해서 실응답 보고 바로 고칠 수 있게 함.

env (.env):
  ECOUNT_COM_CODE / ECOUNT_USER_ID / ECOUNT_API_KEY   (필수)
  ECOUNT_TEST=true   (기본 true → sboapi 테스트존)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

LAN_TYPE = "ko-KR"


def _find_key(obj: Any, target: str) -> Any:
    """중첩 dict/list 에서 target 키의 첫 값을 재귀 탐색 (응답 구조 불확실 대비)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == target and v not in (None, ""):
                return v
            found = _find_key(v, target)
            if found not in (None, ""):
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_key(item, target)
            if found not in (None, ""):
                return found
    return None


@dataclass
class EcountOAPIClient:
    com_code: str
    user_id: str
    api_cert_key: str
    test: bool = True
    zone: str = ""
    session_id: str = ""
    timeout: float = 20.0

    @classmethod
    def from_env(cls) -> "EcountOAPIClient":
        com = os.getenv("ECOUNT_COM_CODE", "").strip()
        uid = os.getenv("ECOUNT_USER_ID", "").strip()
        key = os.getenv("ECOUNT_API_KEY", "").strip()
        test = os.getenv("ECOUNT_TEST", "true").strip().lower() != "false"
        missing = [n for n, v in (("ECOUNT_COM_CODE", com), ("ECOUNT_USER_ID", uid), ("ECOUNT_API_KEY", key)) if not v]
        if missing:
            raise RuntimeError(f".env 에 {', '.join(missing)} 가 필요합니다 (이카운트 API 인증키발급 후).")
        return cls(com_code=com, user_id=uid, api_cert_key=key, test=test)

    @property
    def _prefix(self) -> str:
        return "sboapi" if self.test else "oapi"

    def _zone_host(self) -> str:
        return f"https://{self._prefix}.ecount.com"

    def _api_host(self) -> str:
        return f"https://{self._prefix}{self.zone}.ecount.com"

    # ── 1) Zone 조회 ──
    def get_zone(self) -> dict[str, Any]:
        url = f"{self._zone_host()}/OAPI/V2/Zone"
        r = httpx.post(url, json={"COM_CODE": self.com_code}, timeout=self.timeout)
        raw = _safe_json(r)
        zone = _find_key(raw, "ZONE")
        if zone:
            self.zone = str(zone)
        return {"ok": bool(zone), "zone": self.zone, "status": r.status_code, "raw": raw}

    # ── 2) 로그인 → SESSION_ID ──
    def login(self) -> dict[str, Any]:
        if not self.zone:
            return {"ok": False, "error": "zone 미확보 — get_zone() 먼저", "session_id": ""}
        url = f"{self._api_host()}/OAPI/V2/OAPILogin"
        payload = {
            "COM_CODE": self.com_code,
            "USER_ID": self.user_id,
            "API_CERT_KEY": self.api_cert_key,
            "LAN_TYPE": LAN_TYPE,
            "ZONE": self.zone,
        }
        r = httpx.post(url, json=payload, timeout=self.timeout)
        raw = _safe_json(r)
        sid = _find_key(raw, "SESSION_ID")
        if sid:
            self.session_id = str(sid)
        return {"ok": bool(sid), "session_id": self.session_id, "status": r.status_code, "raw": raw}

    # ── 연결 자가진단 (1단계 목표) ──
    def self_check(self) -> dict[str, Any]:
        """Zone → Login 을 돌려 인증이 실제로 되는지 확인. 실패해도 raw 를 담아 반환."""
        z = self.get_zone()
        if not z["ok"]:
            return {"ok": False, "stage": "zone", "detail": z}
        lg = self.login()
        if not lg["ok"]:
            return {"ok": False, "stage": "login", "zone": self.zone, "detail": lg}
        return {"ok": True, "stage": "done", "zone": self.zone,
                "session_id_preview": self.session_id[:8] + "…" if self.session_id else ""}

    # ── 3) 데이터 조회 (실응답 받아 필드 확정 후 확장 — 현재 best-effort) ──
    def _post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.session_id:
            raise RuntimeError("session_id 없음 — self_check()/login() 먼저")
        url = f"{self._api_host()}/OAPI/V2/{endpoint}?SESSION_ID={self.session_id}"
        r = httpx.post(url, json=payload, timeout=self.timeout)
        return _safe_json(r)

    def list_inventory_balance(self, base_date: str) -> dict[str, Any]:
        """재고 현황 조회 (엔드포인트는 확인됨; 요청 바디 필드는 실응답으로 확정 필요).

        실제 endpoint: InventoryBalance/GetListInventoryBalanceStatusByLocation
        base_date: 'YYYYMMDD'
        """
        # TODO(실연동): BASE_DATE 외 필수 파라미터를 실응답 에러 메시지 보고 보정
        raw = self._post("InventoryBalance/GetListInventoryBalanceStatusByLocation", {"BASE_DATE": base_date})
        result = _find_key(raw, "Result")
        return {"result": result, "raw": raw}


def _safe_json(r: httpx.Response) -> Any:
    try:
        return r.json()
    except Exception:  # noqa: BLE001 — JSON 아니면 텍스트 그대로 (디버그용)
        return {"_non_json_text": r.text[:1000], "_status": r.status_code}
