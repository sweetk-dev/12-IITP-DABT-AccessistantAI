# -*- coding: utf-8 -*-
"""이동 방식(mode) 선택·자동 추천 회귀 테스트 (#251).

    python3 tests/test_route_mode.py

계약:
  1) mode 미지정(자동) — 도보를 먼저 만들고, 도보가 멀면 대중교통 조합으로 승격
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

    async def __call__(self, origin, destination, profile="wheelchair_manual",
                       alternatives=1, mode=""):
        self.calls.append(mode)
        r = self.responses[len(self.calls) - 1]
        return r() if callable(r) else r


def _plan(mode="", rec=None):
    orig = route_client.plan_route
    route_client.plan_route = rec
    try:
        return _run(tool_handlers.tool_plan_accessible_route(
            destination_poi_id="TBF-1", origin_lat=37.39, origin_lng=126.95, mode=mode))
    finally:
        route_client.plan_route = orig


def t_auto_short_stays_walk():
    rec = _Recorder([_walk_resp(500)])
    r = _plan("", rec)
    assert rec.calls == ["walk"], "짧은 도보인데 추가 호출을 함: %s" % rec.calls
    assert r["mode_used"] == "walk" and r["auto_mode"] is True


def t_auto_long_upgrades():
    rec = _Recorder([_walk_resp(2000), _transit_resp()])
    r = _plan("", rec)
    assert rec.calls == ["walk", "walk_bus_subway"]
    assert r["mode_used"] == "walk_bus_subway"
    assert r["mode_label"] == "도보+버스+지하철"


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
    assert r["summary"]["walk_distance_m"] == 300


if __name__ == "__main__":
    for nm, fn in sorted((k, v) for k, v in list(globals().items()) if k.startswith("t_")):
        check(nm, fn)
    print()
    if FAILS:
        print("FAILED: %d" % len(FAILS))
        sys.exit(1)
    print("all passed")
