# -*- coding: utf-8 -*-
"""안내 중 대화 고도화 회귀 테스트 (#248).

    python3 tests/test_guidance_dialog.py

여기서 지키는 계약:
  1) nav_state 메시지는 허용 필드만, 형이 깨져도 안전하게 세션에 반영된다
  2) get_current_guidance 는 안내 중이면 현재 안내 문장을, 아니면 idle 을 준다
  3) explain_route_segment 는 안내 진행 중일 때 '경고 구간 자동 선택'이 아니라
     현재 구간이 기본값이 된다 — 단, 모델이 명시한 값은 덮지 않는다
  4) find_nearby_transit 은 accessible=None 을 '이용 불가'로 뭉개지 않는다
     (accessible_status 유지 + ai_instruction 에 미판정 안내 포함)
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

# tool_handlers 는 DB 계층을 import 한다 — 도구 계약만 검증하므로 최소 스텁으로 대체
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
from nav_context import (update_nav_state, current_guidance_result,   # noqa: E402
                         inject_nav_defaults, note_new_route)

FAILS = []


def check(name, fn):
    try:
        fn()
        print("  PASS  %s" % name)
    except AssertionError as e:
        FAILS.append(name)
        print("  FAIL  %s — %s" % (name, e))


NAV_MSG = {
    "type": "nav_state", "route_id": "r_ab12", "guiding": True,
    "step_idx": 4, "total_steps": 11,
    "current": "만안로를 따라 120m 직진합니다", "next": "횡단보도를 건넙니다",
    "remaining_m": 86, "dest_name": "김중업건축박물관", "profile": "wheelchair_manual",
    "hack": "should_be_dropped",
}


# ── 1. nav_state 반영 ────────────────────────────────────────────────
def t_update_keeps_allowed_fields_only():
    nav = {}
    update_nav_state(nav, NAV_MSG)
    assert nav["route_id"] == "r_ab12" and nav["step_idx"] == 4
    assert "hack" not in nav, "허용 밖 필드가 세션에 들어옴"


def t_update_survives_bad_types():
    nav = {}
    update_nav_state(nav, {"guiding": "yes", "step_idx": "abc", "remaining_m": None})
    assert nav["guiding"] is True
    assert nav["step_idx"] is None, "형이 깨진 step_idx 가 그대로 남음"


# ── 2. get_current_guidance ─────────────────────────────────────────
def t_guidance_result_while_guiding():
    nav = {}
    update_nav_state(nav, NAV_MSG)
    r = current_guidance_result(nav)
    assert r["status"] == "guiding"
    assert r["current_instruction"] == NAV_MSG["current"]
    assert r["step_no"] == 5, "step_no 는 사람 기준 1부터여야 함"
    assert "1~2문장" in r["ai_instruction"], "이동 중 짧은 답변 지시가 없음"


def t_guidance_result_idle():
    r = current_guidance_result({})
    assert r["status"] == "idle"
    r2 = current_guidance_result({"route_id": "r_x", "guiding": False})
    assert r2["status"] == "idle" and r2["route_id"] == "r_x"


def t_guidance_result_arrived():
    """도착 직후에는 '안내를 다시 시작하라'고 권하면 안 된다 (리뷰 #5)."""
    nav = {}
    update_nav_state(nav, dict(NAV_MSG, guiding=False, step_idx=10, total_steps=11))
    r = current_guidance_result(nav)
    assert r["status"] == "arrived"
    assert "도착" in r["ai_instruction"]
    assert "안내 시작" not in r["ai_instruction"], "도착 후 재시작 권유가 남아 있음"


def t_new_route_resets_stale_state():
    """대화로 새 경로가 생기면 이전 경로의 step_idx 가 주입되면 안 된다 (리뷰 #3)."""
    nav = {}
    update_nav_state(nav, NAV_MSG)              # 이전 경로 r_ab12 진행 중
    note_new_route(nav, "r_new99")              # plan_accessible_route 성공 직후
    fargs = inject_nav_defaults("explain_route_segment", {}, nav, {})
    assert fargs["route_id"] == "r_new99", "이전 route_id 가 주입됨"
    assert fargs.get("step_idx") is None, "이전 경로의 step_idx 가 새 경로에 주입됨"
    # 같은 route_id 로 다시 불리면 상태를 지우지 않는다
    update_nav_state(nav, dict(NAV_MSG, route_id="r_new99", step_idx=2))
    note_new_route(nav, "r_new99")
    assert nav["step_idx"] == 2, "동일 경로 재통지가 진행 상태를 지움"


# ── 3. explain_route_segment 기본 구간 주입 ──────────────────────────
def t_inject_explain_defaults():
    nav = {}
    update_nav_state(nav, NAV_MSG)
    fargs = inject_nav_defaults("explain_route_segment", {}, nav, {})
    assert fargs["route_id"] == "r_ab12"
    assert fargs["step_idx"] == 4, "현재 구간이 기본값이 아님(경고 구간 자동 선택으로 새어 나감)"


def t_inject_respects_explicit_args():
    nav = {}
    update_nav_state(nav, NAV_MSG)
    fargs = inject_nav_defaults("explain_route_segment",
                                {"route_id": "r_old", "step_idx": 1}, nav, {})
    assert fargs["route_id"] == "r_old" and fargs["step_idx"] == 1, "모델이 명시한 값을 덮어씀"


def t_inject_no_stepidx_when_not_guiding():
    nav = {}
    update_nav_state(nav, dict(NAV_MSG, guiding=False))
    fargs = inject_nav_defaults("explain_route_segment", {}, nav, {})
    assert fargs.get("step_idx") is None, "안내 종료 후에도 step_idx 를 주입함"


def t_inject_transit_location():
    loc = {"lat": 37.39, "lng": 126.95}
    fargs = inject_nav_defaults("find_nearby_transit", {}, {}, loc)
    assert fargs["lat"] == 37.39 and fargs["lng"] == 126.95
    fargs2 = inject_nav_defaults("find_nearby_transit", {"place": "안양역"}, {}, loc)
    assert "lat" not in fargs2, "기준 장소를 말했는데 현재 위치를 덮어씌움"


# ── 4. find_nearby_transit 계약 ─────────────────────────────────────
TRANSIT_RESP = {
    "source": "db", "count": 2,
    "items": [
        {"type": "transit_stop", "name": "안양예술공원사거리", "dist_m": 85,
         "mobile_no": "09272", "center_yn": False,
         "accessible": None, "accessible_status": "unknown",
         "warnings": ["저상버스 정차 여부가 확인되지 않았습니다. 실시간 도착정보로 확인하세요."],
         "routes": [{"route_id": "241253001", "name": "2", "type": "마을버스",
                     "end_station": "신성중학교.씨엘포레자이아파트", "station_seq": [40]}]},
        {"type": "transit_station", "name": "안양역", "dist_m": 320,
         "accessible": True, "accessible_status": "yes", "warnings": []},
    ],
}


def _run(coro):
    # get_event_loop() 은 루프 없는 컨텍스트에서 3.12+ deprecated / 3.14 RuntimeError —
    # 호출마다 새 루프를 만드는 asyncio.run 으로 실행한다.
    return asyncio.run(coro)


def t_transit_tool_contract():
    async def fake_transit(lat, lng, radius_m=800, profile="wheelchair_manual"):
        return TRANSIT_RESP
    orig = route_client.transit_access
    route_client.transit_access = fake_transit
    try:
        r = _run(tool_handlers.tool_find_nearby_transit(lat=37.39, lng=126.95))
    finally:
        route_client.transit_access = orig
    assert r["status"] == "success" and r["count"] == 2
    stop = r["items"][0]
    assert stop["accessible"] is None, "미판정 accessible 이 변조됨"
    assert stop["accessible_status"] == "unknown"
    assert stop["routes"][0]["end_station"], "방면(종점명)이 빠짐"
    assert stop["routes"][0]["station_seq"] == [40], "경유 순번이 빠짐"
    ai = r["ai_instruction"]
    assert "이용 불가" in ai and "unknown" in ai, "미판정≠불가 지시가 없음"
    assert "station_seq" in ai or "순번" in ai, "순환 노선 방면 판별 지시가 없음"


def t_transit_tool_needs_location():
    r = _run(tool_handlers.tool_find_nearby_transit())
    assert r["status"] == "need_location"


def t_transit_tool_radius_clamped():
    captured = {}
    async def fake_transit(lat, lng, radius_m=800, profile="wheelchair_manual"):
        captured["radius"] = radius_m
        return {"items": []}
    orig = route_client.transit_access
    route_client.transit_access = fake_transit
    try:
        _run(tool_handlers.tool_find_nearby_transit(lat=1, lng=2, radius_m=99999))
    finally:
        route_client.transit_access = orig
    assert captured["radius"] == 2000, "반경 상한(2000m)이 안 걸림"


def t_dispatcher_has_new_tool():
    disp = tool_handlers.get_tool_dispatcher(embed_fn=None)
    assert "find_nearby_transit" in disp


if __name__ == "__main__":
    for nm, fn in sorted((k, v) for k, v in list(globals().items()) if k.startswith("t_")):
        check(nm, fn)
    print()
    if FAILS:
        print("FAILED: %d" % len(FAILS))
        sys.exit(1)
    print("all passed")
