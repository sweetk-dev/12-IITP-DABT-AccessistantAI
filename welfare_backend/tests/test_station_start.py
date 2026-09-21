# -*- coding: utf-8 -*-
"""역에서 출발하는 도보 경로 — 역 안/밖 · 타고 온 방향 (v1.51.0, #300 · 02 v1.28.0).

    python3 tests/test_station_start.py

계약:
  1) origin_station 이 오면 도보로만 계획하고(자동 추천 승격 없음) 02 에 origin_station 을 싣는다
  2) 02 station_nearby 는 최소 필드로 도구 결과에 싣고, 모델에 '말로 답하면 다시 호출' 지침을 준다
  3) station_start 는 역 안 안내 문장(inside)을 싣고 먼저 말하라고 지시한다
  4) 대중교통 모드에는 origin_station 을 보내지 않는다
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


def _route(extra=None, dist=1289):
    d = {"route_id": "r_x", "routes": [{"summary": {"total_distance_m": dist, "duration_sec": 1170},
                                        "steps": [{"instruction": "a"}], "legs": []}]}
    d.update(extra or {})
    return d


HINT = {"station": "관악", "distance_m": 40, "question": "지금 관악역 안(승강장)에 계신가요, 역 밖에 계신가요?",
        "travel_question": "어느 쪽에서 열차를 타고 오셨나요?",
        "choices": [{"travel": "south", "updown": "하행", "label": "석수·서울 쪽에서 타고 왔어요"},
                    {"travel": "north", "updown": "상행", "label": "안양·수원 쪽에서 타고 왔어요"}]}
START = {"station": "관악", "travel": "south", "exit": {"exit_no": "2"},
         "egress": {"inside": ["내린 승강장의 승강기로 이동합니다 — (1F) 안양역 방향 승강장 진행방향 앞쪽 끝",
                               "2번 출구 승강기로 나갑니다 — (1F) 2번 출구 옆"]}}


def _with_fake(responses):
    bodies = []

    async def fake_call(method, path, *, params=None, json=None):
        bodies.append(json)
        return responses[min(len(bodies), len(responses)) - 1]
    return bodies, fake_call


def t_inside_station_forces_walk_and_sends_origin_station():
    bodies, fake = _with_fake([_route({"station_start": START})])
    orig = route_client._call
    route_client._call = fake
    try:
        r = _run(tool_handlers.tool_plan_accessible_route(
            destination_poi_id="14792", origin_lat=37.4196, origin_lng=126.9085,
            origin_station="관악", origin_travel="SOUTH"))
    finally:
        route_client._call = orig
    assert len(bodies) == 1, "자동 추천 승격(대중교통 재요청)이 일어나면 안 된다: %d" % len(bodies)
    b = bodies[0]
    assert "mode" not in b and b["origin_station"] == {"name": "관악", "travel": "south"}, b
    assert r["mode_used"] == "walk" and r["auto_mode"] is False
    ss = r["station_start"]
    assert ss["exit_no"] == "2" and ss["inside"][0].startswith("내린 승강장의 승강기")
    assert "station_start.inside" in r["ai_instruction"]
    assert r["station_nearby"] is None


def t_unknown_travel_is_sent_as_none():
    bodies, fake = _with_fake([_route({"station_start": START})])
    orig = route_client._call
    route_client._call = fake
    try:
        _run(tool_handlers.tool_plan_accessible_route(
            destination_poi_id="14792", origin_lat=37.4196, origin_lng=126.9085,
            origin_station="관악", origin_travel="모름"))
    finally:
        route_client._call = orig
    assert bodies[0]["origin_station"]["travel"] is None


def t_station_nearby_brief_and_instruction():
    bodies, fake = _with_fake([_route({"station_nearby": HINT}, dist=500)])
    orig = route_client._call
    route_client._call = fake
    try:
        r = _run(tool_handlers.tool_plan_accessible_route(
            destination_poi_id="14792", origin_lat=37.4196, origin_lng=126.9085))
    finally:
        route_client._call = orig
    assert "origin_station" not in bodies[0]
    sn = r["station_nearby"]
    assert sn["station"] == "관악" and [c["travel"] for c in sn["choices"]] == ["south", "north"]
    assert "updown" not in sn["choices"][0]
    ai = r["ai_instruction"]
    assert "origin_station='관악'" in ai and "석수·서울 쪽에서 타고 왔어요" in ai


def t_transit_mode_never_sends_origin_station():
    bodies, fake = _with_fake([_route()])
    orig = route_client._call
    route_client._call = fake
    try:
        _run(route_client.plan_route({"lat": 1, "lng": 2}, {"type": "tour", "poi_id": "1"},
                                     mode="walk_subway", origin_station={"name": "관악", "travel": "south"}))
        _run(route_client.plan_route({"lat": 1, "lng": 2}, {"type": "tour", "poi_id": "1"},
                                     mode="walk", origin_station={"name": "가"*25, "travel": None}))
    finally:
        route_client._call = orig
    assert "origin_station" not in bodies[0]
    assert len(bodies[1]["origin_station"]["name"]) == 20


if __name__ == "__main__":
    for nm, fn in sorted((k, v) for k, v in list(globals().items()) if k.startswith("t_")):
        check(nm, fn)
    print()
    if FAILS:
        print("FAILED: %d" % len(FAILS))
        sys.exit(1)
    print("all passed")
