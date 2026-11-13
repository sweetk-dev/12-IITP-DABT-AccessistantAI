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
