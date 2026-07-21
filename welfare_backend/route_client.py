# -*- coding: utf-8 -*-
"""경로 추천 API(02-IITP-DABT-Route) 클라이언트.

경로·관광 기능은 별도 API 서비스로 두고 이 비서는 도구 호출만 수행한다(느슨한 결합).
경로 서비스가 죽어도 정책 상담은 계속 동작해야 하므로,
  - 짧은 타임아웃 + 1회 재시도
  - 연속 실패 시 서킷 오픈(30초) — 매 요청마다 지연되는 것을 막는다
  - 실패는 예외가 아니라 {"status": "error", ...} 로 반환해 LLM 이 상황을 설명할 수 있게 한다
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

import httpx

logger = logging.getLogger("route_client")

BASE_URL = os.environ.get("ROUTE_API_BASE_URL", "").rstrip("/")
API_TOKEN = os.environ.get("ROUTE_API_TOKEN", "")
TIMEOUT_SEC = float(os.environ.get("ROUTE_API_TIMEOUT", "6"))

FEATURE_ROUTE = os.environ.get("FEATURE_ROUTE", "0") == "1"
FEATURE_TOUR = os.environ.get("FEATURE_TOUR", "0") == "1"

_FAIL_THRESHOLD = 3
_OPEN_SEC = 30.0
_fail_count = 0
_open_until = 0.0


def enabled() -> bool:
    return bool(BASE_URL) and (FEATURE_ROUTE or FEATURE_TOUR)


def _headers() -> dict:
    h = {"Accept": "application/json"}
    if API_TOKEN:
        h["Authorization"] = "Bearer %s" % API_TOKEN
    return h


def _circuit_open() -> bool:
    return time.time() < _open_until


def _record(success: bool):
    global _fail_count, _open_until
    if success:
        _fail_count = 0
        _open_until = 0.0
        return
    _fail_count += 1
    if _fail_count >= _FAIL_THRESHOLD:
        _open_until = time.time() + _OPEN_SEC
        logger.warning("경로 API 연속 실패 %d회 — %.0f초간 호출 차단", _fail_count, _OPEN_SEC)


def _err(message: str, detail: str = "", ai_instruction: str = None) -> dict:
    return {
        "status": "error",
        "message": message,
        "detail": detail,
        "ai_instruction": ai_instruction or (
            "경로 안내 서비스에 일시적으로 연결할 수 없다고 사용자에게 짧게 알리고, "
            "정책 상담은 계속 이용 가능하다고 안내하세요. 경로를 추측해서 만들어내지 마세요."
        ),
    }


async def _call(method: str, path: str, *, params: Optional[dict] = None,
                json: Optional[dict] = None) -> dict:
    if not BASE_URL:
        return _err("경로 서비스가 설정되지 않았습니다")
    if _circuit_open():
        return _err("경로 서비스가 일시적으로 응답하지 않습니다")

    url = "%s%s" % (BASE_URL, path)
    last_detail = ""
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SEC) as client:
                r = await client.request(method, url, params=params, json=json,
                                         headers=_headers())
            if r.status_code >= 500:
                last_detail = "HTTP %d" % r.status_code
                continue
            _record(True)
            if r.status_code >= 400:
                body: Any = {}
                try:
                    body = r.json()
                except Exception:
                    body = {}
                detail_msg = body.get("detail") or "경로를 만들 수 없습니다"
                # 4xx 는 "일시적 장애"가 아니라 요청 자체의 문제(서비스 지역 밖 등) —
                # 오해를 낳지 않도록 사유 기반 안내문을 함께 전달한다.
                custom_ai = None
                if ("떨어져" in str(detail_msg)) or ("네트워크" in str(detail_msg)):
                    custom_ai = (
                        "출발지(또는 현재 위치)가 서비스 지역인 안양시 보행 데이터 범위를 벗어나 "
                        "경로를 만들 수 없다고 정확히 안내하세요. 안양시 내 출발지 이름(예: 안양역)을 "
                        "말씀해 주시거나 이동·관광 화면의 지도를 눌러 출발지를 지정하면 안내할 수 있다고 "
                        "덧붙이세요. 일시적인 오류라고 말하지 말고, 경로를 추측하지 마세요."
                    )
                return _err(detail_msg, "HTTP %d" % r.status_code, custom_ai)
            return r.json()
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_detail = "%s: %s" % (type(e).__name__, e)
            continue

    _record(False)
    logger.warning("경로 API 호출 실패 %s %s — %s", method, path, last_detail)
    return _err("경로 서비스에 연결하지 못했습니다", last_detail)


# ── 경로 ──
async def plan_route(origin: dict, destination: dict, profile: str = "wheelchair_manual",
                     alternatives: int = 1) -> dict:
    return await _call(
        "POST", "/route/plan",
        json={"origin": origin, "destination": destination,
              "profile": profile, "alternatives": alternatives},
    )


async def reroute(current: dict, destination: dict, profile: str = "wheelchair_manual",
                  route_id: str = None) -> dict:
    return await _call(
        "POST", "/route/reroute",
        json={"current": current, "destination": destination,
              "profile": profile, "route_id": route_id},
    )


async def get_route(route_id: str) -> dict:
    return await _call("GET", "/route/%s" % route_id)


async def profiles() -> dict:
    return await _call("GET", "/profiles")


async def meta_network() -> dict:
    """서비스 가능한 공간 범위(bbox) 등 네트워크 메타."""
    return await _call("GET", "/meta/network")


# ── 관광 ──
async def tour_spots(sigungu: str = "안양", limit: int = 20) -> dict:
    return await _call("GET", "/tour/bf-spots", params={"sigungu": sigungu, "limit": limit})


async def tour_detail(poi_id: str) -> dict:
    return await _call("GET", "/tour/bf-spots/%s" % poi_id)


async def tour_recommend(disabilities: list, sigungu: str = "안양",
                         match_mode: str = "all", topk: int = 10,
                         origin_lat: float = None, origin_lng: float = None,
                         offset: int = 0) -> dict:
    # origin 을 주면 02 route-api(v1.9.0+)가 거리 오름차순 + offset 페이징으로 응답한다.
    body = {"disabilities": disabilities, "sigungu": sigungu,
            "match_mode": match_mode, "topk": topk, "offset": offset}
    if origin_lat is not None and origin_lng is not None:
        body["origin_lat"] = origin_lat
        body["origin_lng"] = origin_lng
    return await _call("POST", "/tour/recommend", json=body)


# ── 대중교통 접근점 ──
async def transit_access(lat: float, lng: float, radius_m: float = 800,
                         profile: str = "wheelchair_manual") -> dict:
    return await _call(
        "GET", "/transit/access-points",
        params={"lat": lat, "lng": lng, "radius_m": radius_m, "profile": profile},
    )
