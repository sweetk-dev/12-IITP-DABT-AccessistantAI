# -*- coding: utf-8 -*-
"""문화·체육 시설 도구 회귀 테스트 (v2.4.0 · 02 v1.36.0).

    python3 tests/test_culture_tools.py

계약:
  1) find_culture_sports — 명시 types 우선, 없으면 사용자 말(query)로 종류를 고르고, 아무것도 안 걸리면 4종 전부
     · '도서관'만으로는 점자도서관을 고르지 않는다(점자·시각장애 표현이 있을 때만)
  2) 반경 밖 결과(beyond_radius)가 카드에 그대로 남고, 지침이 "근처에는 없고 가장 가까운 곳"을 구분하라고 한다
  3) 책나래·체육회 설명 지침, 길안내 지역 밖(in_service_area=false)이면 전화 문의 지침
  4) 결과 0건이면 "없다 + 경기도 자료" 지침, 지어내지 말 것
  5) ui_action(show_culture)에 카드·기준 좌표, 반경은 500~20000 으로 제한
  6) 현재 위치 주입 대상에 포함, 위치도 장소도 없으면 need_location
  7) 도구 등록(디스패처·실시간 선언·폴백 선언)
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


CULTURE = {"source": "db", "radius_m": 5000, "count": 4,
           "region_note": "경기도 자료입니다. 경기도 밖(서울 등)의 시설은 포함되지 않습니다.",
           "items": [
    {"facility_id": 10, "facility_type": "braille_lib", "type_label": "점자도서관",
     "type_desc": "점자·음성(녹음) 도서를 빌려 주는 시각장애인 도서관", "name": "경기도시각장애인도서관",
     "addr": "경기도 수원시 팔달구 수성로157번길 27-4", "tel": "031-213-5045", "homepage": "lib.eyes1004.com",
     "dist_m": 14800, "beyond_radius": True, "in_service_area": False, "lat": 37.287, "lng": 127.006,
     "source_label": "경기데이터드림(경기도 장애인복지종합지원센터)"},
    {"facility_id": 1, "facility_type": "booknarae_lib", "type_label": "책나래 신청 도서관",
     "type_desc": "장애인이 책을 무료 택배로 빌려 보는 '책나래' 서비스를 신청할 수 있는 도서관",
     "name": "안양시립석수도서관", "addr": "경기도 안양시 만안구", "dist_m": 850, "beyond_radius": False,
     "in_service_area": True, "lat": 37.40, "lng": 126.92},
    {"facility_id": 20, "facility_type": "sports_fac", "name": "반다비체육센터", "addr": "경기도 안양시 만안구 냉천로 39",
     "dist_m": 1200, "beyond_radius": False, "in_service_area": True, "lat": 37.392, "lng": 126.945},
    {"facility_id": 31, "facility_type": "sports_org", "name": "안양시장애인체육회", "addr": "경기도 안양시 만안구",
     "dist_m": 2300, "beyond_radius": False, "lat": 37.393, "lng": 126.93},   # 구버전: in_service_area 없음
    {"facility_id": 99, "facility_type": "museum", "name": "모르는 종류"},
]}


def _culture(resp, **kw):
    seen = {}

    async def fake(lat, lng, types="", radius_m=5000, limit=3, fallback=True, fallback_limit=2):
        seen.update(lat=lat, lng=lng, types=types, radius_m=radius_m, fallback=fallback)
        return resp
    orig = route_client.culture_nearby
    route_client.culture_nearby = fake
    try:
        return _run(tool_handlers.tool_find_culture_sports(**kw)), seen
    finally:
        route_client.culture_nearby = orig


def t_type_selection():
    f = tool_handlers._culture_types_from_query
    assert f("sports_org", "점자도서관") == "sports_org", "명시 types 우선"
    assert f("", "근처 점자도서관 있어?") == "braille_lib"
    assert f("", "근처 도서관 어디야") == "booknarae_lib", "'도서관'만으로 점자도서관을 고르지 않는다"
    assert f("", "장애인이 운동할 수 있는 곳") == "sports_fac"
    assert f("", "장애인체육회 연락처") == "sports_org"
    assert f("", "장애인 운동 프로그램 있어?") == "sports_org", "'운동 프로그램' 은 체육회 질문"
    assert f("", "운동 프로그램 하는 체육관") == "sports_fac,sports_org", "체육시설 고유 낱말이 있으면 둘 다"
    assert f("", "뭐 있어") == "braille_lib,booknarae_lib,sports_fac,sports_org", "아무것도 안 걸리면 4종"
    assert f("bogus", "") == "braille_lib,booknarae_lib,sports_fac,sports_org", "잘못된 types 는 무시"


def t_cards_and_instruction():
    r, seen = _culture(CULTURE, lat=37.39, lng=126.95, query="도서관이랑 체육시설")
    assert r["status"] == "success" and r["count"] == 4, r.get("count")   # 모르는 종류는 버린다
    assert seen["fallback"] is True and seen["types"] == "booknarae_lib,sports_fac"
    lib = r["items"][0]
    assert lib["beyond_radius"] is True and lib["dist_text"] == "약 14.8km"
    assert lib["in_service_area"] is False and lib["homepage"] == "lib.eyes1004.com"
    assert r["items"][1]["dist_text"] == "850m"
    assert r["items"][3]["in_service_area"] is None, "값이 없으면 None 그대로(화면이 안내 버튼을 숨긴다)"
    ai = r["ai_instruction"]
    assert "반경 5km 안에는 없고, 경기도 자료 중 가장 가까운 곳" in ai and "단정하지" in ai
    assert "방문 전에 전화로 확인" in ai
    assert "택배" in ai and "단체 사무국" in ai and "시각장애인 도서관" in ai
    assert "길안내 지역 밖" in ai and "전화" in ai
    ua = r["ui_action"]
    assert ua["action"] == "show_culture" and ua["payload"]["base"] == {"lat": 37.39, "lng": 126.95}
    assert len(ua["payload"]["items"]) == 4


def t_empty_result():
    r, _ = _culture({"count": 0, "items": []}, lat=37.39, lng=126.95)
    assert r["count"] == 0
    assert "없다고 분명히" in r["ai_instruction"] and "경기도 자료" in r["ai_instruction"]
    assert "지어내지" in r["ai_instruction"]


def t_radius_clamped():
    _, seen = _culture(CULTURE, lat=37.39, lng=126.95, radius_m=999999)
    assert seen["radius_m"] == 20000
    _, seen = _culture(CULTURE, lat=37.39, lng=126.95, radius_m=10)
    assert seen["radius_m"] == 500
    _, seen = _culture(CULTURE, lat=37.39, lng=126.95, radius_m="x")
    assert seen["radius_m"] == 5000


def t_error_passthrough():
    r, _ = _culture({"status": "error", "error": "timeout"}, lat=37.39, lng=126.95)
    assert r["status"] == "error"


def t_need_location():
    r = _run(tool_handlers.tool_find_culture_sports())
    assert r["status"] != "success" and "위치" in r.get("ai_instruction", ""), r


def t_location_injection():
    loc = {"lat": 37.39, "lng": 126.95}
    assert inject_nav_defaults("find_culture_sports", {}, {}, loc) == {"lat": 37.39, "lng": 126.95}
    a = inject_nav_defaults("find_culture_sports", {"place": "안양역", "lat": 1, "lng": 2}, {}, loc)
    assert a == {"place": "안양역", "lat": 1, "lng": 2}


def t_registered():
    import inspect
    src = inspect.getsource(tool_handlers)
    assert '"find_culture_sports": tool_find_culture_sports' in src
    lb = (ROOT / "live_bridge.py").read_text(encoding="utf-8")
    assert 'name="find_culture_sports"' in lb and "`find_culture_sports`" in lb
    lp = (ROOT / "local_pipeline.py").read_text(encoding="utf-8")
    assert 'fn("find_culture_sports"' in lp
    mn = (ROOT / "main.py").read_text(encoding="utf-8")
    assert '"/api/v1/tools/find_culture_sports"' in mn


if __name__ == "__main__":
    for nm, fn in sorted((k, v) for k, v in list(globals().items()) if k.startswith("t_")):
        check(nm, fn)
    print()
    if FAILS:
        print("FAILED: %d" % len(FAILS))
        sys.exit(1)
    print("all passed")
