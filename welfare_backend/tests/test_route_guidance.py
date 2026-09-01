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
  4) "이름을 못 찾음"(place_not_found)과 "범위 밖"(out_of_service_area)을 섞지 않는다 —
     안양시 안에 있는 시설이 "안양시 밖"으로 안내되면 이용자가 서비스 범위를 오해한다
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
    """목적지를 못 찾은 것은 '범위 밖'이 아니다 — 그렇게 말하지 말라고 지시해야 한다."""
    ai = route_client._ai_for_4xx("목적지 POI 를 찾을 수 없습니다 (poi_backend=db)")
    no_transient(ai, "목적지 미발견")
    assert route_client.SERVICE_AREA in ai, "서비스 지역명이 없음"
    assert "밖이라고 말하지 마세요" in ai, "'범위 밖'으로 안내하지 말라는 지시가 없음"
    assert "찾지 못했다" in ai


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


# 02 /poi/search 응답 스텁 — 이름으로 찾히는 장소들
_SEARCH = {
    "안양시청": {"count": 1, "items": [
        {"type": "building", "poi_id": None, "name": "안양시청",
         "lat": 37.39429, "lng": 126.95687}]},
    "서울시청": {"count": 1, "items": [
        {"type": "building", "poi_id": None, "name": "서울시청",
         "lat": 37.5665, "lng": 126.9780}]},
    "평촌아트홀": {"count": 1, "items": [
        {"type": "tour", "poi_id": "TBF-1", "name": "평촌아트홀",
         "lat": 37.3906, "lng": 126.9505}]},
}
# 지역 필터를 풀었을 때만 나오는 결과 — 범위 밖임이 표시되어 돌아온다
_SEARCH_WIDE = {
    "관악장애인종합복지관": {"count": 1, "items": [
        {"type": "tour", "poi_id": "SEOUL-1", "name": "관악장애인종합복지관",
         "lat": 37.4784, "lng": 126.9516, "in_service_area": False}]},
}
# 안양 보행망 bbox (실측 근사) — "범위 밖" 판정의 유일한 근거
_BBOX = {"min_lat": 37.36, "min_lng": 126.88, "max_lat": 37.45, "max_lng": 127.00}


class _Spy(object):
    def __init__(self):
        self.plan_called = 0
        self.search_called = 0

    async def tour_spots(self, sigungu="안양", limit=60):
        return _TOUR

    async def poi_search(self, q, sigungu="안양", limit=8, include_outside=False):
        self.search_called += 1
        key = q.strip()
        if include_outside and key in _SEARCH_WIDE:
            return dict(_SEARCH_WIDE[key])
        return dict(_SEARCH.get(key, {"count": 0, "items": []}))

    async def meta_network(self):
        return {"region": "안양시", "bbox": _BBOX}

    async def plan_route(self, origin, destination, profile="wheelchair_manual", alternatives=1, mode=""):
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
                                 poi_search=spy.poi_search, meta_network=spy.meta_network,
                                 SERVICE_AREA=route_client.SERVICE_AREA)
    tool_handlers.route_client = stub
    tool_handlers._SERVICE_BBOX["value"] = None      # 범위 캐시는 테스트마다 새로 조회
    tool_handlers._SERVICE_BBOX["checked"] = False
    try:
        return fn(spy)
    finally:
        tool_handlers.route_client = orig
        tool_handlers._SERVICE_BBOX["value"] = None
        tool_handlers._SERVICE_BBOX["checked"] = False


def t_out_of_area_destination():
    """좌표는 찾았지만 보행망 범위 밖 — 이때만 '지역 밖'이라고 말한다."""
    def body(spy):
        r = _run(tool_handlers.tool_plan_accessible_route(
            destination_place="서울시청", origin_place="안양역"))
        assert r["status"] == "out_of_service_area", r
        assert spy.plan_called == 0, "범위 밖인데 경로 API 를 호출함"
        no_transient(r["ai_instruction"], "범위 밖 목적지")
        assert route_client.SERVICE_AREA in r["message"]
        assert r["ui_action"]["action"] == "route_unavailable", "화면에 알리지 않음"
    _with_spy(body)


def t_place_not_found_is_not_out_of_area():
    """이름을 못 찾은 것을 '지역 밖'으로 안내하면 이용자가 서비스 범위를 오해한다."""
    def body(spy):
        r = _run(tool_handlers.tool_plan_accessible_route(
            destination_place="없는이름복지관", origin_place="안양역"))
        assert r["status"] == "place_not_found", r
        assert spy.plan_called == 0
        no_transient(r["ai_instruction"], "이름 미발견")
        ai = r["ai_instruction"]
        assert "밖이라고 말하지 마세요" in ai, "'지역 밖'으로 말하지 말라는 지시가 없음"
        assert "지도에서" in ai, "다음에 할 일(지도 지정)을 알려주지 않음"
        assert r["ui_action"]["reason"] == "place_not_found"
    _with_spy(body)


def t_general_facility_resolves_by_name():
    """관광지도 역도 아닌 시설(시청·복지관)이 이름으로 해석돼야 한다 — 이 결함의 본체."""
    def body(spy):
        r = _run(tool_handlers.tool_plan_accessible_route(
            destination_place="안양시청", origin_place="안양역"))
        assert r["status"] == "success", r
        assert spy.search_called >= 1, "장소 검색을 시도하지 않음"
        assert spy.last[1]["type"] == "building", spy.last
        assert abs(spy.last[1]["lat"] - 37.39429) < 1e-6, spy.last
        assert r["destination_label"] == "안양시청"
    _with_spy(body)


