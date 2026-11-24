# -*- coding: utf-8 -*-
"""역 승강기는 하나만 — 길안내 중이면 경로에서 탈(내릴) 승강기 (v2.29.6). 외부 호출 없음."""
import asyncio
import os
import sys
import types as _t
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ROUTE_API_BASE_URL", "http://route-api:18100")
os.environ.setdefault("FEATURE_ROUTE", "1")
os.environ.setdefault("FEATURE_TOUR", "1")
for _real in ("sqlalchemy", "sqlalchemy.ext.asyncio", "database", "models"):
    try:
        __import__(_real)
    except Exception:                          # noqa: BLE001
        break
for name, attrs in (
    ("sqlalchemy", {"select": lambda *a, **k: None, "or_": lambda *a, **k: None,
                    "text": lambda *a, **k: None, "func": _t.SimpleNamespace()}),
    ("sqlalchemy.ext", {}),
    ("sqlalchemy.ext.asyncio", {"AsyncSession": object}),
    ("database", {"AsyncSessionLocal": None, "get_db": None, "engine": None}),
    ("models", {"WelfarePolicy": object, "PolicyChunk": object}),
):
    if name not in sys.modules:
        m = _t.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m

import nav_context as nc        # noqa: E402
import tool_handlers as th      # noqa: E402

ING = {"station": "명학", "inside": ["2번 출구 승강기로 역에 들어갑니다 — 2번 출구 앞",
                                    "개찰구를 지나 안양 방향 승강장 승강기를 탑니다 — 승강장 10-4 앞",
                                    "10번째 칸에 타면 내릴 때 승강기가 가깝습니다"], "car": "10번째 칸에 타세요"}
EG = {"station": "안양", "exit_no": "1", "where": None,
      "inside": ["내린 승강장의 승강기로 이동합니다 — 승강장 1-1 앞", "1번 출구 승강기로 나갑니다 — 1번 출구"],
      "outside": None}


def _nav(**kw):
    nav = {}
    nc.update_nav_state(nav, dict({"route_id": "r1", "guiding": True, "step_idx": 3, "total_steps": 9}, **kw))
    return nav


def test_board_side_picks_platform_elevator():
    nav = _nav(station_ingress=ING, station_egress=EG)
    r = nc.route_elevator(nav)
    assert r["side"] == "board" and r["station"] == "명학"
    assert r["text"] == "개찰구를 지나 안양 방향 승강장 승강기를 탑니다 — 승강장 10-4 앞"


def test_named_station_picks_matching_side():
    nav = _nav(station_ingress=ING, station_egress=EG)
    assert nc.route_elevator(nav, "안양역")["text"] == "내린 승강장의 승강기로 이동합니다 — 승강장 1-1 앞"
    assert nc.route_elevator(nav, "명학역")["side"] == "board"
    assert nc.route_elevator(nav, "범계") is None


def test_no_route_info_gives_none_and_cleaning():
    assert nc.route_elevator(_nav()) is None
    nav = _nav(station_ingress={"station": "명학", "inside": []})
    assert nav["station_ingress"] is None


def test_injection_fills_station_and_route_elevator():
    nav = _nav(station_ingress=ING, station_egress=EG)
    f = nc.inject_nav_defaults("get_station_facilities", {}, nav, {})
    assert f["station"] == "명학" and f["route_elevator"]["side"] == "board"
    f = nc.inject_nav_defaults("get_station_facilities", {"station": "범계", "route_elevator": {"text": "지어낸 것"}}, nav, {})
    assert "route_elevator" not in f, "경로에 없는 역이면 모델이 넣은 값도 지운다"


def _run_tool(monkeypatch, **kw):
    async def fac(name=""):
        return {"name": name, "line": "1호선", "counts": {"elevator": 4},
                "elevators": [{"exit_no": "1", "detail_loc": "1번 출구"}, {"exit_no": "2", "detail_loc": "2번 출구"}]}
    monkeypatch.setattr(th.route_client, "station_facilities", fac)
    return asyncio.run(th.tool_get_station_facilities(**kw))


def test_tool_answers_route_elevator_only(monkeypatch):
    re_ = {"side": "board", "station": "명학", "text": "개찰구를 지나 안양 방향 승강장 승강기를 탑니다 — 승강장 10-4 앞"}
    r = _run_tool(monkeypatch, station="명학", route_elevator=re_)
    assert r["route_elevator"] == re_
    assert "route_elevator.text" in r["ai_instruction"] and "하나만" in r["ai_instruction"]
    assert "2~3개" not in r["ai_instruction"]


def test_tool_without_route_says_one(monkeypatch):
    r = _run_tool(monkeypatch, station="명학")
    assert r["route_elevator"] is None
    assert "하나만 말하세요" in r["ai_instruction"] and "어느 방면으로 타세요?" in r["ai_instruction"]
    assert "2~3개" not in r["ai_instruction"]


def test_declaration_station_optional_and_instruction():
    lb = pytest.importorskip("live_bridge")
    decls = {d.name: d for t in lb.build_tool_declarations() if t is not None
             for d in (getattr(t, "function_declarations", None) or [])}
    assert not (decls["get_station_facilities"].parameters.required or [])
    assert "엘리베이터는 **하나만** 말합니다" in lb.SYSTEM_INSTRUCTION
    assert "출입구별 위치를 2~3개만" not in lb.SYSTEM_INSTRUCTION


