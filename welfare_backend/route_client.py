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

# 경로 안내가 가능한 지역. 안내문에서 "왜 안 되는지" 를 정확히 말하기 위해 필요하다.
SERVICE_AREA = os.environ.get("ROUTE_SERVICE_AREA", "안양시")

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


# "일시적 장애" 는 서비스가 실제로 응답하지 못할 때(5xx·타임아웃·서킷 오픈)만 쓴다.
# 요청 자체가 처리 불가한 4xx 에까지 이 문구가 붙으면, 사용자는 "잠시 후 다시 하면 되겠지"
# 라고 오해한 채 영원히 되지 않는 요청을 반복하게 된다.
_AI_TRANSIENT = (
    "경로 안내 서비스에 지금 연결할 수 없다고 사용자에게 짧게 알리고, 잠시 후 다시 시도해 "
    "달라고 안내하세요. 정책 상담은 계속 이용 가능하다고 덧붙이세요. 경로를 추측해서 만들어내지 마세요."
)
_AI_NEUTRAL = (
    "경로를 만들지 못한 이유를 사용자에게 그대로 짧게 전달하세요. "
    "일시적인 오류라고 말하지 말고, 경로를 추측해서 만들어내지 마세요."
)


def _ai_for_4xx(detail: str) -> str:
    """4xx 사유별 안내문. 4xx 는 서비스 장애가 아니라 요청 자체의 문제다."""
    d = str(detail or "")

    # 목적지를 찾지 못함 — 서비스 범위 밖이거나 아직 등록되지 않은 장소
    if ("목적지 POI" in d) or ("poi_id" in d) or ("관광지를 찾을 수 없" in d) or ("출입구 정보" in d):
        return (
            "요청하신 목적지는 경로 안내가 가능한 지역(%s) 밖이거나 아직 등록되지 않은 장소라 "
            "길을 안내할 수 없다고 정확히 안내하세요. 현재 경로 안내는 %s 안에서만 가능하다는 점을 "
            "분명히 밝히고, %s 안의 장소를 말씀해 주시거나 이동·관광 화면에서 목적지를 골라 달라고 "
            "요청하세요. 서비스 장애나 일시적인 오류라고 말하지 말고, 경로를 추측하지 마세요."
            % (SERVICE_AREA, SERVICE_AREA, SERVICE_AREA)
        )

    # 출발지가 보행망에서 너무 멀다 — 대개 현재 위치가 서비스 지역 밖
    if "떨어져" in d:
        return (
            "출발지(또는 현재 위치)가 경로 안내가 가능한 지역(%s)의 보행 데이터 범위를 벗어나 "
            "경로를 만들 수 없다고 정확히 안내하세요. %s 안의 출발지 이름(예: 안양역)을 말씀해 "
            "주시거나 이동·관광 화면의 지도를 눌러 출발지를 지정하면 안내할 수 있다고 덧붙이세요. "
            "일시적인 오류라고 말하지 말고, 경로를 추측하지 마세요."
            % (SERVICE_AREA, SERVICE_AREA)
        )

    # 안내했던 경로가 만료됨 — 다시 요청하면 된다
    if "만료" in d:
        return (
            "이전에 안내한 경로 정보가 만료되었다고 알리고, 목적지를 다시 말씀해 주시면 "
            "새로 안내해 드리겠다고 요청하세요. 일시적인 오류라고 말하지 말고, 경로를 추측하지 마세요."
        )

    # 통행 가능한 경로 자체가 없음 — 접근성 제약 때문일 수 있다
    if ("경로를 찾지 못" in d) or ("같은 지점" in d) or ("통행 가능한" in d):
        return (
            "출발지에서 목적지까지 이동 수단(휠체어 등)으로 통행 가능한 보행 경로를 찾지 못했다고 "
            "안내하고, 출발지나 목적지를 조금 바꿔 다시 시도해 보시라고 제안하세요. "
            "일시적인 오류라고 말하지 말고, 경로를 추측하지 마세요."
        )

    return _AI_NEUTRAL


def _err(message: str, detail: str = "", ai_instruction: str = None) -> dict:
    return {
        "status": "error",
        "message": message,
        "detail": detail,
        # 기본값은 중립 문구다. "일시적" 은 진짜 연결 실패 지점에서만 명시적으로 붙인다.
        "ai_instruction": ai_instruction or _AI_NEUTRAL,
    }


async def _call(method: str, path: str, *, params: Optional[dict] = None,
                json: Optional[dict] = None) -> dict:
    if not BASE_URL:
        return _err("경로 서비스가 설정되지 않았습니다", ai_instruction=_AI_TRANSIENT)
    if _circuit_open():
        return _err("경로 서비스가 일시적으로 응답하지 않습니다",
                    ai_instruction=_AI_TRANSIENT)

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
                # 오해를 낳지 않도록 사유별 안내문을 함께 전달한다.
                logger.info("경로 API 4xx %s %s — %s", method, path, detail_msg)
                return _err(detail_msg, "HTTP %d" % r.status_code,
                            _ai_for_4xx(detail_msg))
            return r.json()
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_detail = "%s: %s" % (type(e).__name__, e)
            continue

    _record(False)
    logger.warning("경로 API 호출 실패 %s %s — %s", method, path, last_detail)
    return _err("경로 서비스에 연결하지 못했습니다", last_detail, _AI_TRANSIENT)


# ── 경로 ──
async def plan_route(origin: dict, destination: dict, profile: str = "wheelchair_manual",
                     alternatives: int = 1, mode: str = "") -> dict:
    body = {"origin": origin, "destination": destination,
            "profile": profile, "alternatives": alternatives}
    # 02 v1.12.0 멀티모달(#36) — walk 은 기존 계약이므로 생략해 하위 서버와도 호환 유지
    if mode in ("walk_bus", "walk_bus_subway"):
        body["mode"] = mode
    return await _call("POST", "/route/plan", json=body)


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


# ── 수집 장치화 (02 v1.16.0 — 트랙·접근성 제보) ──
async def log_track(payload: dict) -> dict:
    """주행 GPS 트랙 배치 업로드 패스스루. 참여자 식별자는 넣지 않는다(route_id 익명)."""
    return await _call("POST", "/track/log", json=payload)


async def report_accessibility(payload: dict) -> dict:
    """접근성 오류 제보 패스스루 — 접수 즉시 해당 지점에 '이용자 제보' 경고가 붙는다."""
    return await _call("POST", "/report/accessibility", json=payload)


async def list_access_reports(status: str = "", limit: int = 50, offset: int = 0) -> dict:
    params = {"limit": limit, "offset": offset}
    if status:
        params["status"] = status
    return await _call("GET", "/report/accessibility", params=params)


async def review_access_report(report_id: int, payload: dict) -> dict:
    return await _call("PATCH", "/report/accessibility/%d" % int(report_id), json=payload)


async def get_report_photo(report_id: int):
    """제보 사진 바이트 (bytes, mime) — 없으면 (None, None)."""
    if not BASE_URL:
        return None, None
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SEC) as client:
            r = await client.get("%s/report/accessibility/%d/photo" % (BASE_URL, int(report_id)),
                                 headers=_headers())
        if r.status_code == 200:
            return r.content, r.headers.get("content-type", "image/jpeg")
    except (httpx.TimeoutException, httpx.TransportError):
        pass
    return None, None
