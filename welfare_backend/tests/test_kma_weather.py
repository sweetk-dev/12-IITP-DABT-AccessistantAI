# -*- coding: utf-8 -*-
"""기상청 단기예보 원천 (v2.30.0) — 격자·값 해석·요약 형태 변환·예비 원천 전환. 외부 호출 없음(고정 자료)."""
import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kma_weather as km        # noqa: E402
import weather_client as wc     # noqa: E402

NOW = datetime(2026, 11, 12, 15, 20, tzinfo=km.KST)


def _f(date, time, cat, v):
    return {"fcstDate": date, "fcstTime": time, "category": cat, "fcstValue": v}


def _fixture(rain_now=True):
    ncst = [{"category": c, "obsrValue": v} for c, v in
            (("PTY", "1" if rain_now else "0"), ("RN1", "4.5" if rain_now else "0"), ("T1H", "14.2"),
             ("REH", "90"), ("WSD", "3.1"))]
    uf = []
    for h, pty, rn in (("1600", "1", "2.0mm"), ("1700", "0", "강수없음"), ("1800", "0", "강수없음")):
        uf += [_f("20261112", h, "PTY", pty), _f("20261112", h, "RN1", rn), _f("20261112", h, "SKY", "4"),
               _f("20261112", h, "T1H", "14"), _f("20261112", h, "POP", "80" if pty == "1" else "20"),
               _f("20261112", h, "WSD", "3")]
    vil = []
    for d in ("20261112", "20261113"):
        for h in range(24):
            hh = "%02d00" % h
            wet = d == "20261113" and 9 <= h <= 12
            vil += [_f(d, hh, "TMP", "10"), _f(d, hh, "POP", "90" if wet else "10"),
                    _f(d, hh, "PTY", "1" if wet else "0"), _f(d, hh, "PCP", "1.0mm" if wet else "강수없음"),
                    _f(d, hh, "SKY", "3"), _f(d, hh, "REH", "70"), _f(d, hh, "WSD", "10" if d == "20261113" else "2"),
                    _f(d, hh, "SNO", "적설없음")]
    vil += [_f("20261113", "0600", "TMN", "8.0"), _f("20261113", "1500", "TMX", "17.0")]
    day = [_f("20261112", "0600", "TMN", "5.0"), _f("20261112", "1500", "TMX", "15.0")]
    return ncst, uf, vil, day


def test_grid_and_amounts():
    assert km.latlon_to_grid(37.5665, 126.9780) == (60, 127)      # 서울시청 — 기상청 격자표
    assert km.latlon_to_grid(37.3943, 126.9568) == (59, 123)      # 안양시청
    assert [km.parse_amount(x) for x in ("강수없음", "1mm 미만", "6.2mm", "30.0~50.0mm", "50.0mm 이상", "적설없음", "0")] == \
        [0.0, 0.5, 6.2, 40.0, 50.0, 0.0, 0.0]


def test_base_times():
    t = datetime(2026, 11, 12, 15, 20, tzinfo=km.KST)
    assert km.base_ncst(t).strftime("%H%M") == "1400" and km.base_ufcst(t).strftime("%H%M") == "1430"
    assert km.base_vilage(t).strftime("%H%M") == "1400" and km.base_dayrun(t).strftime("%d%H") == "1202"
    t2 = datetime(2026, 11, 12, 1, 5, tzinfo=km.KST)
    assert km.base_vilage(t2).strftime("%d%H") == "1123" and km.base_dayrun(t2).strftime("%d%H") == "1123"


def test_feels_like():
    assert round(km.feels_like(-5, 50, 5)) == -11       # 겨울 — 풍속 냉각
    assert round(km.feels_like(33, 70, 1)) == 34        # 여름 — 습구온도
    assert km.feels_like(15, 60, 2) == 15


