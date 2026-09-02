# -*- coding: utf-8 -*-
"""장소 해석 카카오 로컬 폴백 회귀 테스트 (v1.42.0).

    python3 tests/test_place_geocode.py

배경: "국민건강보험공단 안양지사", "관평로 182" 처럼 경로 서비스(02)가 모르는 기관명·
도로명주소가 목적지로 오면 place_not_found 로 끝났다. 화면 검색창의 카카오 폴백을 음성 도구
경로에도 붙였다.

계약:
  1) 02 지역 안 검색이 비면 카카오 로컬을 부르고, 서비스 지역 안 결과를 building 으로 쓴다
  2) 02 가 먼저 찾으면 카카오를 부르지 않는다 (호출 순서·비용)
  3) 카카오 결과가 전부 지역 밖이면 02 범위 확대 검색을 먼저 보고, 그래도 없을 때 카카오
     지역 밖 결과를 in_service_area=False 로 돌려 "범위 밖" 안내가 된다
  4) 카카오 키가 없으면(search 가 []) 기존 동작 그대로 place_not_found
  5) 카카오 호출 예외는 삼키고 다음 단계로 간다
  6) kakao_local 정규화: 키워드/주소 응답 → name·addr·lat·lng·in_service_area, 지역 안 우선 정렬
"""
import asyncio
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ROUTE_API_BASE_URL", "http://route-api:18100")
os.environ.setdefault("FEATURE_ROUTE", "1")

for name, attrs in (
    ("sqlalchemy", {"select": lambda *a, **k: None, "or_": lambda *a, **k: None}),
    ("sqlalchemy.ext", {}),
    ("sqlalchemy.ext.asyncio", {"AsyncSession": object}),
    ("database", {"AsyncSessionLocal": None}),
    ("models", {}),
):
    if name not in sys.modules:
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m

import kakao_local                                         # noqa: E402
import route_client                                        # noqa: E402
import tool_handlers                                       # noqa: E402

FAILS = []


def check(name, fn):
    try:
        fn()
        print("  PASS  %s" % name)
    except AssertionError as e:
        FAILS.append(name)
        print("  FAIL  %s — %s" % (name, e))


def _run(coro):
    return asyncio.run(coro)


_BBOX = {"min_lat": 37.36, "min_lng": 126.88, "max_lat": 37.45, "max_lng": 127.00}
_SEARCH = {
    "안양시청": {"count": 1, "items": [
        {"type": "building", "name": "안양시청", "lat": 37.39429, "lng": 126.95685}]},
}
_SEARCH_WIDE = {
    "관악장애인종합복지관": {"count": 1, "items": [
        {"type": "tour", "poi_id": "SEOUL-1", "name": "관악장애인종합복지관",
         "lat": 37.4784, "lng": 126.9516, "in_service_area": False}]},
}
_KAKAO = {
    "국민건강보험공단 안양지사": [
        {"name": "국민건강보험공단 안양지사", "addr": "경기 안양시 동안구 시민대로 235",
         "category": "공공기관", "lat": 37.3910, "lng": 126.9520, "source": "kakao_keyword",
         "in_service_area": True}],
    "관평로 182": [
        {"name": "관평로 182", "addr": "경기 안양시 동안구 관평로 182", "category": "주소",
         "lat": 37.3985, "lng": 126.9640, "source": "kakao_address", "in_service_area": True}],
    "국민건강보험공단 강남지사": [
        {"name": "국민건강보험공단 강남지사", "addr": "서울 강남구 테헤란로 1",
         "category": "공공기관", "lat": 37.4980, "lng": 127.0280, "source": "kakao_keyword",
         "in_service_area": False}],
}


