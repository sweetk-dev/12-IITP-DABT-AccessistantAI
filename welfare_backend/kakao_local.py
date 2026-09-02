# -*- coding: utf-8 -*-
"""카카오 로컬(장소·주소 검색) 클라이언트 — 서버 도구 경로용 (v1.42.0).

경로 서비스(02 `/poi/search`)는 무장애 관광지·지하철역·OSM 건물 이름만 안다. 이용자가 말하는
목적지에는 기관명("국민건강보험공단 안양지사")과 도로명주소("관평로 182")가 흔한데 둘 다
거기 없어 `place_not_found` 로 끝났다(2026-09-02 실측 7건). 화면의 장소 검색창에는 이미 카카오
장소검색(JS SDK)이 폴백으로 붙어 있었으므로, 같은 출처를 음성 도구 경로에도 붙인다.

- 키워드: GET https://dapi.kakao.com/v2/local/search/keyword.json  (place_name·도로명주소·좌표)
- 주소:   GET https://dapi.kakao.com/v2/local/search/address.json  (도로명·지번 → 좌표)
인증은 REST API 키(`KAKAO_REST_API_KEY`, 헤더 `Authorization: KakaoAK …`) — JS 키와 다르다.
키가 없으면 조용히 빈 결과를 돌려준다(기능 플래그 없이도 기존 동작 유지).

전국 검색이라 "안양지사"를 찾는데 서울 지점이 먼저 나오는 일이 있다. 그래서 키워드 검색은
안양시청 반경 20km 거리순으로 부르고, 결과마다 서비스 지역(bbox) 안인지를 ``in_service_area``
로 붙여 돌려준다 — 호출부는 지역 안 결과를 우선 쓰고, 밖의 결과는 "범위 밖" 안내의 근거로
쓴다. bbox 를 모르면 전부 안으로 본다(범위를 모르면서 '밖'이라고 단정하지 않는다).
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger("kakao_local")

REST_KEY = os.environ.get("KAKAO_REST_API_KEY", "")
BASE_URL = "https://dapi.kakao.com/v2/local/search"
TIMEOUT_SEC = float(os.environ.get("KAKAO_LOCAL_TIMEOUT", "4"))
# 안양시청 — 키워드 검색의 거리 정렬 중심(반경 20km 안에서 가까운 순)
DEFAULT_CENTER = (37.3943, 126.9568)


def enabled() -> bool:
    return bool(REST_KEY)


def _in_bbox(lat: float, lng: float, bbox: Optional[dict]) -> bool:
    if not bbox or bbox.get("min_lat") is None:
        return True
    return (bbox["min_lat"] <= lat <= bbox["max_lat"]
            and bbox["min_lng"] <= lng <= bbox["max_lng"])


def _norm_keyword(doc: dict) -> Optional[dict]:
    try:
        lat, lng = float(doc.get("y")), float(doc.get("x"))
    except (TypeError, ValueError):
        return None
    return {
        "name": doc.get("place_name") or "",
        "addr": doc.get("road_address_name") or doc.get("address_name") or "",
        "category": doc.get("category_group_name") or doc.get("category_name") or "",
        "lat": lat, "lng": lng,
        "source": "kakao_keyword",
    }


def _norm_address(doc: dict) -> Optional[dict]:
    try:
        lat, lng = float(doc.get("y")), float(doc.get("x"))
    except (TypeError, ValueError):
        return None
    road = doc.get("road_address") or {}
    name = (road.get("building_name") or "").strip() or doc.get("address_name") or ""
    return {
        "name": name,
        "addr": doc.get("address_name") or road.get("address_name") or "",
        "category": "주소",
        "lat": lat, "lng": lng,
        "source": "kakao_address",
    }


async def _get(path: str, params: dict) -> list:
    if not enabled():
        return []
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SEC) as client:
            r = await client.get(BASE_URL + path, params=params,
                                 headers={"Authorization": "KakaoAK %s" % REST_KEY})
        if r.status_code != 200:
            logger.warning("카카오 로컬 %s HTTP %d: %s", path, r.status_code, r.text[:200])
            return []
        return (r.json() or {}).get("documents") or []
    except (httpx.TimeoutException, httpx.TransportError, ValueError) as e:
        logger.warning("카카오 로컬 %s 실패: %s", path, e)
        return []


def _squash(s: str) -> str:
    return "".join((s or "").split()).lower()


def _tag(items: list, bbox: Optional[dict], q: str = "") -> list:
    """서비스 지역 판정을 붙이고 정렬한다: 지역 안 → 이름이 질의와 같음 → 질의로 시작 →
    이름이 짧은 순(거리순은 그 안에서 유지).

    거리순 그대로 두면 "국민건강보험공단 안양지사"에 "무인민원발급창구 국민건강보험공단
    안양지사"(같은 건물의 부속 시설)가 먼저 나온다(실측). 이용자가 말한 이름 그대로인
    항목을 앞세운다.
    """
    qs = _squash(q)
    out = []
    for it in items:
        if not it:
            continue
        it["in_service_area"] = _in_bbox(it["lat"], it["lng"], bbox)
        out.append(it)

    def key(x):
        nm = _squash(x.get("name"))
        return (0 if x["in_service_area"] else 1,
                0 if qs and nm == qs else 1,
                0 if qs and nm.startswith(qs) else 1,
                len(nm))
    out.sort(key=key)
    return out


async def search(q: str, bbox: Optional[dict] = None, limit: int = 5,
                 center: tuple = DEFAULT_CENTER) -> list:
    """키워드 → (없으면) 주소 순으로 찾는다. 항목: name·addr·category·lat·lng·source·in_service_area."""
    q = (q or "").strip()
    if len(q) < 2 or not enabled():
        return []
    docs = await _get("/keyword.json", {"query": q, "size": 10, "y": center[0], "x": center[1],
                                         "radius": 20000, "sort": "distance"})
    out = _tag([_norm_keyword(d) for d in docs], bbox, q)
    if not out:
        docs = await _get("/address.json", {"query": q, "size": 10})
        out = _tag([_norm_address(d) for d in docs], bbox, q)
    return out[:limit]
