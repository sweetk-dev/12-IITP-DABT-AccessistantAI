# -*- coding: utf-8 -*-
"""정책상담 → 이동경로 안내 넘기기 (v2.0.0).

    python3 tests/test_handoff.py

계약:
  1) 정책상담 세션은 경로를 만들지 않고 목적지만 확정해 넘긴다(경로 서버 호출 없음)
  2) 목적지를 못 찾거나 범위 밖이면 넘기지 않고 종전 사유 그대로 답한다
  3) 세션 종류는 서버가 주입한다 — 모델이 보낸 handoff 값은 무시된다
  4) 이동경로 안내 세션(mode == "navi")은 종전대로 경로를 만든다
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
import nav_context                                         # noqa: E402

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


class _Patch:
    """도구가 쓰는 조회 함수를 바꿔 끼운다. 경로 서버를 부르면 실패로 기록한다."""

    def __init__(self, hit=None, outside=False):
        self.hit, self.outside, self.route_calls = hit, outside, 0
        self._saved = {}

    def __enter__(self):
        async def _resolve(place):
            return self.hit

        async def _outside_place(h):
            return self.outside

        async def _outside_area(lat, lng):
            return self.outside

        async def _plan(*a, **k):
            self.route_calls += 1
            return {"status": "error"}

        for mod, nm, fn in ((tool_handlers, "_resolve_place", _resolve),
                            (tool_handlers, "_outside_place", _outside_place),
                            (tool_handlers, "_outside_service_area", _outside_area),
                            (route_client, "plan_route", _plan)):
            if hasattr(mod, nm):
                self._saved[(mod, nm)] = getattr(mod, nm)
                setattr(mod, nm, fn)
        return self

    def __exit__(self, *a):
        for (mod, nm), fn in self._saved.items():
            setattr(mod, nm, fn)


def t_building_destination_is_handed_off_without_routing():
    hit = {"label": "안양시청", "lat": 37.3943, "lng": 126.9568}
    with _Patch(hit) as p:
        r = _run(tool_handlers.tool_plan_accessible_route(destination_place="시청", profile="visual", handoff=True))
    assert r["status"] == "handoff", r
    assert p.route_calls == 0
    ui = r["ui_action"]
    assert ui["action"] == "handoff_navi" and ui["profile"] == "visual"
    assert ui["dest"] == {"name": "안양시청", "kind": "building", "lat": 37.3943, "lng": 126.9568}
    assert "버튼" in r["ai_instruction"] and "말하지 마세요" in r["ai_instruction"]


def t_tour_poi_destination_keeps_poi_id():
    hit = {"label": "안양예술공원", "lat": 37.42, "lng": 126.92, "poi_id": "T77"}
    with _Patch(hit):
        r = _run(tool_handlers.tool_plan_accessible_route(destination_place="예술공원", handoff=True))
    d = r["ui_action"]["dest"]
    assert d["poi_id"] == "T77" and d["kind"] == "tour" and d["name"] == "안양예술공원"
    assert "lat" not in d          # poi 는 경로 서버가 접근점을 정한다


def t_origin_is_not_required_for_handoff():
    hit = {"label": "안양역", "lat": 37.4016, "lng": 126.9228}
    with _Patch(hit):
        r = _run(tool_handlers.tool_plan_accessible_route(destination_place="안양역", handoff=True))
    assert r["status"] == "handoff"      # 현재 위치가 없어도 need_location 이 아니다


def t_unknown_profile_is_not_carried():
    hit = {"label": "안양역", "lat": 37.4016, "lng": 126.9228}
    with _Patch(hit):
        r = _run(tool_handlers.tool_plan_accessible_route(destination_place="안양역", profile="root", handoff=True))
    assert r["ui_action"]["profile"] == ""


def t_not_found_and_out_of_area_are_not_handed_off():
    with _Patch(None):
        r = _run(tool_handlers.tool_plan_accessible_route(destination_place="없는곳", handoff=True))
    assert r["status"] == "place_not_found", r
    with _Patch({"label": "서울시청", "lat": 37.56, "lng": 126.97}, outside=True):
        r = _run(tool_handlers.tool_plan_accessible_route(destination_place="서울시청", handoff=True))
    assert r["status"] == "out_of_service_area", r
    with _Patch(None):
        r = _run(tool_handlers.tool_plan_accessible_route(handoff=True))
    assert r["status"] == "need_destination", r


def t_open_navi_screen_handoff_wording():
    r = _run(tool_handlers.tool_open_navi_screen(handoff=True))
    assert r["ui_action"] == {"action": "open_navi"} and "버튼" in r["ai_instruction"]
    r2 = _run(tool_handlers.tool_open_navi_screen())
    assert "이동했다고" in r2["ai_instruction"]


def t_session_kind_decides_handoff_not_the_model():
    f = nav_context.inject_handoff
    assert f("plan_accessible_route", {"destination_place": "x"}, None)["handoff"] is True
    assert f("plan_accessible_route", {"handoff": False}, "")["handoff"] is True
    assert "handoff" not in f("plan_accessible_route", {"handoff": True}, "navi")
    assert f("open_navi_screen", {}, "policy")["handoff"] is True
    assert "handoff" not in f("find_toilet", {"place": "x"}, None)


if __name__ == "__main__":
    for nm, fn in sorted((k, v) for k, v in list(globals().items()) if k.startswith("t_")):
        check(nm, fn)
    print()
    if FAILS:
        print("FAILED: %d" % len(FAILS))
        sys.exit(1)
    print("all passed")