class _Spy(object):
    def __init__(self, kakao_raises=False):
        self.plan_called = 0
        self.search_calls = []
        self.kakao_calls = []
        self.kakao_raises = kakao_raises
        self.last = None

    async def tour_spots(self, sigungu="안양", limit=60):
        return {"count": 0, "items": []}

    async def poi_search(self, q, sigungu="안양", limit=8, include_outside=False):
        self.search_calls.append((q.strip(), include_outside))
        key = q.strip()
        if include_outside and key in _SEARCH_WIDE:
            return dict(_SEARCH_WIDE[key])
        if include_outside:
            return {"count": 0, "items": []}
        return dict(_SEARCH.get(key, {"count": 0, "items": []}))

    async def meta_network(self):
        return {"region": "안양시", "bbox": _BBOX}

    async def kakao_search(self, q, bbox=None, limit=5, center=None):
        self.kakao_calls.append((q.strip(), bbox))
        if self.kakao_raises:
            raise RuntimeError("boom")
        return [dict(x) for x in _KAKAO.get(q.strip(), [])]

    async def plan_route(self, origin, destination, profile="wheelchair_manual",
                         alternatives=1, mode="", realtime=False):
        self.plan_called += 1
        self.last = (origin, destination)
        return {"route_id": "r_x", "routes": [{"summary": {"total_distance_m": 100,
                "duration_sec": 120, "max_slope_deg": 1.0, "stairs_cnt": 0,
                "crossing_cnt": 1, "warnings": []}, "steps": [{"instruction": "직진"}]}]}


def _with_spy(fn, **kw):
    spy = _Spy(**kw)
    orig_rc, orig_kk = tool_handlers.route_client, tool_handlers.kakao_local
    tool_handlers.route_client = types.SimpleNamespace(
        tour_spots=spy.tour_spots, plan_route=spy.plan_route, poi_search=spy.poi_search,
        meta_network=spy.meta_network, SERVICE_AREA=route_client.SERVICE_AREA)
    tool_handlers.kakao_local = types.SimpleNamespace(search=spy.kakao_search)
    tool_handlers._SERVICE_BBOX["value"] = None
    tool_handlers._SERVICE_BBOX["checked"] = False
    try:
        return fn(spy)
    finally:
        tool_handlers.route_client, tool_handlers.kakao_local = orig_rc, orig_kk
        tool_handlers._SERVICE_BBOX["value"] = None
        tool_handlers._SERVICE_BBOX["checked"] = False


def t_institution_name_resolves_via_kakao():
    def body(spy):
        r = _run(tool_handlers.tool_plan_accessible_route(
            destination_place="국민건강보험공단 안양지사", origin_place="안양역"))
        assert r["status"] == "success", r
        assert spy.kakao_calls and spy.kakao_calls[0][1] == _BBOX, "bbox 없이 카카오를 부름: %s" % spy.kakao_calls
        assert spy.last[1]["type"] == "building", spy.last
        assert abs(spy.last[1]["lat"] - 37.3910) < 1e-6, spy.last
        assert r["destination_label"] == "국민건강보험공단 안양지사", r.get("destination_label")
        assert ("국민건강보험공단 안양지사", True) not in spy.search_calls, "지역 안에서 찾았는데 범위 확대 검색을 함"
    _with_spy(body)


def t_road_address_resolves_via_kakao():
    def body(spy):
        r = _run(tool_handlers.tool_plan_accessible_route(
            destination_place="관평로 182", origin_place="안양역"))
        assert r["status"] == "success", r
        assert abs(spy.last[1]["lng"] - 126.9640) < 1e-6, spy.last
    _with_spy(body)


def t_route_hit_skips_kakao():
    def body(spy):
        r = _run(tool_handlers.tool_plan_accessible_route(
            destination_place="안양시청", origin_place="안양역"))
        assert r["status"] == "success", r
        assert spy.kakao_calls == [], "02 가 찾았는데 카카오를 부름: %s" % spy.kakao_calls
    _with_spy(body)


def t_kakao_outside_only_becomes_out_of_area():
    def body(spy):
        r = _run(tool_handlers.tool_plan_accessible_route(
            destination_place="국민건강보험공단 강남지사", origin_place="안양역"))
        assert r["status"] == "out_of_service_area", r
        assert spy.plan_called == 0
        # 02 범위 확대 검색을 먼저 봤다
        assert ("국민건강보험공단 강남지사", True) in spy.search_calls, spy.search_calls
    _with_spy(body)


def t_route_wide_hit_beats_kakao_outside():
    """02 범위 확대 결과(관광지)가 있으면 카카오 지역 밖 결과보다 앞선다."""
    def body(spy):
        r = _run(tool_handlers.tool_plan_accessible_route(
            destination_place="관악장애인종합복지관", origin_place="안양역"))
        assert r["status"] == "out_of_service_area", r
    _with_spy(body)


