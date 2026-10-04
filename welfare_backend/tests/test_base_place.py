# -*- coding: utf-8 -*-
"""기준 장소를 쓰는 조회 도구의 실패 사유 구분 + 서비스 범위 캐시 회귀 테스트.

    python3 tests/test_base_place.py

배경: 화장실·긴급지원·식당·주변 정류장 도구는 "○○ 근처"처럼 기준 장소를 말로 받는다. 그 이름을
좌표로 바꾸지 못했을 때 '서비스 범위 밖'으로 답하면 안양시 안의 시설도 "안양시 밖"으로 안내되고,
함께 실리는 route_unavailable 화면 신호가 진행 중인 경로를 화면에서 지운다.

지키는 계약:
  1) 이름을 못 찾으면 place_not_found — 도구 이름을 담고, ui_action 은 없고, "밖"이라고 하지 않는다
  2) 찾았는데 범위 밖이면 out_of_service_area — 도구 이름을 담고, ui_action 은 없다
  3) 경로 도구(plan_accessible_route)의 두 응답은 종전 그대로 route_unavailable 을 싣는다
  4) 서비스 범위(bbox)는 조회에 성공했을 때만 캐시한다. 실패는 캐시하지 않고 짧은 간격 뒤
     다시 조회하며, 그 간격 안에서는 경로 서비스를 다시 부르지 않는다
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

FAILS = []
_BBOX = {"min_lat": 37.36, "min_lng": 126.88, "max_lat": 37.45, "max_lng": 127.00}
_INSIDE = {"lat": 37.3943, "lng": 126.9568, "label": "안양시청", "kind": "building"}
_OUTSIDE = {"lat": 37.5665, "lng": 126.9780, "label": "서울시청", "kind": "building"}


def check(name, fn):
    try:
        fn()
        print("  PASS  %s" % name)
    except AssertionError as e:
        FAILS.append(name)
        print("  FAIL  %s — %s" % (name, e))
    except Exception as e:                     # noqa: BLE001
        FAILS.append(name)
        print("  ERROR %s — %r" % (name, e))


def _run(coro):
    return asyncio.run(coro)


def _reset_bbox():
    tool_handlers._SERVICE_BBOX.update({"value": None, "checked": False, "retry_at": 0.0})


class _Env:
    """_resolve_place 와 경로 클라이언트를 가짜로 바꾼다. place → 해석 결과(없으면 None)."""

    def __init__(self, places):
        self.places = places
        self.calls = []            # 주변 조회 API 호출 기록

    def __enter__(self):
        env = self

        async def resolve(place):
            return env.places.get((place or "").strip())

        async def nearby(*a, **k):
            env.calls.append((a, k))
            return {"items": []}

        async def meta():
            return {"bbox": dict(_BBOX)}

        self._saved = (tool_handlers._resolve_place, tool_handlers.route_client)
        tool_handlers._resolve_place = resolve
        tool_handlers.route_client = types.SimpleNamespace(
            meta_network=meta, toilet_nearby=nearby, support_nearby=nearby, food_nearby=nearby,
            transit_access=nearby, bus_arrivals=nearby, SERVICE_AREA=route_client.SERVICE_AREA)
        _reset_bbox()
        return self

    def __exit__(self, *exc):
        tool_handlers._resolve_place, tool_handlers.route_client = self._saved
        _reset_bbox()
        return False


# 기준 장소를 받는 도구 전부 — (도구 이름, 호출)
_TOOLS = (
    ("find_toilet", lambda p: tool_handlers.tool_find_toilet(place=p)),
    ("find_emergency_support", lambda p: tool_handlers.tool_find_emergency_support(place=p, situation="배터리")),
    ("find_accessible_restaurants", lambda p: tool_handlers.tool_find_accessible_restaurants(place=p)),
    ("find_nearby_transit", lambda p: tool_handlers.tool_find_nearby_transit(place=p)),
    ("get_bus_arrivals", lambda p: tool_handlers.tool_get_bus_arrivals(place=p)),
)


def t_unknown_base_place_is_place_not_found_for_every_tool():
    for tool, call in _TOOLS:
        with _Env({}) as env:
            r = _run(call("안양시청 민원실"))
            assert r["status"] == "place_not_found", "%s: %s" % (tool, r.get("status"))
            assert r["tool_name"] == tool, "%s: tool_name=%s" % (tool, r.get("tool_name"))
            assert "ui_action" not in r, "%s: 화면의 경로를 지우는 신호가 실림 %s" % (tool, r.get("ui_action"))
            assert r["place"] == "안양시청 민원실" and "찾지 못" in r["message"]
            assert "밖이라고 말하지" in r["ai_instruction"], "%s: 범위 밖으로 말하지 말라는 지시가 없음" % tool
            assert "밖입니다" not in r["message"]
            assert env.calls == [], "%s: 장소를 못 찾았는데 주변 조회를 호출함" % tool


def t_out_of_area_base_place_is_out_of_service_area_for_every_tool():
    for tool, call in _TOOLS:
        with _Env({"서울시청": dict(_OUTSIDE)}) as env:
            r = _run(call("서울시청"))
            assert r["status"] == "out_of_service_area", "%s: %s" % (tool, r.get("status"))
            assert r["tool_name"] == tool
            assert "ui_action" not in r, "%s: 화면의 경로를 지우는 신호가 실림" % tool
            assert route_client.SERVICE_AREA in r["message"] and "서울시청" in r["message"]
            assert env.calls == [], "%s: 범위 밖인데 주변 조회를 호출함" % tool


def t_search_flag_marks_out_of_area_even_without_bbox():
    hit = dict(_INSIDE, in_service_area=False)        # 검색이 범위 밖이라고 알려 준 결과
    with _Env({"어딘가": hit}):
        r = _run(tool_handlers.tool_find_toilet(place="어딘가"))
    assert r["status"] == "out_of_service_area" and r["tool_name"] == "find_toilet", r


def t_resolved_base_place_inside_area_still_queries():
    for tool, call in _TOOLS:
        if tool == "get_bus_arrivals":
            continue                                   # 정류장 탐색 응답 형식이 달라 아래에서 따로 본다
        with _Env({"안양시청": dict(_INSIDE)}) as env:
            r = _run(call("안양시청"))
            assert r["status"] == "success", "%s: %s" % (tool, r)
            assert r["base_label"] == "안양시청"
            assert len(env.calls) == 1, "%s: 주변 조회를 하지 않음" % tool
    with _Env({"안양시청": dict(_INSIDE)}) as env:
        r = _run(tool_handlers.tool_get_bus_arrivals(place="안양시청"))
        assert r["status"] == "no_stop_nearby" and len(env.calls) == 1, r


def t_route_tool_keeps_screen_signal():
    with _Env({"서울시청": dict(_OUTSIDE)}):
        nf = _run(tool_handlers.tool_plan_accessible_route(destination_place="없는곳"))
        oos = _run(tool_handlers.tool_plan_accessible_route(destination_place="서울시청"))
    assert nf["status"] == "place_not_found" and nf["tool_name"] == "plan_accessible_route"
    assert nf["ui_action"]["action"] == "route_unavailable" and nf["ui_action"]["reason"] == "place_not_found"
    assert oos["status"] == "out_of_service_area"
    assert oos["ui_action"]["action"] == "route_unavailable"


# ── 서비스 범위 캐시 ────────────────────────────────────────────
class _Meta:
    def __init__(self, results):
        self.results, self.calls = list(results), 0

    async def __call__(self):
        self.calls += 1
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _with_meta(meta, fn):
    saved_rc, saved_log = tool_handlers.route_client, tool_handlers.logger.disabled
    tool_handlers.route_client = types.SimpleNamespace(meta_network=meta,
                                                       SERVICE_AREA=route_client.SERVICE_AREA)
    tool_handlers.logger.disabled = True        # 예상된 실패 로그로 출력이 어지럽지 않게
    _reset_bbox()
    try:
        return fn()
    finally:
        tool_handlers.route_client, tool_handlers.logger.disabled = saved_rc, saved_log
        _reset_bbox()


def t_bbox_failure_is_not_cached_and_retried_after_interval():
    err = {"status": "error", "message": "경로 서비스에 연결하지 못했습니다"}
    meta = _Meta([err, RuntimeError("boom"), {"bbox": dict(_BBOX)}])

    def body():
        assert _run(tool_handlers._service_bbox()) is None
        assert tool_handlers._SERVICE_BBOX["checked"] is False, "조회 실패를 캐시함"
        # 재시도 간격 안 — 경로 서비스를 다시 부르지 않는다
        assert _run(tool_handlers._service_bbox()) is None and meta.calls == 1
        assert _run(tool_handlers._outside_service_area(37.5665, 126.9780)) is False, \
            "범위를 모르면 '밖'이라고 단정하지 않는다"
        # 간격이 지나면 다시 조회한다(예외도 실패로 센다)
        tool_handlers._SERVICE_BBOX["retry_at"] = 0.0
        assert _run(tool_handlers._service_bbox()) is None and meta.calls == 2
        assert tool_handlers._SERVICE_BBOX["retry_at"] > 0.0
        tool_handlers._SERVICE_BBOX["retry_at"] = 0.0
        assert _run(tool_handlers._service_bbox()) == _BBOX and meta.calls == 3
        # 경로 서비스가 복구된 뒤에는 범위 판정이 다시 켜진다
        assert _run(tool_handlers._outside_service_area(37.5665, 126.9780)) is True
        assert _run(tool_handlers._outside_service_area(37.3943, 126.9568)) is False
    _with_meta(meta, body)


def t_bbox_success_is_cached():
    meta = _Meta([{"bbox": dict(_BBOX)}])

    def body():
        for _ in range(3):
            assert _run(tool_handlers._service_bbox()) == _BBOX
        assert meta.calls == 1, "성공한 범위를 다시 조회함"
    _with_meta(meta, body)


def t_bbox_retry_interval_is_short():
    assert 0 < tool_handlers._SERVICE_BBOX_RETRY_SEC <= 60


if __name__ == "__main__":
    for nm, fn in sorted((k, v) for k, v in list(globals().items())
                         if k.startswith("t_") and callable(v)):
        check(nm, fn)
    print()
    if FAILS:
        print("FAILED: %d" % len(FAILS))
        sys.exit(1)
    print("all passed")
