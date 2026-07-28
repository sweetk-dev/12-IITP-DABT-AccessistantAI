# -*- coding: utf-8 -*-
"""경로 안내 실패 안내문 회귀 테스트 (의존성 없이 단독 실행).

    python3 tests/test_route_guidance.py

배경: 실브라우저 테스트에서 "안양역 → 관악장애인종합복지관"(서울 관악구, 서비스 범위 밖)을
요청하자 비서가 "경로 안내 서비스에 잠시 장애가 있어서…" 라고 답했다.
실제로는 서비스가 정상이었고, 02-Route 가 404("목적지 POI 를 찾을 수 없습니다")를 준 것이다.
사용자는 "잠시 후 다시 하면 되겠지"라고 오해한 채 영원히 되지 않는 요청을 반복하게 된다.

여기서 지키는 계약:
  1) 4xx(요청 자체의 문제) 안내문에는 "일시적/장애" 표현이 절대 들어가지 않는다
  2) 5xx·타임아웃·서킷 오픈(진짜 장애)에만 "일시적" 표현이 들어간다
  3) 범위 밖 목적지는 경로 API 를 호출하기 전에 걸러 out_of_service_area 로 답한다
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

# tool_handlers 는 DB 계층을 import 한다 — 안내문 로직만 검증하므로 최소 스텁으로 대체
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

import route_client                      # noqa: E402
import tool_handlers                     # noqa: E402

FAILS = []
BANNED = ("일시적", "장애가", "잠시 후", "정상화")
# 안내문은 모델에게 "일시적인 오류라고 말하지 말라"고 지시한다 — 이 부정형은 금칙어가 아니다.
NEGATIONS = (
    "서비스 장애나 일시적인 오류라고 말하지 말고",
    "일시적인 오류라고 말하지 말고",
    "일시적인 장애라고 말하지 말고",
)


def check(name, fn):
    try:
        fn()
        print("  PASS  %s" % name)
    except AssertionError as e:
        FAILS.append(name)
        print("  FAIL  %s — %s" % (name, e))


def no_transient(text, where):
    """긍정형으로 '일시적 장애'를 주장하지 않는지 본다 (부정 지시문은 제외)."""
    assert any(neg in text for neg in NEGATIONS), \
        "%s 안내문에 '일시적 오류라고 말하지 말라'는 지시가 없음: %s" % (where, text)
    stripped = text
    for neg in NEGATIONS:
        stripped = stripped.replace(neg, "")
    for w in BANNED:
        assert w not in stripped, "%s 안내문이 '%s' 를 주장함: %s" % (where, w, text)


# ── 1. 4xx 사유별 안내문 ───────────────────────────────────────────────
def t_dest_not_found():
    ai = route_client._ai_for_4xx("목적지 POI 를 찾을 수 없습니다 (poi_backend=db)")
    no_transient(ai, "목적지 미발견")
    assert route_client.SERVICE_AREA in ai, "서비스 지역명이 없음"
    assert "밖" in ai, "범위 밖이라는 사실이 없음"


def t_poi_id_required():
    ai = route_client._ai_for_4xx("poi_id 가 필요합니다")
    no_transient(ai, "poi_id 누락")


def t_origin_far():
    ai = route_client._ai_for_4xx("현재 위치가 보행 네트워크에서 812m 떨어져 있어 경로를 만들 수 없습니다")
    no_transient(ai, "출발지 이탈")
    assert "출발지" in ai


def t_route_expired():
    ai = route_client._ai_for_4xx("경로를 찾을 수 없습니다(만료되었을 수 있음)")
    no_transient(ai, "경로 만료")
    assert "만료" in ai


def t_no_route():
    ai = route_client._ai_for_4xx("통행 가능한 경로를 찾지 못했습니다")
    no_transient(ai, "경로 없음")


def t_default_err_is_neutral():
    no_transient(route_client._err("경로를 만들 수 없습니다")["ai_instruction"], "_err 기본값")


# ── 2. 진짜 장애일 때만 "일시적" ───────────────────────────────────────
def t_transient_keeps_wording():
    assert "일시적" in route_client._AI_TRANSIENT or "잠시" in route_client._AI_TRANSIENT


def t_circuit_open_is_transient():
    route_client._open_until = 9e18
    try:
        r = asyncio.get_event_loop().run_until_complete(route_client._call("GET", "/profiles"))
    finally:
        route_client._open_until = 0.0
    assert r["status"] == "error"
    assert r["ai_instruction"] == route_client._AI_TRANSIENT


# ── 3. 범위 밖 목적지는 경로 API 를 부르기 전에 차단 ───────────────────
_TOUR = {"items": [
    {"poi_id": "TBF-1", "name": "평촌아트홀", "lat": 37.3906, "lng": 126.9505},
]}


class _Spy(object):
    def __init__(self):
        self.plan_called = 0

    async def tour_spots(self, sigungu="안양", limit=60):
        return _TOUR

    async def plan_route(self, origin, destination, profile="wheelchair_manual", alternatives=1):
        self.plan_called += 1
        self.last = (origin, destination)
        return {"route_id": "r_x", "routes": [{"summary": {"total_distance_m": 100,
                "duration_sec": 120, "max_slope_deg": 1.0, "stairs_cnt": 0,
                "crossing_cnt": 1, "warnings": []}, "steps": [{"instruction": "직진"}]}]}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _with_spy(fn):
    spy = _Spy()
    orig = tool_handlers.route_client
    stub = types.SimpleNamespace(tour_spots=spy.tour_spots, plan_route=spy.plan_route,
                                 SERVICE_AREA=route_client.SERVICE_AREA)
    tool_handlers.route_client = stub
    try:
        return fn(spy)
    finally:
        tool_handlers.route_client = orig


def t_out_of_area_destination():
    def body(spy):
        r = _run(tool_handlers.tool_plan_accessible_route(
            destination_place="관악장애인종합복지관", origin_place="안양역"))
        assert r["status"] == "out_of_service_area", r
        assert spy.plan_called == 0, "범위 밖인데 경로 API 를 호출함"
        no_transient(r["ai_instruction"], "범위 밖 목적지")
        assert route_client.SERVICE_AREA in r["message"]
    _with_spy(body)


def t_known_destination_still_works():
    def body(spy):
        r = _run(tool_handlers.tool_plan_accessible_route(
            destination_place="평촌아트홀", origin_place="안양역"))
        assert r["status"] == "success", r
        assert spy.plan_called == 1
        assert spy.last[1]["poi_id"] == "TBF-1", spy.last
        assert r["origin_label"].startswith("안양역")
        assert r["destination_label"] == "평촌아트홀"
    _with_spy(body)


def t_station_destination_uses_coord():
    def body(spy):
        r = _run(tool_handlers.tool_plan_accessible_route(
            destination_place="범계역", origin_place="안양역"))
        assert r["status"] == "success", r
        assert spy.last[1]["type"] == "coord", spy.last
    _with_spy(body)


def t_no_destination_asks_back():
    def body(spy):
        r = _run(tool_handlers.tool_plan_accessible_route(origin_place="안양역"))
        assert r["status"] == "need_destination", r
        assert spy.plan_called == 0
    _with_spy(body)


def t_out_of_area_origin():
    def body(spy):
        r = _run(tool_handlers.tool_plan_accessible_route(
            destination_poi_id="TBF-1", origin_place="서울시청"))
        assert r["status"] == "out_of_service_area", r
        assert spy.plan_called == 0
        no_transient(r["ai_instruction"], "범위 밖 출발지")
    _with_spy(body)


if __name__ == "__main__":
    print("경로 안내 실패 안내문 회귀 테스트")
    for nm, fn in sorted((k[2:], v) for k, v in list(globals().items())
                         if k.startswith("t_") and callable(v)):
        check(nm, fn)
    print("")
    if FAILS:
        print("FAILED %d" % len(FAILS))
        sys.exit(1)
    print("ALL PASSED")
