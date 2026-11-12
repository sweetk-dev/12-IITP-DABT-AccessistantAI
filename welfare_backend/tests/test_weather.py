# -*- coding: utf-8 -*-
"""날씨 조회·실내 장소·짧은 답변 지침 (v2.29.0). 외부 호출 없음(예보 응답은 고정 자료)."""
import asyncio
import os
import sys
import types as _t
from datetime import datetime
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

import weather_client as wc          # noqa: E402
import tool_handlers as th           # noqa: E402
import nav_context as nc             # noqa: E402
import local_pipeline as lp          # noqa: E402

NOW = datetime(2026, 11, 12, 14, 20, tzinfo=wc.KST)


def _fc(cur=None, prob=None, prec=None, codes=None, daily=None):
    times = ["2026-11-12T%02d:00" % h for h in range(24)] + ["2026-11-13T%02d:00" % h for h in range(24)]
    c = {"time": "2026-11-12T14:15", "temperature_2m": 15.2, "apparent_temperature": 13.6, "precipitation": 0.0,
         "rain": 0.0, "snowfall": 0.0, "weather_code": 1, "wind_speed_10m": 3.5, "wind_gusts_10m": 8.7,
         "relative_humidity_2m": 75}
    c.update(cur or {})
    d = {"time": ["2026-11-12", "2026-11-13"], "weather_code": [3, 3], "temperature_2m_max": [15.2, 16.9],
         "temperature_2m_min": [5.2, 7.3], "apparent_temperature_max": [14.0, 15.5],
         "apparent_temperature_min": [3.0, 5.0], "precipitation_sum": [0.0, 0.0], "snowfall_sum": [0.0, 0.0],
         "precipitation_probability_max": [0, 7], "wind_gusts_10m_max": [9, 8]}
    d.update(daily or {})
    return {"current": c, "daily": d, "hourly": {"time": times,
            "precipitation_probability": prob or [0] * 48, "precipitation": prec or [0.0] * 48,
            "weather_code": codes or [1] * 48}}


def test_clear_day_is_fine():
    s = wc.summarize(_fc(), {"current": {"pm10": 20.8, "pm2_5": 15.5}}, now=NOW)
    assert s["now"]["temp"] == 15 and s["now"]["feels_like"] == 14 and s["now"]["sky"] == "대체로 맑음"
    assert s["now"]["raining"] is False and s["now"]["starts_around"] == ""
    assert s["outdoor"] == "좋음" and s["cautions"] == [] and s["dust"] == "보통"
    assert s["today"]["temp_max"] == 15 and s["observed"] is False


def test_heavy_rain_now_is_hard_and_reports_when_it_stops():
    prec = [0.0] * 48; prob = [0] * 48; codes = [1] * 48
    for h in range(14, 17):
        prec[h], prob[h], codes[h] = 18.0, 95, 65
    s = wc.summarize(_fc(cur={"precipitation": 4.5, "rain": 4.5, "weather_code": 65}, prob=prob, prec=prec, codes=codes),
                     None, now=NOW)
    assert s["now"]["raining"] and s["now"]["rain_label"] == "강한 비"
    assert s["now"]["stops_around"] == "오후 5시"
    assert s["outdoor"] == "어려움" and "비가 많이 와" in s["cautions"][0]
    assert s["today"]["rain_hours"] == "오후 3시~오후 4시"


def test_rain_expected_later_is_caution():
    prob = [0] * 48; prob[17] = 80
    s = wc.summarize(_fc(prob=prob), None, now=NOW)
    assert s["now"]["starts_around"] == "오후 5시"
    assert s["outdoor"] == "주의" and "비 예보" in s["cautions"][0]


def test_cold_and_snow():
    s = wc.summarize(_fc(cur={"temperature_2m": -6, "apparent_temperature": -12, "snowfall": 0.4, "weather_code": 73}),
                     None, now=NOW)
    assert s["now"]["snowing"] and not s["now"]["raining"]
    assert s["outdoor"] == "어려움"
    assert s["cautions"][0].startswith("눈이 와") and any("얼 수" in c for c in s["cautions"])
    assert any("체감 영하 12도" in c for c in s["cautions"])


