# -*- coding: utf-8 -*-
"""리프트만 있는 승강장 주의 — 경로 도구 결과·자동 추천 회귀 테스트 (v2.0.8, 경로 서비스 v1.35.0).

    python3 tests/test_platform_caution.py

배경: 안양 → 김중업건축박물관 도보+지하철 경로가 관악역 상행(석수 방향) 승강장에 내리게 했는데, 그 승강장에는
승강기가 없고 휠체어리프트만 있다. 경로 서비스 v1.35.0 이 경고(summary.platform_access·warnings)를 싣는다.

계약:
  1) 경로 도구 결과에 platform_cautions(경고 문장 목록)가 실리고, ai_instruction 이 요약보다 먼저 말하라고 지시한다
  2) 지하철 구간 요약(transit[].platform_cautions)에도 그 구간의 경고가 실린다
  3) 역 안 출발 경로(station_start 스텝의 platform_access)도 platform_cautions 로 모인다
  4) 자동 추천: 대중교통이 리프트 승강장을 거치고 덜 걷는 거리가 감점 이하이면 도보를 유지한다
  5) 자동 추천: 리프트 경고가 없거나, 덜 걷는 거리가 감점보다 크면 종전대로 대중교통으로 승격한다
  6) 구버전 경로 응답(키 없음)은 빈 목록 — 지시문도 붙지 않는다
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_route_mode import _Recorder, _plan, _transit_resp, _walk_resp, check, FAILS  # noqa: E402
import tool_handlers  # noqa: E402

WARN = ("관악역 하차 승강장(석수 방향)에는 대합실로 이어지는 승강기가 없고 휠체어리프트만 있습니다"
        "(폭 800mm·길이 1,100mm). 승강장의 리프트 호출 버튼을 누르거나 역무실에 연락해 역무원을 불러 주세요.")
PA = {"station": "관악", "side": "alight", "access": "lift_only", "toward": "석수", "updown": "상행",
      "lift": {"detail_loc": "1F(석수역 방향 상행승강장 계단 옆)", "width_mm": 800, "length_mm": 1100},
      "warning": WARN}


def _lift_transit(walk_m=1310, penalty=1500):
    """경로 서비스 v1.35.0 형식 — 안양 → 관악(북행) 지하철, 하차 승강장 리프트뿐."""
    r = _transit_resp()
    route = r["routes"][0]
    route["summary"].update({"walk_distance_m": walk_m, "warnings": [WARN],
                             "platform_access": [PA], "platform_access_penalty_m": penalty})
    route["legs"] = [
        {"kind": "walk", "summary": {"total_distance_m": 17, "duration_sec": 15}},
        {"kind": "subway", "line": "1호선", "board": {"name": "안양"}, "alight": {"name": "관악"},
         "station_cnt": 1, "warnings": [WARN],
         "platform_access": {"board": {"station": "안양", "side": "board", "access": "elevator"}, "alight": PA}},
        {"kind": "walk", "summary": {"total_distance_m": walk_m - 17, "duration_sec": 1175}},
    ]
    return r


def t_explicit_subway_result_carries_cautions():
    r = _plan("walk_subway", _Recorder([_lift_transit()]))
    assert r["status"] == "success", r
    assert r["platform_cautions"] == [WARN], r["platform_cautions"]
    sub = [t for t in r["transit"] if t["kind"] == "subway"][0]
    assert sub["platform_cautions"] == [WARN]
    ins = r["ai_instruction"]
    assert "platform_cautions 가 있으면 경로 요약보다 먼저" in ins, ins
    # 지시문의 순서 — 승강장 주의가 거리·시간 요약 지시보다 앞에 온다
    assert ins.index("platform_cautions") < ins.index("총 거리·예상 시간"), ins
    assert r["auto_kept_walk"] is None


def t_station_start_cautions_collected():
    resp = _walk_resp(900)
    resp["station_start"] = {"station": "관악", "travel": "north", "exit": {"exit_no": "2"},
                             "egress": {"inside": ["내린 승강장 쪽에는 휠체어리프트만 있습니다 — 1F(석수역 방향 상행승강장 계단 옆)"
                                                   "(폭 800mm·길이 1,100mm). 승강장의 리프트 호출 버튼을 누르거나 역무실에 "
                                                   "연락해 역무원을 불러 주세요"]}}
    resp["routes"][0]["steps"] = [{"maneuver": "station_start", "instruction": "관악역 안에서 출발합니다.",
                                   "warnings": [WARN], "platform_access": [PA]}]
    rec = _Recorder([resp])

    async def _rec(origin, destination, profile="wheelchair_manual", alternatives=1, mode="",
                   realtime=False, low_floor=None, origin_station=None):
        return await rec(origin, destination, profile, alternatives, mode, realtime, low_floor)
    import route_client
    orig = route_client.plan_route
    route_client.plan_route = _rec
    try:
        import asyncio
        r = asyncio.run(tool_handlers.tool_plan_accessible_route(
            destination_poi_id="TBF-1", origin_lat=37.39, origin_lng=126.95,
            origin_station="관악", origin_travel="north"))
    finally:
        route_client.plan_route = orig
    assert r["status"] == "success", r
    assert r["platform_cautions"] == [WARN], r["platform_cautions"]
    assert "역무원을 불러 주세요" in r["station_start"]["inside"][0]


def t_auto_keeps_walk_when_lift_only_and_saving_small():
    # 도보 2,500m vs 지하철 경로 도보 1,310m — 아끼는 1,190m 가 감점 1,500m 이하 → 도보 유지
    rec = _Recorder([_walk_resp(2500), _lift_transit()])
    r = _plan("", rec)
    assert rec.calls == ["walk", "walk_bus_subway"], rec.calls
    assert r["mode_used"] == "walk" and r["auto_mode"] is True, r["mode_used"]
    assert r["auto_kept_walk"] == [WARN]
    assert r["platform_cautions"] == [], "도보 경로에는 승강장 주의가 없다"
    assert "자동 추천이 도보를 고른 이유" in r["ai_instruction"]


def t_auto_upgrades_when_saving_exceeds_penalty():
    # 도보 4,000m vs 1,310m — 아끼는 2,690m > 1,500m → 경고와 함께 대중교통으로 승격
    rec = _Recorder([_walk_resp(4000), _lift_transit()])
    r = _plan("", rec)
    assert r["mode_used"] == "walk_subway", r["mode_used"]
    assert r["platform_cautions"] == [WARN] and r["auto_kept_walk"] is None


def t_auto_upgrades_without_lift_flags_as_before():
    rec = _Recorder([_walk_resp(1000), _transit_resp()])
    r = _plan("", rec)
    assert r["mode_used"] == "walk_bus_subway", "리프트 경고가 없으면 종전처럼 승격"
    assert r["platform_cautions"] == [] and r["auto_kept_walk"] is None
    assert "platform_cautions 가 있으면" not in r["ai_instruction"]


def t_penalty_default_when_value_missing():
    t = _lift_transit(penalty=None)
    w = _walk_resp(2500)
    assert tool_handlers._lift_only_keeps_walk(w, t) is True        # 기본 1,500m 로 판정
    assert tool_handlers._lift_only_keeps_walk(_walk_resp(3000), t) is False


def t_transit_walk_falls_back_to_leg_sum():
    # summary.walk_distance_m 이 없는 응답 — 도보 구간 거리 합(17 + 1,293 = 1,310m)으로 판정한다
    t = _lift_transit()
    del t["routes"][0]["summary"]["walk_distance_m"]
    assert tool_handlers._lift_only_keeps_walk(_walk_resp(2500), t) is True     # 1,190m ≤ 1,500m
    assert tool_handlers._lift_only_keeps_walk(_walk_resp(3000), t) is False    # 1,690m > 1,500m
    # 도보 구간 거리도 모르면 판정하지 않는다(승격 유지)
    for leg in t["routes"][0]["legs"]:
        if leg["kind"] == "walk":
            leg["summary"].pop("total_distance_m")
    assert tool_handlers._lift_only_keeps_walk(_walk_resp(2500), t) is False


def t_unknown_or_zero_walk_does_not_keep_walk():
    t = _lift_transit()
    w = _walk_resp(0)
    assert tool_handlers._lift_only_keeps_walk(w, t) is False
    del w["routes"][0]["summary"]["total_distance_m"]
    assert tool_handlers._lift_only_keeps_walk(w, t) is False


if __name__ == "__main__":
    for nm, fn in sorted((k, v) for k, v in list(globals().items()) if k.startswith("t_")):
        check(nm, fn)
    print()
    if FAILS:
        print("FAILED: %d" % len(FAILS))
        sys.exit(1)
    print("all passed")