def t_no_key_keeps_place_not_found():
    def body(spy):
        r = _run(tool_handlers.tool_plan_accessible_route(
            destination_place="없는이름복지관", origin_place="안양역"))
        assert r["status"] == "place_not_found", r
        assert spy.kakao_calls, "카카오를 시도조차 하지 않음"
    _with_spy(body)


def t_kakao_error_is_swallowed():
    def body(spy):
        r = _run(tool_handlers.tool_plan_accessible_route(
            destination_place="없는이름복지관", origin_place="안양역"))
        assert r["status"] == "place_not_found", r
        assert ("없는이름복지관", True) in spy.search_calls, "카카오 예외 뒤 범위 확대 검색을 건너뜀"
    _with_spy(body, kakao_raises=True)


def t_kakao_local_disabled_without_key():
    orig = kakao_local.REST_KEY
    kakao_local.REST_KEY = ""
    try:
        assert kakao_local.enabled() is False
        assert _run(kakao_local.search("안양시청", bbox=_BBOX)) == []
    finally:
        kakao_local.REST_KEY = orig


def t_kakao_local_normalizes_and_orders():
    orig_key, orig_get = kakao_local.REST_KEY, kakao_local._get
    calls = []

    async def fake_get(path, params):
        calls.append((path, dict(params)))
        if path == "/keyword.json":
            return [
                {"place_name": "국민건강보험공단 강남지사", "road_address_name": "서울 강남구 테헤란로 1",
                 "category_group_name": "공공기관", "x": "127.0280", "y": "37.4980"},
                {"place_name": "국민건강보험공단 안양지사", "road_address_name": "경기 안양시 동안구 시민대로 235",
                 "category_group_name": "공공기관", "x": "126.9520", "y": "37.3910"},
                {"place_name": "좌표없음", "x": "", "y": ""},
            ]
        return []

    kakao_local.REST_KEY = "k"
    kakao_local._get = fake_get
    try:
        out = _run(kakao_local.search("국민건강보험공단", bbox=_BBOX))
        assert [o["name"] for o in out] == ["국민건강보험공단 안양지사", "국민건강보험공단 강남지사"], out
        assert out[0]["in_service_area"] is True and out[1]["in_service_area"] is False
        assert out[0]["addr"] == "경기 안양시 동안구 시민대로 235" and out[0]["source"] == "kakao_keyword"
        assert calls[0][1]["sort"] == "distance" and calls[0][1]["radius"] == 20000
        assert len(calls) == 1, "키워드로 찾았는데 주소 검색까지 함"
    finally:
        kakao_local.REST_KEY, kakao_local._get = orig_key, orig_get


def t_kakao_local_address_fallback():
    orig_key, orig_get = kakao_local.REST_KEY, kakao_local._get

    async def fake_get(path, params):
        if path == "/keyword.json":
            return []
        return [{"address_name": "경기 안양시 동안구 관평로 182", "x": "126.9640", "y": "37.3985",
                 "road_address": {"building_name": "", "address_name": "경기 안양시 동안구 관평로 182"}}]

    kakao_local.REST_KEY = "k"
    kakao_local._get = fake_get
    try:
        out = _run(kakao_local.search("관평로 182", bbox=_BBOX))
        assert len(out) == 1 and out[0]["source"] == "kakao_address", out
        assert out[0]["name"] == "경기 안양시 동안구 관평로 182" and out[0]["category"] == "주소"
        assert out[0]["in_service_area"] is True
        assert _run(kakao_local.search("x", bbox=_BBOX)) == [], "한 글자 질의를 호출함"
    finally:
        kakao_local.REST_KEY, kakao_local._get = orig_key, orig_get


if __name__ == "__main__":
    print("장소 해석 카카오 로컬 폴백 회귀 테스트")
    for nm, fn in sorted((k[2:], v) for k, v in list(globals().items())
                         if k.startswith("t_") and callable(v)):
        check(nm, fn)
    print("")
    if FAILS:
        print("FAILED %d" % len(FAILS))
        sys.exit(1)
    print("ALL PASSED")