def test_mild_cold_mentions_battery():
    s = wc.summarize(_fc(cur={"temperature_2m": 2, "apparent_temperature": -2}), None, now=NOW)
    assert s["outdoor"] == "주의" and "배터리" in s["cautions"][0]


def test_heat_and_bad_dust():
    s = wc.summarize(_fc(cur={"temperature_2m": 34, "apparent_temperature": 36}), {"current": {"pm10": 90, "pm2_5": 20}},
                     now=NOW)
    assert s["outdoor"] == "어려움" and "폭염" in s["cautions"][0]
    assert s["dust"] == "나쁨" and any("미세먼지" in c for c in s["cautions"])


def test_tomorrow_uses_forecast_wording():
    prob = [0] * 48; prec = [0.0] * 48
    for h in range(33, 37):
        prob[h], prec[h] = 90, 2.0
    s = wc.summarize(_fc(prob=prob, prec=prec, daily={"precipitation_sum": [0, 8.0], "precipitation_probability_max": [0, 90]}),
                     None, day="tomorrow", now=NOW)
    t = s["tomorrow"]
    assert s["day"] == "tomorrow" and "now" not in s
    assert t["rain_hours"] == "오전 9시~낮 12시" and t["precip_prob_max"] == 90
    assert s["cautions"] == ["비 예보가 있어 우비나 방수 덮개를 챙기는 게 좋습니다"]


def test_feels_word_keeps_answers_honest():
    assert [wc.feels_word(v) for v in (-12, -3, 8, 14, 25, 30, 36)] == [
        "매우 추움", "추움", "쌀쌀함", "선선함", "따뜻함", "더움", "매우 더움"]
    s = wc.summarize(_fc(), None, now=NOW)
    assert s["now"]["feels_word"] == "선선함" and s["today"]["feels_word_low"] == "쌀쌀함"
    assert "feels_word" in th._WEATHER_AI and "'좋음'이면 묻지 않습니다" in th._WEATHER_AI


def test_rain_label_and_dust_grade():
    assert [wc.rain_label(v) for v in (0.5, 3, 15)] == ["약한 비", "비", "강한 비"]
    assert wc.dust_grade(None, None) is None and wc.dust_grade(200, 10) == "매우 나쁨"


def _run(coro):
    return asyncio.run(coro)


def test_tool_uses_current_location_then_default(monkeypatch):
    seen = []

    async def fake_fetch(lat, lng):
        seen.append((lat, lng))
        return {"fc": _fc(), "air": None}
    monkeypatch.setattr(wc, "fetch", fake_fetch)
    r = _run(th.tool_get_weather(lat=37.39, lng=126.95))
    assert r["status"] == "success" and r["place_label"] == "현재 위치" and "현재 위치" in r["ai_instruction"]
    r = _run(th.tool_get_weather())
    assert r["place_label"] == "안양시" and seen[-1] == wc.DEFAULT_POINT[:2]
    assert "한두 문장" in r["ai_instruction"]


def test_tool_place_not_found_and_fetch_error(monkeypatch):
    async def none_place(q):
        return None

    async def bad_fetch(lat, lng):
        return {"error": "forecast_unavailable"}
    monkeypatch.setattr(th, "_resolve_place", none_place)
    assert _run(th.tool_get_weather(place="없는동네"))["status"] == "place_not_found"
    monkeypatch.setattr(wc, "fetch", bad_fetch)
    r = _run(th.tool_get_weather())
    assert r["status"] == "error" and "짐작" in r["ai_instruction"]


def test_tool_place_resolves_outside_service_area(monkeypatch):
    async def place(q):
        return {"lat": 37.26, "lng": 127.03, "label": "수원시청", "in_service_area": False}

    async def fake_fetch(lat, lng):
        return {"fc": _fc(), "air": None}
    monkeypatch.setattr(th, "_resolve_place", place)
    monkeypatch.setattr(wc, "fetch", fake_fetch)
    r = _run(th.tool_get_weather(place="수원", day="tomorrow"))
    assert r["status"] == "success" and r["place_label"] == "수원시청" and r["day"] == "tomorrow"


def test_weather_location_injection_does_not_ask_permission():
    f = nc.inject_nav_defaults("get_weather", {}, {}, {"lat": 37.4, "lng": 126.9})
    assert (f["lat"], f["lng"]) == (37.4, 126.9)
    assert "lat" not in nc.inject_nav_defaults("get_weather", {"place": "수원"}, {}, {"lat": 37.4, "lng": 126.9})
    assert nc.needs_current_location("get_weather", {}, {}) is False