# ── v2.34.0: 화면이 역 안내를 보내기 전 — 대화로 만든 경로에서 탈 칸·승강장 방향 ──
ROUTE_RESULT = {"status": "success", "route_id": "r_k", "ui_action": {"action": "show_route", "route": {"routes": [{
    "steps": [{"maneuver": "subway_board", "ingress": {
        "station": "관악", "inside": ["관악역 2번 출구 승강기로 맞이방에 올라갑니다",
                                     "안양역 방면 승강기 전용 개찰구로 들어가 승강기로 내려갑니다 — 승강장 1-1 앞"],
        "car": {"text": "7-2까지 이동해 타면 안양역에서 내리자마자 승강기입니다"}}},
              {"maneuver": "subway_alight"}],
    "legs": [{"kind": "subway", "board": {"name": "관악"}, "alight": {"name": "안양"},
              "platform_access": {"board": {"updown": "하행"}, "alight": {"updown": "하행"}},
              "egress": {"station": "안양", "inside": ["내린 승강장 7-2 앞 승강기로 올라갑니다", "1번 출구 승강기로 나갑니다"]}}]}]}}}


def test_seed_route_stations_gives_car_and_updown_without_screen():
    nav = {}
    nc.note_new_route(nav, "r_k")
    nc.seed_route_stations(nav, ROUTE_RESULT)
    r = nc.route_elevator(nav, "관악역")
    assert r["side"] == "board" and r["car"].startswith("7-2까지") and r["updown"] == "하행"
    assert r["text"].endswith("승강장 1-1 앞")
    a = nc.route_elevator(nav, "안양")
    assert a["side"] == "alight" and a["text"].startswith("내린 승강장 7-2") and a["updown"] == "하행"
    f = nc.inject_nav_defaults("get_station_facilities", {}, nav, {})
    assert f["station"] == "관악" and f["route_elevator"]["car"]


def test_screen_ingress_wins_and_keeps_updown():
    nav = {}
    nc.note_new_route(nav, "r_k")
    nc.seed_route_stations(nav, ROUTE_RESULT)
    nc.update_nav_state(nav, {"route_id": "r_k", "guiding": False, "step_idx": 0, "total_steps": 3,
                              "station_ingress": {"station": "관악", "inside": ["화면 문장 — 승강장 1-1 앞"], "car": None}})
    r = nc.route_elevator(nav, "관악")
    assert r["text"] == "화면 문장 — 승강장 1-1 앞" and r["updown"] == "하행"
    assert r["car"].startswith("7-2까지"), "화면이 칸을 안 보냈으면 경로의 칸을 쓴다"


def test_new_route_drops_old_stations_keeps_dial_numbers():
    nav = {"dial_numbers": [{"tel": "1666-0420", "digits": "16660420", "name": "센터"}]}
    nc.note_new_route(nav, "r_k")
    nc.seed_route_stations(nav, ROUTE_RESULT)
    nc.note_new_route(nav, "r_other")
    assert "route_stations" not in nav and nav["dial_numbers"][0]["digits"] == "16660420"
    assert nc.route_elevator(nav, "관악") is None


def _run_fac(monkeypatch, **kw):
    async def fac(name=""):
        return {"name": name, "line": "1호선", "counts": {"elevator": 3},
                "elevators": [{"exit_no": "2", "detail_loc": "2번 출구 옆"}],
                "status": {"safety_plate": "yes"},
                "platforms": [{"platform_no": "1", "updown": "상행", "safety_plate": "yes", "gap_min_cm": 7.4, "gap_max_cm": 15.3},
                              {"platform_no": "2", "updown": "하행", "safety_plate": "no", "gap_min_cm": 6.0, "gap_max_cm": 10.8}]}
    monkeypatch.setattr(th.route_client, "station_facilities", fac)
    return asyncio.run(th.tool_get_station_facilities(**kw))


def test_gap_uses_route_platform_and_car(monkeypatch):
    re_ = {"side": "board", "station": "관악", "text": "승강장 1-1 앞", "updown": "하행",
           "car": "7-2까지 이동해 타면 안양역에서 내리자마자 승강기입니다"}
    r = _run_fac(monkeypatch, station="관악", route_elevator=re_)
    assert r["route_platform"]["gap_max_cm"] == 10.8 and r["route_platform"]["updown"] == "하행"
    ai = r["ai_instruction"]
    assert "route_platform" in ai and "platform_gap_max_cm 은 말하지 마세요" in ai
    assert "route_elevator.car 를 그대로" in ai


def test_gap_without_route_asks_direction(monkeypatch):
    r = _run_fac(monkeypatch, station="관악")
    assert r["route_platform"] is None and r["platform_gap_max_cm"] == 15.3
    ai = r["ai_instruction"]
    assert "그대로 말하지 마세요" in ai and "어느 방면으로 타는지 먼저" in ai
    assert "내릴 역 승강기에 맞춘 칸을 알려 드릴 수 있다" in ai


def test_dial_only_known_numbers():
    nav = {}
    nc.note_dial_numbers(nav, {"items": [{"name": "안양시 교통약자이동지원센터", "tel": "1666-0420 / 031-857-0420(시내 직통)"}]})
    assert [x["digits"] for x in nav["dial_numbers"]] == ["16660420", "0318570420"]
    ok = nc.dial_phone_result(nav, "1666-0420")
    assert ok["status"] == "success" and ok["ui_action"] == {"action": "dial", "tel": "16660420", "name": "안양시 교통약자이동지원센터"}
    assert "통화 버튼을 누르시면" in ok["ai_instruction"]
    assert nc.dial_phone_result(nav, "031-857-0420")["ui_action"]["tel"] == "0318570420"
    bad = nc.dial_phone_result(nav, "02-123-4567")
    assert bad["status"] == "unknown_number" and "ui_action" not in bad
    assert nc.dial_phone_result({}, "1666-0420")["status"] == "unknown_number"
