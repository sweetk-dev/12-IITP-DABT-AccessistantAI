# -*- coding: utf-8 -*-
"""음식점·건물 편의시설·기관 명부 도구 회귀 테스트 (v1.55.0, 02 v1.31.0).

    python3 tests/test_data_tools.py

계약:
  1) find_accessible_restaurants — 휠체어 출입 3상태를 그대로 싣고, 확인된 곳 수를 지침에 넣는다.
     확인된 곳이 없으면 "정보 없음 ≠ 못 감" 을 지침에 명시한다. 위치가 없어도 지역 전체로 찾는다
  2) 기준 장소를 말하면 그 좌표로 찾는다
  3) check_building_accessibility — 있음/없음/자료 없음을 나눠 싣고, 자료가 없으면 지어내지 말라고 지시
  4) find_service_providers / find_standard_workplaces — 기준일·전화 확인 안내
  5) find_toilet — 공공건물 화장실 표시가 카드에 실린다
  6) 네 도구 모두 디스패처·Live 선언에 있고, 현재 위치 주입 대상에 음식점·건물이 들어 있다
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

import route_client                                        # noqa: E402
import tool_handlers                                       # noqa: E402
from nav_context import inject_nav_defaults                # noqa: E402

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


def _patch(attr, fn):
    orig = getattr(route_client, attr)
    setattr(route_client, attr, fn)
    return orig


FOOD = {"total": 12, "confirmed": 2, "unknown": 10, "items": [
    {"name": "경사로식당", "addr": "a", "dist_m": 220, "cuisine": "한식", "entry_status": "yes",
     "facilities": ["접근로·경사로", "장애인 화장실"], "record_type": "tour_listing", "lat": 37.39, "lng": 126.95,
     "toilet": {"status": "own", "nearby": None, "radius_m": 200}},
    {"name": "흥부가", "addr": "b", "dist_m": 400, "cuisine": "일반음식점", "entry_status": "yes",
     "facilities": ["접근로·경사로"], "record_type": "building_survey", "survey_note": "건물 단위",
     "lat": 37.391, "lng": 126.95,
     "toilet": {"status": "nearby", "radius_m": 200,
                "nearby": {"name": "공원 공중화장실", "dist_m": 44, "source": "PUBLIC_TOILET", "open_time": "24시간",
                           "lat": 37.3906, "lng": 126.95}}},
    {"name": "모르는식당", "addr": "c", "dist_m": 500, "entry_status": "unknown", "facilities": [],
     "record_type": "tour_listing", "lat": 37.392, "lng": 126.95},
]}


def t_restaurants_confirmed():
    calls = []

    async def fake(lat=None, lng=None, **kw):
        calls.append((lat, lng, kw))
        return FOOD
    orig = _patch("food_nearby", fake)
    try:
        r = _run(tool_handlers.tool_find_accessible_restaurants(lat=37.39, lng=126.95))
    finally:
        route_client.food_nearby = orig
    assert r["status"] == "success" and r["count"] == 3 and r["confirmed"] == 2
    assert [i["entry_label"] for i in r["items"]] == ["접근로·경사로 확인", "접근로·경사로 확인", "휠체어 정보 없음"]
    ai = r["ai_instruction"]
    assert "12곳 중 휠체어 정보가 확인된 곳은 2곳" in ai, ai
    assert "building_survey" in ai and "단정하지" in ai
    assert "toilet" in ai and "nearby" in ai                                  # v1.56.0 화장실 짝짓기 지침
    assert r["items"][0]["toilet"] == {"status": "own", "nearby": None}
    assert r["items"][1]["toilet"]["nearby"] == {"name": "공원 공중화장실", "dist_m": 44, "open_time": "24시간"}
    assert r["items"][2]["toilet"] is None                                    # 02 가 toilet 을 안 주면 None
    ua = r["ui_action"]
    assert ua["action"] == "show_restaurants" and len(ua["payload"]["items"]) == 3
    assert calls[0][:2] == (37.39, 126.95)


def t_restaurants_none_confirmed_and_no_location():
    async def fake(lat=None, lng=None, **kw):
        return {"total": 3, "confirmed": 0, "items": [FOOD["items"][2]]}
    orig = _patch("food_nearby", fake)
    try:
        r = _run(tool_handlers.tool_find_accessible_restaurants())
    finally:
        route_client.food_nearby = orig
    assert r["status"] == "success" and r["radius_m"] is None
    assert "확인된 음식점은 없다" in r["ai_instruction"]
    assert "못 가는 곳이 아닙니다" in r["ai_instruction"]
    assert "안양시 전체" in r["ai_instruction"]


def t_restaurants_place_resolved():
    async def place(q):
        return {"lat": 37.40, "lng": 126.92, "label": "안양역"}
    seen = []

    async def fake(lat=None, lng=None, **kw):
        seen.append((lat, lng))
        return FOOD
    orig_p = tool_handlers._resolve_place
    tool_handlers._resolve_place = place
    orig = _patch("food_nearby", fake)
    try:
        r = _run(tool_handlers.tool_find_accessible_restaurants(place="안양역", lat=1.0, lng=2.0))
    finally:
        route_client.food_nearby = orig
        tool_handlers._resolve_place = orig_p
    assert seen == [(37.40, 126.92)] and r["base_label"] == "안양역"


def t_building():
    async def fake(q="", lat=None, lng=None, **kw):
        return {"items": [{"name": "박달복합청사", "facl_type": "국가 또는 지자체 청사", "addr": "x",
                           "entry_status": "yes", "has": ["장애인 화장실"], "lacks": ["장애인 주차구역"],
                           "status": {"dis_toilet": "yes", "dis_parking": "no", "elevator": "unknown"},
                           "basis": {"dis_toilet": "text"}, "base_dt": "2026-09-22"}]}
    orig = _patch("facility_accessibility", fake)
    try:
        r = _run(tool_handlers.tool_check_building_accessibility(name="박달복합청사"))
    finally:
        route_client.facility_accessibility = orig
    it = r["items"][0]
    assert it["has"] == ["장애인 화장실"] and it["lacks"] == ["장애인 주차구역"]
    assert it["no_data"] == ["승강기"] and it["from_text"] == ["장애인 화장실"]
    assert "'없다'고 하지 마세요" in r["ai_instruction"]

    async def empty(**kw):
        return {"items": []}
    orig = _patch("facility_accessibility", empty)
    try:
        r = _run(tool_handlers.tool_check_building_accessibility(name="안양시청"))
    finally:
        route_client.facility_accessibility = orig
    assert r["count"] == 0 and "자료에 없다" in r["ai_instruction"] and "지어내지" in r["ai_instruction"]


def t_directory():
    async def prov(**kw):
        return {"total": 9, "base_date": "2025-09-23", "items": [
            {"name": "바름아동센터", "services": ["주간활동"], "addr": "만안구", "tel": "031-441-5095"}]}

    async def wp(**kw):
        return {"total": 1, "base_date": "2025-07-10", "items": [
            {"name": "㈜고운누리", "business": "카페", "addr": "a", "tel": "043-261-7376", "cert_date": "2021-11-22"}]}
    o1, o2 = _patch("service_providers", prov), _patch("std_workplaces", wp)
    try:
        r = _run(tool_handlers.tool_find_service_providers(service="주간활동"))
        w = _run(tool_handlers.tool_find_standard_workplaces(keyword="카페"))
    finally:
        route_client.service_providers, route_client.std_workplaces = o1, o2
    assert r["items"][0]["tel"] == "031-441-5095" and "2025-09-23 기준" in r["ai_instruction"]
    assert "행정복지센터" in r["ai_instruction"]
    assert w["items"][0]["business"] == "카페" and "1588-1519" in w["ai_instruction"]


def t_toilet_building_flag():
    async def fake(lat, lng, **kw):
        return {"items": [{"name": "만안구보건소 (건물 안 장애인화장실)", "type": "국가 또는 지자체 청사",
                           "dist_m": 100, "accessible": True, "open_time": "건물 운영시간 내",
                           "facility_toilet": True, "building_toilet": True, "lat": 1, "lng": 2}]}
    orig = _patch("toilet_nearby", fake)
    try:
        r = _run(tool_handlers.tool_find_toilet(lat=37.39, lng=126.95))
    finally:
        route_client.toilet_nearby = orig
    it = r["items"][0]
    assert it["building_toilet"] is True and it["facility_type"] == "국가 또는 지자체 청사"
    assert "building_toilet" in r["ai_instruction"]


def t_registered_and_injected():
    import inspect
    src = inspect.getsource(tool_handlers)
    for n in ("find_accessible_restaurants", "check_building_accessibility",
              "find_service_providers", "find_standard_workplaces"):
        assert '"%s": tool_%s' % (n, n) in src, n
    lb = (ROOT / "live_bridge.py").read_text(encoding="utf-8")
    for n in ("find_accessible_restaurants", "check_building_accessibility",
              "find_service_providers", "find_standard_workplaces"):
        assert 'name="%s"' % n in lb, n
    assert "`find_accessible_restaurants` 를 **먼저**" in lb
    loc = {"lat": 37.39, "lng": 126.95}
    a = inject_nav_defaults("find_accessible_restaurants", {}, {}, loc)
    assert a["lat"] == 37.39
    a = inject_nav_defaults("check_building_accessibility", {"name": "보건소"}, {}, loc)
    assert a["lng"] == 126.95
    a = inject_nav_defaults("find_accessible_restaurants", {"place": "안양역"}, {}, loc)
    assert "lat" not in a


if __name__ == "__main__":
    for nm, fn in sorted((k, v) for k, v in list(globals().items()) if k.startswith("t_")):
        check(nm, fn)
    print()
    if FAILS:
        print("FAILED: %d" % len(FAILS))
        sys.exit(1)
    print("all passed")