SPOTS = [{"poi_id": str(i), "name": n, "lat": 37.4, "lng": 126.9} for i, n in enumerate([
    "김중업 건축박물관 (옛 유유산업 공장)", "안양천", "안양아트센터", "병목안 시민공원", "안양실내체육관",
    "안양종합운동장(실내.외수영장)", "동편마을 카페거리", "안양종합운동장", "안양파빌리온", "안양문화원"])]


def test_indoor_filter(monkeypatch):
    calls = []

    async def fake_rec(dis, **kw):
        calls.append(kw)
        return {"items": SPOTS, "total": len(SPOTS)}
    monkeypatch.setattr(th.route_client, "tour_recommend", fake_rec)
    r = _run(th.tool_find_bf_tour_spots(indoor=True, topk=10))
    names = [x["name"] for x in r["results"]]
    assert names == ["김중업 건축박물관 (옛 유유산업 공장)", "안양아트센터", "안양실내체육관",
                     "안양종합운동장(실내.외수영장)", "안양파빌리온", "안양문화원"]
    assert r["indoor_only"] is True and r["has_more"] is False and calls[-1]["topk"] == 50
    r = _run(th.tool_find_bf_tour_spots(topk=5))
    assert r["indoor_only"] is False and calls[-1]["topk"] == 5 and len(r["results"]) == len(SPOTS)


def test_declarations_live_and_fallback():
    fb = {t["function"]["name"]: t["function"] for t in lp._ollama_tools()}
    assert set(fb["get_weather"]["parameters"]["properties"]) == {"place", "day"}
    assert "indoor" in fb["find_bf_tour_spots"]["parameters"]["properties"]
    lb = pytest.importorskip("live_bridge")
    decls = {d.name: d for t in lb.build_tool_declarations() if t is not None
             for d in (getattr(t, "function_declarations", None) or [])}
    assert set(decls["get_weather"].parameters.properties) == {"place", "day"}
    assert "indoor" in decls["find_bf_tour_spots"].parameters.properties


def test_weather_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("WEATHER_ENABLED", "0")
    assert "get_weather" not in {t["function"]["name"] for t in lp._ollama_tools()}


def test_toilet_one_at_a_time(monkeypatch):
    async def base(place, lat, lng, name):
        return 37.38, 126.93, None

    async def near(lat, lng, radius_m=800, limit=5, accessible_only=True):
        return {"items": [{"name": "브레인빌딩 개방화장실", "dist_m": 66, "accessible": True},
                          {"name": "명학역 공중화장실", "dist_m": 107, "accessible": True}]}
    monkeypatch.setattr(th, "_resolve_base", base)
    monkeypatch.setattr(th.route_client, "toilet_nearby", near)
    r = _run(th.tool_find_toilet(lat=37.38, lng=126.93))
    assert r["count"] == 2   # 화면 카드에는 모두 보인다
    assert "가장 가까운 한 곳" in r["ai_instruction"] and "다음 항목 하나만" in r["ai_instruction"]
    lb = pytest.importorskip("live_bridge")
    assert "**가장 가까운 한 곳만**" in lb.SYSTEM_INSTRUCTION and "가까운 1~2곳을 거리·개방시간" not in lb.SYSTEM_INSTRUCTION


def test_instruction_rules():
    lb = pytest.importorskip("live_bridge")
    s = lb.SYSTEM_INSTRUCTION
    for frag in ("한 번의 답변은 4문장 이내", "이어서 말씀드릴까요?", "바깥 날씨를 말하는 추위·더위",
                 "`get_weather`", "`indoor` true", "길안내 음성이 방금 말한 안내를 그대로 다시 읽지 마세요",
                 "불러 드릴까요?", "서두와 맺음말을 붙이지 마세요"):
        assert frag in s, frag
    assert "5~7문장 허용" not in s and "최대 8문장" not in s
    assert "get_weather" in lp.LOCAL_SYSTEM_PROMPT and "4문장" in lp.LOCAL_SYSTEM_PROMPT
    assert "맺음말은 붙이지 마세요" in lp.LOCAL_SYSTEM_PROMPT
