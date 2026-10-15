"""과제 공개 데이터 OPEN API 조회 클라이언트 — 상담 통계 도구(lookup_local_stats)용.

설정(환경변수)
    IITP_OPENAPI_BASE_URL   OPEN API 주소(예: https://<host>/iitp/api). 비우면 통계 도구를 선언하지 않는다
    IITP_OPENAPI_KEY        이 서비스 전용 인증키(X-API-KEY). 비우면 통계 도구를 선언하지 않는다
    IITP_OPENAPI_TIMEOUT    호출 1건 시간 상한(초, 기본 6)

응답은 {"success": true, "data": ...} 형식이다. 실패는 예외 대신 {"status": "error", ...} 로 돌려준다
(도구가 그대로 모델에 넘겨 "지금은 조회가 안 된다"고 말하게 한다). 인증키는 로그에 남기지 않는다.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

import httpx

logger = logging.getLogger("openapi_client")

BASE_URL = os.environ.get("IITP_OPENAPI_BASE_URL", "").rstrip("/")
API_KEY = os.environ.get("IITP_OPENAPI_KEY", "").strip()
TIMEOUT_SEC = float(os.environ.get("IITP_OPENAPI_TIMEOUT", "6"))

# 같은 요청을 짧은 시간에 반복하지 않게 하는 메모리 캐시 — 통계는 월 단위로 바뀐다.
_CACHE_TTL_SEC = 600
_cache: dict = {}


def enabled() -> bool:
    return bool(BASE_URL and API_KEY)


def _err(message: str, detail: str = "") -> dict:
    return {"status": "error", "message": message, "detail": detail[:300],
            "ai_instruction": "통계 조회가 지금 되지 않습니다. 숫자를 지어내지 말고, 잠시 후 다시 물어보시거나 "
                              "시청 장애인복지과에 문의하시라고 안내하세요."}


async def get(path: str, params: Optional[dict] = None) -> Any:
    """GET {BASE_URL}/api/v1{path}. 성공하면 data 부분, 실패하면 오류 dict."""
    if not enabled():
        return _err("통계 조회 서비스가 설정되지 않았습니다.")
    key = (path, tuple(sorted((params or {}).items())))
    hit = _cache.get(key)
    now = time.monotonic()
    if hit and now - hit[0] < _CACHE_TTL_SEC:
        return hit[1]
    url = BASE_URL + "/api/v1" + path
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SEC) as client:
            resp = await client.get(url, params=params or {}, headers={"X-API-KEY": API_KEY})
    except httpx.HTTPError as exc:
        logger.warning("openapi %s 호출 실패: %s", path, type(exc).__name__)
        return _err("통계 조회 서버에 연결하지 못했습니다.", type(exc).__name__)
    if resp.status_code == 404:
        return {"status": "not_found", "message": "해당 지역 자료가 없습니다."}
    if resp.status_code != 200:
        logger.warning("openapi %s 응답 %s", path, resp.status_code)
        return _err("통계 조회가 실패했습니다.", "HTTP %d" % resp.status_code)
    try:
        body = resp.json()
    except ValueError:
        return _err("통계 조회 응답을 읽지 못했습니다.")
    if not isinstance(body, dict) or not body.get("success"):
        return _err("통계 조회가 실패했습니다.", str((body or {}).get("error") if isinstance(body, dict) else "")[:200])
    data = body.get("data")
    _cache[key] = (now, data)
    if len(_cache) > 200:
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:50]:
            _cache.pop(k, None)
    return data
