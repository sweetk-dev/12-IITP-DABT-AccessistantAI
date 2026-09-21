# -*- coding: utf-8 -*-
"""긴급대응·화장실 도구 회귀 테스트 (v1.49.0, #296 · 02 v1.26.0).

    python3 tests/test_support_tools.py

계약:
  1) find_emergency_support — 상황 문구로 유형을 고른다(배터리→충전, 고장→수리, 택시→콜택시), 명시 types 우선
  2) 결과 카드에 운영시간 미상(unknown)이 그대로 남고, 지침이 "지어내지 말라"를 담는다
  3) 반경 안에 없는 유형은 지침에 "없다"로 명시된다
  4) ui_action(show_support / show_toilets)에 카드·기준 좌표가 실린다
  5) find_toilet — 접근 가능 화장실만, 없으면 역 화장실 안내로 유도
  6) 현재 위치 주입(inject_nav_defaults) — place 가 없으면 lat/lng, 있으면 주입하지 않는다
  7) 위치도 장소도 없으면 need_location
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


SUPPORT = {"source": "db", "radius_m": 2000, "count": 3, "items": [
    {"support_type": "charge", "type_label": "전동보장구 충전기", "name": "안양시청", "install_desc": "본관 1층",
     "addr": "시민대로 235", "dist_m": 320, "tel": "031-8045-0000", "open_hours": "09:00~18:00",
     "open_hours_status": "known", "source_label": "행정안전부 표준데이터", "confidence": "H",
     "coord_suspect": False, "lat": 37.3943, "lng": 126.9568},
    {"support_type": "repair", "type_label": "보장구 수리", "name": "테스트 수리센터", "addr": "만안구 1",
     "dist_m": 900, "tel": "031-111-1111", "open_hours": None, "open_hours_status": "unknown",
     "source_label": "경기도 보조기기 수리 지정업체", "confidence": "H", "coord_suspect": True,
     "lat": 37.39, "lng": 126.95},
    {"support_type": "calltaxi", "type_label": "장애인콜택시", "name": "경기도 광역이동지원센터",
     "dist_m": 1500, "tel": "1666-0420", "open_hours": "24시간", "open_hours_status": "known",
     "source_label": "수기 등록", "lat": 37.39, "lng": 126.95},
]}


class _Rec:
    def __init__(self, resp):
        self.resp, self.calls = resp, []

    async def __call__(self, lat, lng, types="", radius_m=2000, limit=3, **kw):
        self.calls.append({"lat": lat, "lng": lng, "types": types, "radius_m": radius_m})
        return self.resp


def _support(rec, **kw):
    orig = route_client.support_nearby
    route_client.support_nearby = rec
    try:
        return _run(tool_handlers.tool_find_emergency_support(**kw))
    finally:
        route_client.support_nearby = orig


def t_situation_picks_types():
    f = tool_handlers._support_types_from_situation
    assert f("", "배터리가 10%밖에 안 남았어요") == "charge"
    assert f("", "휠체어 바퀴가 이상해요 고장난 것 같아요") == "repair"
    assert f("", "콜택시 불러줘") == "calltaxi"
    assert f("", "배터리도 없고 바퀴도 이상해") == "charge,repair"
    assert f("", "그냥 근처 알려줘") == "charge,repair,calltaxi"
    assert f("repair", "배터리") == "repair", "명시 types 가 우선"
    assert f("bus", "배터리") == "charge", "모르는 유형은 무시하고 상황으로"


def t_support_cards_and_instruction():
    rec = _Rec(SUPPORT)
    r = _support(rec, lat=37.39, lng=126.95, situation="배터리가 다 됐어요 바퀴도 이상하고 택시도")
    assert r["status"] == "success" and r["count"] == 3, r
    assert rec.calls[0]["types"] == "charge,repair,calltaxi"
    rp = [i for i in r["items"] if i["support_type"] == "repair"][0]
    assert rp["open_hours"] is None and rp["open_hours_status"] == "unknown"
    assert rp["coord_suspect"] is True and rp["type_label"] == "수리센터"
    assert "지어내지" in r["ai_instruction"] and "전화" in r["ai_instruction"]
    assert "콜택시를 함께 권하세요" in r["ai_instruction"]
    ua = r["ui_action"]
    assert ua["action"] == "show_support" and len(ua["payload"]["items"]) == 3
    assert ua["payload"]["base"] == {"lat": 37.39, "lng": 126.95}
    assert r["count_by_type"] == {"charge": 1, "repair": 1, "calltaxi": 1}


def t_missing_type_is_said():
    only_charge = dict(SUPPORT, items=[SUPPORT["items"][0]])
    r = _support(_Rec(only_charge), lat=37.39, lng=126.95, types="charge,repair")
    assert "수리센터 은(는) 반경 2000m 안에 없다" in r["ai_instruction"], r["ai_instruction"]


def t_support_error_passthrough_and_need_location():
    async def err(*a, **k):
        return {"status": "error", "message": "경로 서비스에 연결하지 못했습니다", "ai_instruction": "x"}
    orig = route_client.support_nearby
    route_client.support_nearby = err
    try:
        r = _run(tool_handlers.tool_find_emergency_support(lat=37.39, lng=126.95))
        assert r["status"] == "error"
    finally:
        route_client.support_nearby = orig
    r = _run(tool_handlers.tool_find_emergency_support())
    assert r["status"] == "need_location" and r["tool_name"] == "find_emergency_support"


TOILETS = {"source": "db", "count": 2, "items": [
    {"name": "시청 화장실", "type": "공중화장실", "addr": "a", "dist_m": 120, "accessible": True,
     "dis_male_cnt": 1, "dis_female_cnt": 1, "unisex": False, "open_time": "24시간",
     "emg_bell": True, "tel": None, "lat": 37.3904, "lng": 126.9505},
    {"name": "공원 화장실", "type": "공중화장실", "addr": "b", "dist_m": 400, "accessible": True,
     "dis_male_cnt": 0, "dis_female_cnt": 1, "unisex": True, "open_time": None,
     "emg_bell": False, "lat": 37.391, "lng": 126.951},
]}


def _toilet(resp, **kw):
    async def fake(lat, lng, radius_m=800, limit=5, accessible_only=True):
        return resp
    orig = route_client.toilet_nearby
    route_client.toilet_nearby = fake
    try:
        return _run(tool_handlers.tool_find_toilet(**kw))
    finally:
        route_client.toilet_nearby = orig


def t_toilet_cards():
    r = _toilet(TOILETS, lat=37.39, lng=126.95)
    assert r["status"] == "success" and r["count"] == 2
    assert r["items"][0]["name"] == "시청 화장실" and r["items"][0]["emg_bell"] is True
    assert r["ui_action"]["action"] == "show_toilets"
    assert "개방시간" in r["ai_instruction"]


def t_toilet_none_points_to_station():
    r = _toilet({"count": 0, "items": []}, lat=37.39, lng=126.95)
    assert r["count"] == 0 and "get_station_facilities" in r["ai_instruction"]
    assert "없다고 분명히" in r["ai_instruction"]


def t_location_injection():
    loc = {"lat": 37.39, "lng": 126.95}
    for fn in ("find_emergency_support", "find_toilet"):
        a = inject_nav_defaults(fn, {}, {}, loc)
        assert a == {"lat": 37.39, "lng": 126.95}, (fn, a)
        a = inject_nav_defaults(fn, {"place": "안양역", "lat": 1, "lng": 2}, {}, loc)
        assert a == {"place": "안양역", "lat": 1, "lng": 2}, "place 가 있으면 주입하지 않는다"
        a = inject_nav_defaults(fn, {"lat": 1, "lng": 2}, {}, None)
        assert "lat" not in a, "위치를 모르면 모델이 준 좌표도 버린다"


def t_tools_registered():
    tools = tool_handlers.get_tool_handlers(embed_fn=None) if hasattr(tool_handlers, "get_tool_handlers") else None
    if tools is None:
        import inspect
        src = inspect.getsource(tool_handlers)
        assert '"find_emergency_support": tool_find_emergency_support' in src
        assert '"find_toilet": tool_find_toilet' in src
    else:
        assert "find_emergency_support" in tools and "find_toilet" in tools


if __name__ == "__main__":
    for nm, fn in sorted((k, v) for k, v in list(globals().items()) if k.startswith("t_")):
        check(nm, fn)
    print()
    if FAILS:
        print("FAILED: %d" % len(FAILS))
        sys.exit(1)
    print("all passed")
