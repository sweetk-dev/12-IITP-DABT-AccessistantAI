# -*- coding: utf-8 -*-
"""실시간 버스 도착·역 편의시설 도구 회귀 테스트 (v1.39.0).

    python3 tests/test_transit_live_tools.py

계약:
  1) get_bus_arrivals — 정류장 결정 순서: station_id > 안내 중 승차 정류장(세션 주입) > place > 현재 위치
  2) 저상 차량이 없을 때 "없다"가 아니라 "지금 오는 차 중엔 없다"로 안내한다
  3) 02 가 unavailable 이면 추측하지 않는다(status=unavailable)
  4) get_station_facilities — 출입구별 위치 문장화, 3상태(unknown ≠ no), 역 없음 사유 분리
  5) find_nearby_transit — 역 설비 필드(승강기 수·리프트·장애인화장실 3상태)를 버리지 않는다
  6) nav_state 의 board_station_id/board_route_id 가 get_bus_arrivals 인자로 주입된다
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
from nav_context import inject_nav_defaults, update_nav_state  # noqa: E402

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


ARRIVALS = {"status": "success", "station_id": "208000069", "items": [
    {"route_id": "208000096", "route_name": "51", "route_type": "일반형시내버스", "end_station": "충훈부",
     "vehicles": [{"predict_min": 9, "stops_away": 6, "low_floor": True, "plate_no": "경기71바1118"},
                  {"predict_min": 17, "stops_away": 14, "low_floor": True}]},
    {"route_id": "208000007", "route_name": "1", "route_type": None, "end_station": "구로디지털단지역(중)",
     "vehicles": [{"predict_min": 11, "stops_away": 10, "low_floor": False}]},
], "next_low_floor": {"route_id": "208000096", "route_name": "51", "route_type": "일반형시내버스",
                       "end_station": "충훈부", "predict_min": 9, "stops_away": 6, "plate_no": "경기71바1118"}}

NO_LOW = {"status": "success", "station_id": "1", "items": [
    {"route_id": "208000007", "route_name": "1", "vehicles": [{"predict_min": 3, "low_floor": False}]}],
    "next_low_floor": None}

FACILITIES = {"poi_id": "3900039", "name": "안양", "line": "1호선", "base_dt": "2026-09-01",
              "counts": {"elevator": 4, "escalator": 6, "wheelchair_lift": 0},
              "status": {"dis_slope": "yes", "dis_toilet": "yes", "gen_toilet": "yes",
                         "nursing_room": "unknown", "info_center": "unknown", "safety_plate": "yes"},
              "elevators": [{"exit_no": "2", "detail_loc": "(2F) 1번출구 맞이방 서쪽"},
                            {"exit_no": "내부", "detail_loc": "(1F) 관악역 방향 승강장 4-3 출입문앞"}],
              "lifts": [],
              "toilets": [{"gate_inout": "외", "exit_no": "2", "detail_loc": "(2층) 대합실내 북쪽 게이트 좌측",
                           "kind": "여자", "disabled": True},
                          {"gate_inout": "내", "exit_no": None, "detail_loc": "승강장", "kind": "남자", "disabled": False}],
              "platforms": [{"platform_no": "1", "updown": "상행", "safety_plate": "yes", "screen_door": "no",
                             "gap_min_cm": 9.5, "gap_max_cm": 11.0},
                            {"platform_no": "2", "updown": "하행", "safety_plate": "yes", "screen_door": "no",
                             "gap_min_cm": 8.0, "gap_max_cm": 13.5}]}

UNKNOWN_FAC = {"poi_id": "KRNA_1_MHK", "name": "명학", "line": "1호선",
               "counts": {"elevator": 4, "escalator": None, "wheelchair_lift": 0},
               "status": {"dis_slope": "unknown", "dis_toilet": "unknown", "gen_toilet": "unknown",
                          "nursing_room": "unknown", "info_center": "unknown", "safety_plate": "unknown"},
               "elevators": [{"exit_no": "1", "detail_loc": "(1F) 1번 출입구 및 성결대 셔틀 정류장 앞"}],
               "lifts": [], "toilets": [], "platforms": []}


class _Stub:
    def __init__(self, arrivals=ARRIVALS, access=None, fac=FACILITIES):
        self.arrivals, self.access, self.fac = arrivals, access, fac
        self.calls = []

    async def bus_arrivals(self, station_id, route_id=""):
        self.calls.append(("arrivals", station_id, route_id))
        return self.arrivals

    async def transit_access(self, lat, lng, radius_m=800, profile="wheelchair_manual"):
        self.calls.append(("access", lat, lng))
        return self.access if self.access is not None else {"items": [
            {"type": "transit_station", "poi_id": "3900039", "name": "안양", "dist_m": 50,
             "elevator_cnt": 4, "wheelchair_lift_cnt": 0, "dis_toilet_status": "yes", "line": "1호선",
             "accessible": True, "accessible_status": "yes", "warnings": [], "routes": []},
            {"type": "transit_stop", "poi_id": "208000069", "name": "안양역", "dist_m": 120,
             "mobile_no": "09213", "accessible": None, "accessible_status": "unknown",
             "warnings": ["저상버스 정차 여부가 확인되지 않았습니다."],
             "routes": [{"route_id": "208000096", "name": "51", "type": "일반형시내버스",
                         "end_station": "충훈부", "station_seq": [30]}]},
            {"type": "transit_station", "poi_id": "KRNA_1_MHK", "name": "명학", "dist_m": 700,
             "elevator_cnt": 4, "wheelchair_lift_cnt": 0, "dis_toilet_status": "unknown",
             "accessible": True, "accessible_status": "yes", "warnings": [], "routes": []},
        ]}

    async def station_facilities(self, stn_cd="", name=""):
        self.calls.append(("facilities", stn_cd, name))
        if self.fac is None:
            return {"status": "error", "message": "역을 찾을 수 없습니다 (poi_backend=db)"}
        return self.fac


def _with(stub, coro):
    saved = (route_client.bus_arrivals, route_client.transit_access, route_client.station_facilities)
    route_client.bus_arrivals = stub.bus_arrivals
    route_client.transit_access = stub.transit_access
    route_client.station_facilities = stub.station_facilities
    try:
        return _run(coro)
    finally:
        (route_client.bus_arrivals, route_client.transit_access,
         route_client.station_facilities) = saved


# ── get_bus_arrivals ───────────────────────────────────────
def t_arrivals_by_station_id_reports_next_low_floor():
    st = _Stub()
    r = _with(st, tool_handlers.tool_get_bus_arrivals(station_id="208000069", route_id="208000096",
                                                       station_name="안양역"))
    assert r["status"] == "success" and st.calls == [("arrivals", "208000069", "208000096")]
    assert r["next_low_floor"]["route_name"] == "51" and r["next_low_floor"]["predict_min"] == 9
    assert r["items"][0]["low_floor_soon"] is True
    assert "가장 빨리 오는 저상버스" in r["ai_instruction"] and "안양역" in r["ai_instruction"]
    assert "특정 노선" in r["ai_instruction"]


def t_arrivals_no_low_floor_wording():
    st = _Stub(arrivals=NO_LOW)
    r = _with(st, tool_handlers.tool_get_bus_arrivals(station_id="1"))
    assert r["status"] == "success" and r["next_low_floor"] is None
    assert "지금 오는 차량은 저상이 아니다" in r["ai_instruction"]
    assert "'저상버스가 없다'가 아니라" in r["ai_instruction"]


def t_arrivals_from_current_location_picks_nearest_stop():
    st = _Stub()
    r = _with(st, tool_handlers.tool_get_bus_arrivals(lat=37.40, lng=126.92))
    assert st.calls[0][0] == "access" and st.calls[1] == ("arrivals", "208000069", "")
    assert r["base_label"] == "안양역 정류장(09213)"


def t_arrivals_no_stop_nearby():
    st = _Stub(access={"items": [{"type": "transit_station", "poi_id": "X", "name": "역"}]})
    r = _with(st, tool_handlers.tool_get_bus_arrivals(lat=37.40, lng=126.92))
    assert r["status"] == "no_stop_nearby"


def t_arrivals_need_location():
    st = _Stub()
    r = _with(st, tool_handlers.tool_get_bus_arrivals())
    assert r["status"] == "need_location" and st.calls == []


def t_arrivals_unavailable_does_not_guess():
    st = _Stub(arrivals={"status": "unavailable", "reason": "HTTP 403", "items": [], "next_low_floor": None})
    r = _with(st, tool_handlers.tool_get_bus_arrivals(station_id="208000069"))
    assert r["status"] == "unavailable" and r["reason"] == "HTTP 403"
    assert "추측하지 마세요" in r["ai_instruction"]


def t_arrivals_route_error_passthrough():
    async def boom(station_id, route_id=""):
        return {"status": "error", "message": "경로 서비스에 연결하지 못했습니다", "ai_instruction": "x"}
    saved = route_client.bus_arrivals
    route_client.bus_arrivals = boom
    try:
        r = _run(tool_handlers.tool_get_bus_arrivals(station_id="1"))
    finally:
        route_client.bus_arrivals = saved
    assert r["status"] == "error"


# ── get_station_facilities ─────────────────────────────────
def t_facilities_text_and_three_state():
    st = _Stub()
    r = _with(st, tool_handlers.tool_get_station_facilities(station="안양역"))
    assert st.calls == [("facilities", "", "안양")], "역 접미사를 떼고 이름으로 조회"
    assert r["status"] == "success" and r["station"] == "안양" and r["line"] == "1호선"
    assert r["elevators"][0] == "2번 출입구 — (2F) 1번출구 맞이방 서쪽"
    assert r["elevators"][1].startswith("역사 내부 — ")
    assert r["dis_toilet_status"] == "yes"
    assert r["dis_toilets"] == ["2번 출입구 — 게이트 밖 (2층) 대합실내 북쪽 게이트 좌측"]
    assert r["safety_plate_status"] == "yes" and r["platform_gap_max_cm"] == 13.5
    assert r["elevator_cnt"] == 4 and r["wheelchair_lift_cnt"] == 0


def t_facilities_unknown_is_not_no():
    st = _Stub(fac=UNKNOWN_FAC)
    r = _with(st, tool_handlers.tool_get_station_facilities(station="명학"))
    assert r["dis_toilet_status"] == "unknown" and r["dis_slope_status"] == "unknown"
    assert r["platform_gap_max_cm"] is None and r["dis_toilets"] == []
    assert "'자료가 없다'" in r["ai_instruction"]


def t_facilities_not_found_vs_empty_name():
    st = _Stub(fac=None)
    r = _with(st, tool_handlers.tool_get_station_facilities(station="없는역"))
    assert r["status"] == "station_not_found" and "범계" in r["ai_instruction"]
    r = _with(st, tool_handlers.tool_get_station_facilities(station=""))
    assert r["status"] == "need_station"


# ── find_nearby_transit 보강 ───────────────────────────────
def t_nearby_transit_keeps_station_facility_fields():
    st = _Stub()
    r = _with(st, tool_handlers.tool_find_nearby_transit(lat=37.40, lng=126.92))
    by = {it["name"]: it for it in r["items"]}
    assert by["안양"]["elevator_cnt"] == 4 and by["안양"]["dis_toilet_status"] == "yes"
    assert by["명학"]["dis_toilet_status"] == "unknown"
    assert by["안양역"]["poi_id"] == "208000069" and "elevator_cnt" not in by["안양역"]
    assert "get_bus_arrivals" in r["ai_instruction"]


# ── nav_state 주입 ─────────────────────────────────────────
def t_nav_injects_board_stop_for_arrivals():
    nav = {}
    update_nav_state(nav, {"route_id": "r1", "guiding": True, "step_idx": 1, "total_steps": 4,
                           "leg_kind": "bus", "board_station_id": "208000069",
                           "board_route_id": "208000096", "board_stop_name": "안양역"})
    args = inject_nav_defaults("get_bus_arrivals", {}, nav, {"lat": 37.4, "lng": 126.9})
    assert args == {"station_id": "208000069", "route_id": "208000096", "station_name": "안양역"}
    # 사용자가 장소를 말했으면 세션 값을 덮지 않는다
    args = inject_nav_defaults("get_bus_arrivals", {"place": "범계역"}, nav, {"lat": 37.4, "lng": 126.9})
    assert args == {"place": "범계역"}
    # 안내 중이 아니면 현재 위치
    args = inject_nav_defaults("get_bus_arrivals", {}, {}, {"lat": 37.4, "lng": 126.9})
    assert args == {"lat": 37.4, "lng": 126.9}
    args = inject_nav_defaults("get_bus_arrivals", {"lat": 1, "lng": 2}, {}, {})
    assert args == {}, "세션이 모르는 좌표는 모델이 만든 것 — 버린다"


if __name__ == "__main__":
    for nm, fn in sorted((k, v) for k, v in list(globals().items()) if k.startswith("t_")):
        check(nm, fn)
    print()
    if FAILS:
        print("FAILED: %d" % len(FAILS))
        sys.exit(1)
    print("all passed")
