# -*- coding: utf-8 -*-
"""역을 목적지로 말하면 휠체어 출구까지 · 맞이방 가는 법 (v2.32.0 · 경로 서비스 v1.46.0).

    python3 tests/test_station_destination.py

계약:
  1) "명학역 맞이방"·"명학역 안"·"명학 지하철역"은 정적 역 매핑으로 명학역이다(이름 검색으로 넘기지 않는다)
  2) "명학역 1번 출구"는 역 매핑에 걸리지 않는다(그 출구가 목적지)
  3) 도보 목적지가 역이면 02 에 destination_station 을 싣는다 — 대중교통 모드·역 안 출발에는 싣지 않는다
  4) 02 station_end 가 오면 목적지 이름을 "명학역 1번 출구"로, 도구 결과에 station_end 를, 지침에 맞이방 안내를 싣는다
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
os.environ.setdefault("FEATURE_TOUR", "1")

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


END = {"station": "명학", "exit": {"exit_no": "1", "lat": 37.38507, "lng": 126.93493, "has_elevator": True},
       "enter": {"station": "명학", "exit_no": "1", "via": "elevator",
                 "inside": ["1번 출구 승강기로 맞이방에 올라갑니다 — 성결대학교 셔틀버스 정류장 앞"],
                 "arrive": "명학역 1번 출구 승강기 앞에 도착했습니다 (성결대학교 셔틀버스 정류장 앞). 승강기로 맞이방에 올라가세요"}}


def _route(extra=None, dist=344):
    d = {"route_id": "r_x", "destination": {"lat": 37.38507, "lng": 126.93493},
         "routes": [{"summary": {"total_distance_m": dist, "duration_sec": 300},
                     "steps": [{"instruction": "a"}, {"maneuver": "arrive", "instruction": END["enter"]["arrive"]}],
                     "legs": []}]}
    d.update(extra or {})
    return d


def _with_fake(responses):
    bodies = []

    async def fake_call(method, path, *, params=None, json=None):
        if path != "/route/plan":            # 서비스 범위 조회 등 — 경로 요청만 기록한다
            return {}
        bodies.append(json)
        return responses[min(len(bodies), len(responses)) - 1]
    return bodies, fake_call


def _resolve(q):
    return _run(tool_handlers._resolve_place(q))


def t_station_inside_words_resolve_to_station():
    for q in ("명학역 맞이방", "명학역맞이방", "명학역 안", "명학역 대합실", "명학 지하철역", "명학역", "명학",
              "명학역 승강기"):
        h = _resolve(q)
        assert h and h["kind"] == "station" and h["station"] == "명학", (q, h)
        assert h["label"] == "명학역(1호선)"


def t_exit_number_is_not_station_mapping():
    orig = route_client.poi_search

    async def fake_search(q, sigungu="안양", limit=5, include_outside=False):
        return {"items": []}
    route_client.poi_search = fake_search
    orig_k = tool_handlers.kakao_local.search

    async def fake_k(q, bbox=None, limit=5):
        return []
    tool_handlers.kakao_local.search = fake_k
    orig_t, orig_b = route_client.tour_spots, tool_handlers._service_bbox

    async def fake_t(*a, **k):
        return {"items": []}

    async def fake_b():
        return None
    route_client.tour_spots, tool_handlers._service_bbox = fake_t, fake_b
    try:
        h = _resolve("명학역 1번 출구")
        assert h is None or h.get("kind") != "station", h
    finally:
        route_client.poi_search = orig
        tool_handlers.kakao_local.search = orig_k
        route_client.tour_spots, tool_handlers._service_bbox = orig_t, orig_b


def _plan(**kw):
    bodies, fake = _with_fake([_route({"station_end": END})])
    orig = route_client._call
    route_client._call = fake
    try:
        r = _run(tool_handlers.tool_plan_accessible_route(origin_lat=37.3846, origin_lng=126.9319, **kw))
    finally:
        route_client._call = orig
    return r, bodies


def t_walk_to_station_sends_destination_station():
    r, bodies = _plan(destination_place="명학역 맞이방", mode="walk")
    assert bodies[0].get("destination_station") == {"name": "명학"}, bodies[0]
    assert r["status"] == "success"
    assert r["destination_label"] == "명학역 1번 출구"
    assert r["station_end"]["exit_no"] == "1" and r["station_end"]["via"] == "elevator"
    assert "맞이방에 올라갑니다" in r["ai_instruction"] and "명학역 1번 출구" in r["ai_instruction"]


def t_auto_mode_walk_call_sends_destination_station():
    r, bodies = _plan(destination_place="명학역")          # 짧은 거리 — 자동 추천은 도보를 유지한다
    assert bodies[0].get("destination_station") == {"name": "명학"}
    assert r["destination_label"] == "명학역 1번 출구"


def t_transit_mode_does_not_send():
    r, bodies = _plan(destination_place="명학역", mode="walk_subway")
    assert all("destination_station" not in (b or {}) for b in bodies), bodies


def t_non_station_destination_unchanged():
    bodies, fake = _with_fake([_route()])
    orig = route_client._call
    route_client._call = fake
    try:
        r = _run(tool_handlers.tool_plan_accessible_route(
            destination_poi_id="14792", origin_lat=37.4196, origin_lng=126.9085, mode="walk"))
    finally:
        route_client._call = orig
    assert "destination_station" not in bodies[0] and r["station_end"] is None


def t_route_client_skips_with_origin_station():
    bodies, fake = _with_fake([_route()])
    orig = route_client._call
    route_client._call = fake
    try:
        _run(route_client.plan_route({"lat": 1, "lng": 2}, {"type": "coord", "lat": 1, "lng": 2}, mode="walk",
                                     origin_station={"name": "관악"}, dest_station="명학"))
    finally:
        route_client._call = orig
    assert "destination_station" not in bodies[0] and "origin_station" in bodies[0]


if __name__ == "__main__":
    for nm, fn in sorted((k, v) for k, v in list(globals().items()) if k.startswith("t_")):
        check(nm, fn)
    print()
    if FAILS:
        print("FAILED: %d" % len(FAILS))
        sys.exit(1)
    print("all passed")
