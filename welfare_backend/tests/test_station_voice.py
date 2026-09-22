# -*- coding: utf-8 -*-
"""역 안/밖·출구 확인을 말로 (v1.52.0).

    python3 tests/test_station_voice.py

계약:
  1) 화면이 기다리는 것(station_wait)은 세션이 주입한다 — 모델이 넘긴 값은 덮어쓴다
  2) 기다리는 것이 없으면 화면을 건드리지 않는다(ui_action 없음, status=idle)
  3) 나가는 중(exiting)이면 재촉하지 말라는 지시와 함께 화면 동작만 알린다
  4) 안내 시작 질문에 '역 안'만 답하면 타고 온 방향 선택지를 물으라고 지시한다
  5) nav_state 의 station_wait 는 허용 종류·필드만 남긴다
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
os.environ.setdefault("FEATURE_TOUR", "1")

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

import tool_handlers                                              # noqa: E402
from nav_context import inject_nav_defaults, update_nav_state      # noqa: E402

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


ASK = {"kind": "ask_station", "station": "관악",
       "choices": [{"travel": "south", "label": "석수·서울 쪽에서 타고 왔어요"},
                   {"travel": "north", "label": "안양·수원 쪽에서 타고 왔어요"}]}


def t_inject_overrides_model_value():
    nav = {"station_wait": {"kind": "exit", "station": "관악", "choices": []}}
    fargs = inject_nav_defaults("report_station_position", {"where": "outside", "station_wait": {"kind": "undo"}}, nav, {})
    assert fargs["station_wait"]["kind"] == "exit"
    fargs = inject_nav_defaults("report_station_position", {"where": "outside"}, {}, {})
    assert fargs["station_wait"] is None


def t_no_wait_no_screen_change():
    r = _run(tool_handlers.tool_report_station_position(where="outside", station_wait=None))
    assert r["status"] == "idle" and "ui_action" not in r


def t_exiting_does_not_rush():
    r = _run(tool_handlers.tool_report_station_position(
        where="exiting", station_wait={"kind": "exit", "station": "관악", "choices": []}))
    assert r["status"] == "success" and r["ui_action"] == {"action": "station_position", "where": "exiting", "travel": None}
    assert "재촉하지" in r["ai_instruction"]


def t_outside_moves_on():
    r = _run(tool_handlers.tool_report_station_position(
        where="outside", station_wait={"kind": "exit", "station": "관악", "choices": []}))
    assert r["ui_action"]["where"] == "outside" and "걸어서" in r["ai_instruction"]


def t_inside_without_travel_asks_direction():
    r = _run(tool_handlers.tool_report_station_position(where="inside", station_wait=ASK))
    assert r["ui_action"]["where"] == "inside" and r["ui_action"]["travel"] is None
    assert "석수·서울 쪽에서 타고 왔어요" in r["ai_instruction"] and "travel" in r["ai_instruction"]
    r2 = _run(tool_handlers.tool_report_station_position(where="inside", travel="SOUTH", station_wait=ASK))
    assert r2["ui_action"]["travel"] == "south"
    r3 = _run(tool_handlers.tool_report_station_position(where="inside", travel="west", station_wait=ASK))
    assert r3["ui_action"]["travel"] is None


def t_bad_where_asks_again():
    r = _run(tool_handlers.tool_report_station_position(where="??", station_wait=ASK))
    assert r["status"] == "idle" and "ui_action" not in r


def t_nav_state_cleans_wait():
    nav = {}
    update_nav_state(nav, {"guiding": True, "station_wait": {"kind": "exit", "station": "관악역이름이아주길어서잘려야하는경우입니다정말로요",
                                                             "choices": [{"travel": "east", "label": "x"}], "extra": 1}})
    w = nav["station_wait"]
    assert w["kind"] == "exit" and len(w["station"]) == 20 and w["choices"] == [] and "extra" not in w
    update_nav_state(nav, {"station_wait": {"kind": "hack"}})
    assert nav["station_wait"] is None
    update_nav_state(nav, {"station_wait": ASK})
    assert [c["travel"] for c in nav["station_wait"]["choices"]] == ["south", "north"]


def t_dispatcher_has_tool():
    disp = tool_handlers.get_tool_dispatcher(embed_fn=None)
    assert "report_station_position" in disp


if __name__ == "__main__":
    for nm, fn in sorted((k, v) for k, v in list(globals().items()) if k.startswith("t_")):
        check(nm, fn)
    print()
    if FAILS:
        print("FAILED: %d" % len(FAILS))
        sys.exit(1)
    print("all passed")
