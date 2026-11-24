# -*- coding: utf-8 -*-
"""경로를 찾은 뒤 하는 말은 짧게 (v2.34.2) — 걸리는 시간 · 주의할 곳 · 안내 시작 질문만.

    python3 -m pytest tests/test_route_speech_short.py

계약:
  1) 지시문은 총 거리·최대 경사 숫자·계단 수·첫 이동 문장을 말하지 말라고 한다(화면 카드에 있다). 값은 결과에 그대로 남는다
  2) 말로 알릴 주의할 곳(speak_cautions)은 이용자 제보와 권장 기준(6°) 이상 경사만 — '턱낮춤 미상'은 넣지 않는다
  3) 위치는 출발 150m 안이면 "출발하자마자", 도착 150m 안이면 "도착 바로 앞", 그 밖은 "약 ○m 지나서"
  4) 같은 종류가 150m 안에 이어지면 한 곳으로 묶고, 최대 2곳
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

import route_client                                        # noqa: E402
import tool_handlers as th                                 # noqa: E402


def S(d, w=None, name=None, m="straight"):
    return {"maneuver": m, "instruction": "x", "distance_m": d, "warnings": w or [], "link_name": name}


# 문화원 → 아트센터(11/24 운영)와 같은 모양 — 출발 22m·100m 에 경사 제보 2건, 턱낮춤 미상 4건
C2 = {"steps": [S(14), S(8, m="crossing"), S(78, ["이용자 제보: 경사 심함 (미확인)"]),
                S(11, ["이용자 제보: 경사 심함"], m="crossing"), S(88), S(0, ["턱낮춤 미상"], m="crossing_point"),
                S(423, name="안양로"), S(350, name="안양로"), S(0, ["턱낮춤 미상"], m="crossing_point"),
                S(255), S(0, m="arrive")]}


def test_cautions_merge_nearby_and_skip_unknown_curb():
    c = th._route_speak_cautions(C2)
    assert c == [{"where": "출발하자마자", "what": "경사가 심한 곳", "reported": True}], c


def test_where_labels_and_slope_threshold():
    p = {"steps": [S(500), S(41, ["경사 5.6도 구간"]), S(300, name="안양로"), S(20, ["경사 7.2도 구간"], name="안양로"),
                   S(600), S(10, ["이용자 제보: 턱 있음"]), S(90), S(0, m="arrive")]}
    c = th._route_speak_cautions(p)
    assert c == [{"where": "약 800m 지나서 안양로에서", "what": "경사가 심한 곳", "reported": False},
                 {"where": "도착 바로 앞", "what": "턱이 있는 곳", "reported": True}], c
    assert th._route_speak_cautions({"steps": [S(100), S(0, m="arrive")]}) == []


def _route(steps):
    return {"route_id": "r_s", "destination": {"lat": 37.384, "lng": 126.93},
            "routes": [{"summary": {"total_distance_m": 1227, "duration_sec": 1116, "max_slope_deg": 3.46,
                                    "stairs_cnt": 0, "crossing_cnt": 3, "warnings": ["이용자 제보: 경사 심함 (미확인)"]},
                        "steps": steps, "legs": []}]}


def test_instruction_is_short_and_values_kept():
    async def fake_call(method, path, *, params=None, json=None):
        return _route(C2["steps"]) if path == "/route/plan" else {}
    orig = route_client._call
    route_client._call = fake_call
    try:
        r = asyncio.run(th.tool_plan_accessible_route(origin_lat=37.3906, origin_lng=126.9258,
                                                       destination_lat=37.384, destination_lng=126.93, mode="walk"))
    finally:
        route_client._call = orig
    assert r["status"] == "success", r
    ins = r["ai_instruction"]
    assert "말은 짧게 하세요" in ins and "총 거리 숫자, 최대 경사 숫자, 계단 수, 첫 이동 문장" in ins
    assert "총 거리·예상 시간·최대 경사·계단 수를 한 문장으로" not in ins and "첫 안내 한 문장을 덧붙이세요" not in ins
    assert "주의할 곳: 출발하자마자 경사가 심한 곳" in ins
    assert r["speak_cautions"][0]["where"] == "출발하자마자"
    s = r["summary"]                                            # 물으면 답할 값은 그대로
    assert s["distance_m"] == 1227 and s["max_slope_deg"] == 3.46 and s["stairs_cnt"] == 0
    assert r["warnings"] == ["이용자 제보: 경사 심함 (미확인)"]
