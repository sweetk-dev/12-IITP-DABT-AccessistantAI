# -*- coding: utf-8 -*-
"""이동 방식(mode) 선택·자동 추천 회귀 테스트 (#251).

    python3 tests/test_route_mode.py

계약:
  1) mode 미지정(자동) — 도보를 먼저 만들고, 도보가 멀면 도보+지하철·도보+버스·도보+버스+지하철을
     함께 만들어 (소요시간+승하차 부담)×방식 가중치로 비교한다(v2.20.0). 우선순위 도보 > 지하철 > 버스 > 버스+지하철
  2) 승격 실패(조합 없음)면 도보 결과를 그대로 쓴다 — 오류로 뒤집지 않는다
  3) 명시 mode 는 그대로 전달하고 폴백하지 않는다
  4) 알 수 없는 mode 는 도구가 직접 오류로 답한다(서버 호출 없이)
  5) 대중교통 결과의 legs 가 transit 요약(노선·방면·정거장)으로 압축된다
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


def check(name, fn):
    try:
        fn()
        print("  PASS  %s" % name)
    except AssertionError as e:
        FAILS.append(name)
        print("  FAIL  %s — %s" % (name, e))


def _run(coro):
    return asyncio.run(coro)


def _walk_resp(dist):
    return {"route_id": "r_walk", "routes": [{
        "summary": {"total_distance_m": dist, "duration_sec": dist, "max_slope_deg": 1.0,
                    "stairs_cnt": 0, "crossing_cnt": 0, "warnings": []},
        "geometry": [[37.39, 126.95]], "steps": []}], "fallback": {}}


def _transit_resp():
    return {"route_id": "r_mm", "mode": "walk_bus_subway", "routes": [{
        "summary": {"total_distance_m": 2400, "duration_sec": 1400, "walk_distance_m": 300,
                    "max_slope_deg": 1.0, "stairs_cnt": 0, "crossing_cnt": 0,
                    "eta_note": "소요시간은 추정입니다", "warnings": []},
        "geometry": [[37.39, 126.95]], "steps": [],
        "legs": [
            {"kind": "walk", "summary": {"total_distance_m": 100, "duration_sec": 90}},
            {"kind": "bus", "route": {"route_id": "241253001", "name": "2",
                                      "type": "마을버스", "end_station": "신성중"},
             "board": {"name": "안양박물관", "station_seq": 2},
             "alight": {"name": "안양역", "station_seq": 10}, "stop_cnt": 8,
             "warnings": ["저상버스 정차 여부는 보장되지 않습니다"]},
            {"kind": "subway", "line": "1호선", "board": {"name": "안양"},
             "alight": {"name": "명학"}, "station_cnt": 1, "warnings": []},
        ]}], "fallback": {}}


class _Recorder:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self.realtime = []

    async def __call__(self, origin, destination, profile="wheelchair_manual",
                       alternatives=1, mode="", realtime=False, low_floor=None):
        self.calls.append(mode)
        self.realtime.append(realtime)
        self.low_floor = getattr(self, "low_floor", []) + [low_floor]
        r = self.responses[len(self.calls) - 1]
        return r() if callable(r) else r


def _plan(mode="", rec=None, **extra):
    orig = route_client.plan_route
    route_client.plan_route = rec
    try:
        return _run(tool_handlers.tool_plan_accessible_route(
            destination_poi_id="TBF-1", origin_lat=37.39, origin_lng=126.95, mode=mode, **extra))
    finally:
        route_client.plan_route = orig


def t_auto_short_stays_walk():
    rec = _Recorder([_walk_resp(500)])
    r = _plan("", rec)
    assert rec.calls == ["walk"], "짧은 도보인데 추가 호출을 함: %s" % rec.calls
    assert r["mode_used"] == "walk" and r["auto_mode"] is True


ERR = {"status": "error", "message": "조합 없음"}


def _dur(resp, sec, dist=None):
    """대중교통 응답의 소요시간·총거리를 바꾼 사본."""
    import copy
    r = copy.deepcopy(resp)
    r["routes"][0]["summary"]["duration_sec"] = sec
    if dist is not None:
        r["routes"][0]["summary"]["total_distance_m"] = dist
    return r


def _only(kind):
    """지정한 대중교통 종류만 남긴 응답(legs 기준 실제 방식이 그 종류가 된다)."""
    r = _transit_resp()
    r["routes"][0]["legs"] = [l for l in r["routes"][0]["legs"] if l["kind"] in ("walk", kind)]
    return r


def t_auto_long_upgrades():
    # 도보 4,000m(4,000초) vs 버스+지하철 1,400초+승하차 2구간 360초 → 1,760×1.45=2,552 < 4,000 → 대중교통
    rec = _Recorder([_walk_resp(4000), ERR, ERR, _transit_resp()])
    r = _plan("", rec)
    assert rec.calls == ["walk", "walk_subway", "walk_bus", "walk_bus_subway"], rec.calls
    assert rec.realtime == [False, True, True, True], "대중교통 조합 요청에만 실시간을 붙인다"
    assert r["mode_used"] == "walk_bus_subway"
    assert r["mode_label"] == "도보+버스+지하철"
    sel = [c for c in r["auto_compare"] if c.get("status") == "선택"]
    assert sel and sel[0]["mode"] == "walk_bus_subway", r["auto_compare"]


def t_auto_keeps_walk_when_transit_not_clearly_faster():
    # 2026-10-22 1-A 유형 — 도보 1,419m(1,419초) vs 버스 1정거장 1,000초+승하차 180초 → 1,180×1.30=1,534
    # > 1,419 → 도보. 버스가 조금 빠른 정도로는 도보를 넘지 못한다(우선순위 가중치).
    rec = _Recorder([_walk_resp(1419), ERR, _dur(_only("bus"), 1000), ERR])
    r = _plan("", rec)
    assert r["mode_used"] == "walk", r["auto_compare"]


def t_auto_priority_subway_over_slightly_faster_bus():
    # 지하철 1,500초(+180)×1.15=1,932 vs 버스 1,450초(+180)×1.30=2,119 → 버스가 더 빨라도 지하철
    rec = _Recorder([_walk_resp(5000), _dur(_only("subway"), 1500), _dur(_only("bus"), 1450), ERR])
    r = _plan("", rec)
    assert r["mode_used"] == "walk_subway", r["auto_compare"]


def t_auto_excludes_excessive_detour():
    # 2026-10-22 결함 유형 — 도보 1,300m 에 버스 경로 총 20,161m(15배) → 후보에서 빼고 도보
    rec = _Recorder([_walk_resp(1300), ERR, _dur(_only("bus"), 300, dist=20161), ERR])
    r = _plan("", rec)
    assert r["mode_used"] == "walk"
    assert any(c.get("status") == "제외(우회 과다)" for c in r["auto_compare"]), r["auto_compare"]


def t_auto_same_actual_mode_compared_once():
    # walk_bus 와 walk_bus_subway 요청이 둘 다 버스만 쓴 결과를 주면 한 번만 비교한다
    rec = _Recorder([_walk_resp(5000), ERR, _only("bus"), _only("bus")])
    r = _plan("", rec)
    assert [c["mode"] for c in r["auto_compare"] if c.get("status") in ("후보", "선택")] == ["walk", "walk_bus"]


def t_auto_upgrade_failure_keeps_walk():
    rec = _Recorder([_walk_resp(2000),
                     {"status": "error", "message": "조합 없음"}])
    r = _plan("", rec)
    assert r["status"] == "success" and r["mode_used"] == "walk", \
        "승격 실패가 도보 결과를 뒤집음"


def t_explicit_mode_no_fallback():
    rec = _Recorder([{"status": "error", "message": "조합 없음",
                      "ai_instruction": "x"}])
    r = _plan("walk_bus", rec)
    assert rec.calls == ["walk_bus"]
    assert r["status"] == "error", "명시 모드 실패가 조용히 폴백됨"


def t_unknown_mode_rejected_without_call():
    rec = _Recorder([])
    r = _plan("taxi", rec)
    assert rec.calls == [] and r["status"] == "error"
    assert "이동 방식" in r["ai_instruction"]


def t_transit_brief_from_legs():
    rec = _Recorder([_transit_resp()])
    r = _plan("walk_bus_subway", rec)
    assert r["status"] == "success"
    kinds = [t["kind"] for t in r["transit"]]
    assert kinds == ["bus", "subway"]
    bus = r["transit"][0]
    assert bus["route_name"] == "2" and bus["route_type"] == "마을버스"
    assert bus["end_station"] == "신성중" and bus["stop_cnt"] == 8
    assert bus["board_seq"] == 2
    assert r["eta_note"] and "추정" in r["eta_note"]
    assert "저상버스" in r["ai_instruction"]
    assert r["low_floor_note"] is None, "실시간 응답이 없으면 확인 문구를 만들지 않는다"
    assert bus["board_station_id"] is None and bus["realtime_status"] is None
    assert r["summary"]["walk_distance_m"] == 300


def t_transit_brief_with_realtime_low_floor():
    resp = _transit_resp()
    bus = resp["routes"][0]["legs"][1]
    bus["board"]["poi_id"] = "208000069"
    bus["realtime"] = {"status": "success", "items": [{"route_id": "241253001"}],
                       "next_low_floor": {"route_id": "241253001", "route_name": "2",
                                          "route_type": "마을버스", "end_station": "신성중",
                                          "predict_min": 4, "stops_away": 2, "plate_no": "경기71바0001"}}
    sub = resp["routes"][0]["legs"][2]
    sub["board"]["facilities"] = {"elevators": [{"exit_no": "2", "detail_loc": "(2F) 1번출구 맞이방 서쪽"}],
                                  "lifts": [], "dis_toilet": "yes", "safety_plate": "unknown",
                                  "elevator_cnt": 4, "wheelchair_lift_cnt": 0}
    rec = _Recorder([resp])
    r = _plan("walk_bus_subway", rec)
    b = r["transit"][0]
    assert b["realtime_status"] == "success" and b["board_station_id"] == "208000069"
    assert b["next_low_floor"]["predict_min"] == 4 and b["next_low_floor"]["route_name"] == "2"
    assert "저상버스 2번이 약 4분" in r["low_floor_note"]
    assert "low_floor_note" in r["ai_instruction"]
    s = r["transit"][1]
    assert s["board_facilities"]["elevators"] == ["2번 출입구 — (2F) 1번출구 맞이방 서쪽"]
    assert s["board_facilities"]["dis_toilet"] == "yes"
    assert s["alight_facilities"] is None


def t_low_floor_passthrough_and_note():
    """저상버스 우선(#291, 02 v1.22.0) — 요청값을 그대로 전달하고, 판정 결과를 low_floor·low_floor_note 로 낸다."""
    resp = _transit_resp()
    resp["low_floor"] = {"mode": True, "tier": 1, "expanded_radius": False, "queried_at": 1757200000,
                         "valid_for_sec": 120, "wait_sec": 300}
    bus = resp["routes"][0]["legs"][1]
    bus["low_floor"] = {"tier": 1, "wait_sec": 300, "predict_min": 12, "stops_away": 8,
                        "plate_no": "경기71바0002", "route_name": "2", "source": "arrivals"}
    bus["warnings"] = ["저상버스 2번이 약 12분 뒤 도착 예정입니다(8 정거장 전) — 실시간 정보라 변동될 수 있습니다"]
    rec = _Recorder([resp])
    r = _plan("walk_bus_subway", rec, low_floor=True)
    assert rec.low_floor == [True], "low_floor 요청값이 경로 서버로 전달되지 않음"
    assert r["low_floor"]["mode"] is True and r["low_floor"]["tier"] == 1
    assert r["transit"][0]["low_floor_tier"] == 1
    assert r["low_floor_note"].startswith("저상버스 2번이 약 12분")
    assert "저상버스 우선으로 고른 경로" in r["ai_instruction"]
    assert "저상버스 대기를 더한 추정" in r["ai_instruction"]
    # off 로 요청하면 False 가 그대로 가고, 구버전 응답(low_floor 키 없음)도 깨지지 않는다
    rec = _Recorder([_transit_resp()])
    r = _plan("walk_bus", rec, low_floor=False)
    assert rec.low_floor == [False]
    assert r["low_floor"] == {"mode": False} and r["transit"][0]["low_floor_tier"] is None
    # 자동 추천 비교 요청에도 전달된다(대중교통 세 방식 모두)
    rec = _Recorder([_walk_resp(4000), ERR, ERR, resp])
    r = _plan("", rec, low_floor=True)
    assert rec.low_floor == [None, True, True, True]
    # 저상 없음(tier 3) — 문구는 leg 경고 첫 줄
    resp3 = _transit_resp()
    resp3["low_floor"] = {"mode": True, "tier": 3}
    resp3["routes"][0]["legs"][1]["low_floor"] = {"tier": 3, "route_name": "2"}
    resp3["routes"][0]["legs"][1]["warnings"] = ["현재 운행 중인 저상버스가 없습니다 — 이 구간은 일반 버스 기준 안내입니다"]
    r = _plan("walk_bus", _Recorder([resp3]))
    assert r["low_floor_note"].startswith("현재 운행 중인 저상버스가 없습니다")


def t_mode_label_follows_actual_legs():
    """walk_bus_subway 로 요청해도 플래너가 버스 직결만 고르면 라벨은 도보+버스여야 한다."""
    resp = _transit_resp()
    resp["routes"][0]["legs"] = [l for l in resp["routes"][0]["legs"] if l["kind"] != "subway"]
    rec = _Recorder([resp])
    r = _plan("walk_bus_subway", rec)
    assert rec.calls == ["walk_bus_subway"]
    assert r["mode_used"] == "walk_bus" and r["mode_label"] == "도보+버스"
    assert r["mode_requested"] == "walk_bus_subway"
    assert "지하철을 언급하지 마세요" in r["ai_instruction"]
    # 지하철 구간이 실제로 있으면 그대로
    rec = _Recorder([_transit_resp()])
    r = _plan("walk_bus_subway", rec)
    assert r["mode_used"] == "walk_bus_subway" and "언급하지 마세요" not in r["ai_instruction"]
    # 자동 추천 승격 경로도 같다
    resp2 = _transit_resp(); resp2["routes"][0]["legs"] = resp2["routes"][0]["legs"][:2]
    rec = _Recorder([_walk_resp(4000), ERR, ERR, resp2])
    r = _plan("", rec)
    assert r["mode_used"] == "walk_bus" and r["auto_mode"] is True


def t_transit_brief_realtime_no_low_floor():
    resp = _transit_resp()
    bus = resp["routes"][0]["legs"][1]
    bus["realtime"] = {"status": "success", "items": [{"route_id": "241253001"}], "next_low_floor": None}
    rec = _Recorder([resp])
    r = _plan("walk_bus", rec)
    assert r["transit"][0]["next_low_floor"] is None
    assert "저상버스가 아닙니다" in r["low_floor_note"]


def t_transit_brief_realtime_unavailable_keeps_old_wording():
    resp = _transit_resp()
    resp["routes"][0]["legs"][1]["realtime"] = {"status": "unavailable", "reason": "HTTP 403",
                                                "items": [], "next_low_floor": None}
    rec = _Recorder([resp])
    r = _plan("walk_bus", rec)
    assert r["low_floor_note"] is None
    assert r["transit"][0]["realtime_status"] == "unavailable"
    assert "실시간 도착정보 확인이 필요" in r["ai_instruction"]


def t_walk_subway_mode_passthrough_and_label():
    """walk_subway(#294, 02 v1.25.0) — 그대로 전달하고, 지하철만 있는 결과의 라벨은 '도보+지하철'이다."""
    resp = _transit_resp()
    resp["mode"] = "walk_subway"
    resp["routes"][0]["legs"] = [l for l in resp["routes"][0]["legs"] if l["kind"] != "bus"]
    rec = _Recorder([resp])
    r = _plan("walk_subway", rec)
    assert rec.calls == ["walk_subway"], rec.calls
    assert r["status"] == "success"
    assert r["mode_used"] == "walk_subway" and r["mode_label"] == "도보+지하철"
    assert [t["kind"] for t in r["transit"]] == ["subway"]
    # 버스가 섞인 결과는 여전히 버스+지하철
    rec = _Recorder([_transit_resp()])
    r = _plan("walk_bus_subway", rec)
    assert r["mode_used"] == "walk_bus_subway"
    # 알 수 없는 방식 안내문에 도보+지하철이 들어간다
    r = _plan("taxi", _Recorder([]))
    assert "도보+지하철" in r["ai_instruction"]


def t_default_profile_is_electric():
    """기본 프로필은 전동 휠체어(#294) — 도구가 프로필을 받지 못하면 전동으로 부른다."""
    assert tool_handlers.DEFAULT_PROFILE == "wheelchair_electric"
    seen = {}

    async def rec(origin, destination, profile="x", alternatives=1, mode="", realtime=False, low_floor=None):
        seen["profile"] = profile
        return _walk_resp(500)
    orig = route_client.plan_route
    route_client.plan_route = rec
    try:
        _run(tool_handlers.tool_plan_accessible_route(
            destination_poi_id="TBF-1", origin_lat=37.39, origin_lng=126.95))
    finally:
        route_client.plan_route = orig
    assert seen["profile"] == "wheelchair_electric", seen


def t_call_log_summary_and_ctx():
    """계측 로그(#294) — 요청 문맥·응답 요약만 남고, 좌표 본문은 남지 않는다."""
    body = {"origin": {"lat": 1, "lng": 2}, "destination": {"type": "tour", "poi_id": "TBF-1"},
            "profile": "wheelchair_electric", "mode": "walk_subway"}
    resp = _transit_resp()
    out = route_client._summarize_for_log("/route/plan", body, resp)
    assert out["profile"] == "wheelchair_electric" and out["mode"] == "walk_subway"
    assert out["route_id"] == "r_mm" and out["total_m"] == 2400 and out["walk_m"] == 300
    assert "origin" not in out
    out = route_client._summarize_for_log("/route/plan", {"profile": "p"}, None)
    assert out["mode"] == "walk" and "route_id" not in out
    rec = route_client._summarize_for_log("/tour/recommend",
                                          {"disabilities": ["지체장애"], "sigungu": "안양", "topk": 5},
                                          {"total": 2, "items": [{"poi_id": 1, "score": 1.0, "name": "x"}]})
    assert rec["items"] == [{"poi_id": "1", "score": 1.0}] and rec["total"] == 2
    route_client.set_log_ctx({"reason": "off_route", "prev_route_id": "r_old", "empty": ""})
    ctx = route_client._log_ctx()
    assert ctx == {"reason": "off_route", "prev_route_id": "r_old"}, ctx
    route_client.set_log_ctx(None)
    assert route_client._log_ctx() == {}


def t_realtime_low_floor_without_predict_min_does_not_fail():
    """실시간 응답의 predict_min 이 None 이어도 경로 안내가 실패하지 않는다 — 분을 말하지 않을 뿐이다."""
    resp = _transit_resp()
    bus = resp["routes"][0]["legs"][1]
    bus["board"]["poi_id"] = "208000069"
    bus["realtime"] = {"status": "success", "items": [{"route_id": "241253001"}],
                       "next_low_floor": {"route_id": "241253001", "route_name": "2",
                                          "predict_min": None, "stops_away": 2}}
    r = _plan("walk_bus_subway", _Recorder([resp]))
    assert r["status"] == "success", r
    note = r["low_floor_note"]
    assert note and "저상버스 2번" in note and "None" not in note and "분 뒤" not in note, note
    assert r["transit"][0]["next_low_floor"]["predict_min"] is None

    # 숫자면 종전 문구 그대로
    bus["realtime"]["next_low_floor"]["predict_min"] = 4
    r = _plan("walk_bus_subway", _Recorder([resp]))
    assert r["low_floor_note"] == "승차 정류장에 저상버스 2번이 약 4분 뒤 도착 예정", r["low_floor_note"]



def t_ui_action_carries_requested_mode():
    # 화면의 이동 방식 카드가 말로 바꾼 방식을 따라가도록 — 자동이면 None, 명시면 그 방식
    r = _plan("", _Recorder([_walk_resp(500)]))
    assert r["ui_action"]["mode_requested"] is None, r["ui_action"].get("mode_requested")
    r = _plan("walk", _Recorder([_walk_resp(1100)]))
    assert r["ui_action"]["mode_requested"] == "walk", r["ui_action"].get("mode_requested")
    r = _plan("walk_bus", _Recorder([_transit_resp()]))
    assert r["ui_action"]["mode_requested"] == "walk_bus", r["ui_action"].get("mode_requested")


if __name__ == "__main__":
    for nm, fn in sorted((k, v) for k, v in list(globals().items()) if k.startswith("t_")):
        check(nm, fn)
    print()
    if FAILS:
        print("FAILED: %d" % len(FAILS))
        sys.exit(1)
    print("all passed")