def t_map_picked_coord_destination():
    """지도에서 콕 집은 점은 좌표 그대로 쓴다(대표점 보정 없음)."""
    def body(spy):
        r = _run(tool_handlers.tool_plan_accessible_route(
            destination_lat=37.3960, destination_lng=126.9577,
            destination_type="coord", origin_place="안양역"))
        assert r["status"] == "success", r
        assert spy.last[1]["type"] == "coord", spy.last
        assert spy.search_called == 0, "지도 좌표인데 이름 검색을 함"
    _with_spy(body)


def t_map_picked_coord_out_of_area():
    def body(spy):
        r = _run(tool_handlers.tool_plan_accessible_route(
            destination_lat=37.5665, destination_lng=126.9780,
            destination_type="coord", origin_place="안양역"))
        assert r["status"] == "out_of_service_area", r
        assert spy.plan_called == 0
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


def t_station_destination_uses_building_access():
    """역·건물은 시설 대표점이므로 02 가 출입구 접근점을 다시 잡도록 building 으로 넘긴다."""
    def body(spy):
        r = _run(tool_handlers.tool_plan_accessible_route(
            destination_place="범계역", origin_place="안양역"))
        assert r["status"] == "success", r
        assert spy.last[1]["type"] == "building", spy.last
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


def t_unknown_origin_is_place_not_found():
    def body(spy):
        r = _run(tool_handlers.tool_plan_accessible_route(
            destination_poi_id="TBF-1", origin_place="없는이름역앞"))
        assert r["status"] == "place_not_found", r
        assert r["kind"] == "출발지", r
        assert spy.plan_called == 0
    _with_spy(body)


def t_wide_search_reports_out_of_area_not_missing():
    """지역 밖이라 안 되는 것을 '이름을 못 찾음'으로 말하면 그것도 부정확하다.

    안양 안에서 못 찾으면 범위를 넓혀 한 번 더 보고, 거기서 찾히면 '범위 밖'으로 답한다.
    """
    def body(spy):
        r = _run(tool_handlers.tool_plan_accessible_route(
            destination_place="관악장애인종합복지관", origin_place="안양역"))
        assert r["status"] == "out_of_service_area", r
        assert spy.plan_called == 0
        assert spy.search_called >= 2, "넓힌 재검색을 하지 않음"
        no_transient(r["ai_instruction"], "넓힌 검색 범위 밖")
    _with_spy(body)


def t_out_of_area_message_uses_spoken_name():
    """범위 밖 안내는 이용자가 말한 이름으로 한다.

    넓힌 재검색은 전국 대상이라 느슨하게 매칭된 상호명이 잡힐 수 있고, 그 이름을
    되읽으면 이용자는 자기가 말한 곳 이야기가 아니라고 느낀다.
    """
    def body(spy):
        r = _run(tool_handlers.tool_plan_accessible_route(
            destination_place="관악장애인종합복지관", origin_place="안양역"))
        assert r["status"] == "out_of_service_area", r
        assert "관악장애인종합복지관" in r["message"], r["message"]
        assert r["place"] == "관악장애인종합복지관", r
    _with_spy(body)


def t_place_not_found_may_mention_area_when_user_said_it():
    """발화 자체로 다른 시·도가 분명하면 그 사실은 말해도 된다 — 단정은 금지."""
    def body(spy):
        r = _run(tool_handlers.tool_plan_accessible_route(
            destination_place="없는이름복지관", origin_place="안양역"))
        ai = r["ai_instruction"]
        assert "단정하지는" in ai, "도구 결과만으로 범위 밖 단정 금지 지시가 없음"
        assert "발화 자체로 분명하다면" in ai
    _with_spy(body)


# ── 4. 실패해도 진행 중인 안내는 계속된다 — 그 사실을 반드시 말한다 ──
def t_failure_keeps_active_guidance():
    """말로는 "안내할 수 없다"면서 화면·음성 안내는 계속되면 어긋나 보인다."""
    import nav_context
    nav = {"guiding": True, "route_id": "r_prev", "dest_name": "평촌아트홀",
           "step_idx": 2, "total_steps": 8}
    r = nav_context.annotate_route_failure(
        {"status": "place_not_found", "ai_instruction": "찾지 못했다고 안내하세요.",
         "ui_action": {"action": "route_unavailable"}}, nav)
    assert r["active_guidance"]["destination"] == "평촌아트홀", r
    assert r["active_guidance"]["step_no"] == 3, r
    assert "그대로 계속된다" in r["ai_instruction"], r["ai_instruction"]
    assert r["ui_action"]["guiding_kept"] is True


def t_failure_without_guidance_is_untouched():
    import nav_context
    base = {"status": "place_not_found", "ai_instruction": "찾지 못했다고 안내하세요."}
    r = nav_context.annotate_route_failure(dict(base), {"guiding": False})
    assert "active_guidance" not in r, r
    assert r["ai_instruction"] == base["ai_instruction"]


def t_success_is_not_annotated():
    import nav_context
    r = nav_context.annotate_route_failure(
        {"status": "success", "ai_instruction": "요약하세요."},
        {"guiding": True, "dest_name": "평촌아트홀"})
    assert "active_guidance" not in r, r


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