def test_kma_shape_summarizes_observed_rain():
    s = wc.summarize(km.to_summary_shape(*_fixture(), NOW), None, now=NOW)
    n = s["now"]
    assert s["source"] == "기상청 단기예보" and s["observed"] is True
    assert n["raining"] and n["rain_label"] == "비" and n["sky"] == "비" and n["temp"] == 14
    assert n["stops_around"] == "오후 5시"
    assert s["today"]["temp_max"] == 15 and s["today"]["temp_min"] == 5      # 02시 발표의 TMX·TMN
    assert s["outdoor"] == "주의" and "비가 와" in s["cautions"][0]


def test_observation_beats_forecast_for_now():
    ncst, uf, vil, day = _fixture(rain_now=False)
    s = wc.summarize(km.to_summary_shape(ncst, uf, vil, day, NOW), None, now=NOW)
    assert s["now"]["raining"] is False, "관측이 비 없음이면 예보 값이 있어도 비가 온다고 하지 않는다"
    assert s["now"]["starts_around"] == "오후 4시" and s["now"]["sky"] == "구름 많음"   # 이번 시각(15시) 예보의 하늘


def test_tomorrow_rain_and_mean_wind_caution():
    s = wc.summarize(km.to_summary_shape(*_fixture(), NOW), None, day="tomorrow", now=NOW)
    t = s["tomorrow"]
    assert t["rain_hours"] == "오전 9시~낮 12시" and t["temp_max"] == 17 and t["temp_min"] == 8
    assert t["sky"] == "비"
    assert any("비 예보" in c for c in s["cautions"]) and any("바람이 강해" in c for c in s["cautions"])


def test_fetch_falls_back_to_openmeteo(monkeypatch):
    monkeypatch.setenv("KMA_API_KEY", "test")
    monkeypatch.delenv("WEATHER_PROVIDER", raising=False)
    wc._cache.clear()

    async def boom(lat, lng, now=None):
        raise RuntimeError("kma 30")
    calls = []

    async def fake_get(client, url, params):
        calls.append(url)
        if "air-quality" in url:
            return {"current": {"pm10": 20, "pm2_5": 10}}
        return {"current": {"time": "2026-11-12T15:15", "temperature_2m": 15}, "hourly": {}, "daily": {}}
    monkeypatch.setattr(km, "fetch", boom)
    monkeypatch.setattr(wc, "_get", fake_get)
    r = asyncio.run(wc.fetch(37.39, 126.95))
    assert r["fc"]["current"]["temperature_2m"] == 15 and r["air"]["current"]["pm10"] == 20
    assert any("open-meteo.com/v1/forecast" in u for u in calls)


def test_fetch_uses_kma_when_available(monkeypatch):
    monkeypatch.setenv("KMA_API_KEY", "test")
    monkeypatch.delenv("WEATHER_PROVIDER", raising=False)
    wc._cache.clear()

    async def ok(lat, lng, now=None):
        return km.to_summary_shape(*_fixture(), NOW)
    calls = []

    async def fake_get(client, url, params):
        calls.append(url)
        return {"current": {"pm10": 20, "pm2_5": 10}}
    monkeypatch.setattr(km, "fetch", ok)
    monkeypatch.setattr(wc, "_get", fake_get)
    r = asyncio.run(wc.fetch(37.39, 126.95))
    assert r["fc"]["_source"] == "기상청 단기예보"
    assert calls and all("air-quality" in u for u in calls), "기상청이 되면 Open-Meteo 예보는 부르지 않는다"


def test_provider_switch(monkeypatch):
    monkeypatch.setenv("KMA_API_KEY", "x")
    monkeypatch.setenv("WEATHER_PROVIDER", "openmeteo")
    assert km.enabled() is False
    monkeypatch.delenv("WEATHER_PROVIDER")
    assert km.enabled() is True
    monkeypatch.delenv("KMA_API_KEY")
    monkeypatch.delenv("DATA_GO_KR_API_KEY", raising=False)
    assert km.enabled() is False
