# -*- coding: utf-8 -*-
"""날씨 조회 (v2.29.0) — 지금 날씨·오늘/내일 예보·미세먼지와 휠체어 이동 주의 사항.

비·눈·추위·더위·강한 바람은 휠체어 이용자의 바깥 이동을 크게 제약한다(우산을 들기 어렵고,
젖은 바닥·빙판에서 미끄러지며, 추위에 전동 휠체어 배터리가 빨리 닳는다). 상담원이 날씨 질문에
답하고 실내 장소를 권할 수 있도록 예보를 요약해 돌려준다.

자료: Open-Meteo 예보(api.open-meteo.com)·대기질(air-quality-api.open-meteo.com). 키 없이 쓰는
공개 API 이며, 관측값이 아니라 예보 모델 값이다. 같은 지점(소수 둘째 자리)은 10분 동안 다시 묻지 않는다.

환경변수
  WEATHER_ENABLED      0 이면 도구를 선언하지 않는다(기본 1)
  WEATHER_API_BASE     예보 주소(기본 https://api.open-meteo.com/v1/forecast)
  WEATHER_AIR_API_BASE 대기질 주소(기본 https://air-quality-api.open-meteo.com/v1/air-quality)
"""
import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

logger = logging.getLogger("weather_client")

FORECAST_URL = os.environ.get("WEATHER_API_BASE", "https://api.open-meteo.com/v1/forecast")
AIR_URL = os.environ.get("WEATHER_AIR_API_BASE", "https://air-quality-api.open-meteo.com/v1/air-quality")
SOURCE = "Open-Meteo 예보"
KST = timezone(timedelta(hours=9))

# 위치를 모를 때의 기준점 — 서비스 지역(안양시청)
DEFAULT_POINT = (37.3943, 126.9568, "안양시")

CACHE_TTL_S = 600
TIMEOUT_S = 6.0
_cache: dict = {}


def enabled() -> bool:
    return os.environ.get("WEATHER_ENABLED", "1").strip() not in ("0", "false", "False", "")


# ── 하늘 상태 (WMO 날씨 코드) ──
_SKY = {0: "맑음", 1: "대체로 맑음", 2: "구름 조금", 3: "흐림", 45: "안개", 48: "안개",
        51: "약한 이슬비", 53: "이슬비", 55: "강한 이슬비", 56: "어는 이슬비", 57: "어는 이슬비",
        61: "약한 비", 63: "비", 65: "강한 비", 66: "어는 비", 67: "어는 비",
        71: "약한 눈", 73: "눈", 75: "많은 눈", 77: "싸락눈",
        80: "약한 소나기", 81: "소나기", 82: "강한 소나기", 85: "눈 소나기", 86: "강한 눈 소나기",
        95: "뇌우", 96: "우박을 동반한 뇌우", 99: "우박을 동반한 뇌우"}
RAIN_CODES = set(range(51, 68)) | {80, 81, 82, 95, 96, 99}
SNOW_CODES = {71, 73, 75, 77, 85, 86}
HEAVY_CODES = {65, 67, 75, 82, 86, 95, 96, 99}


def sky_label(code) -> str:
    try:
        return _SKY.get(int(code), "")
    except (TypeError, ValueError):
        return ""


def rain_label(mm_h: float) -> str:
    """시간당 강수량 → 기상청 강도 표현(약한 비 3mm 미만 · 비 3~15mm · 강한 비 15mm 이상)."""
    if mm_h >= 15:
        return "강한 비"
    if mm_h >= 3:
        return "비"
    return "약한 비"


def dust_grade(pm10, pm25) -> Optional[str]:
    """환경부 기준 미세먼지 등급 — PM10·PM2.5 중 나쁜 쪽."""
    def g10(v):
        return 0 if v <= 30 else 1 if v <= 80 else 2 if v <= 150 else 3

    def g25(v):
        return 0 if v <= 15 else 1 if v <= 35 else 2 if v <= 75 else 3
    gs = []
    if isinstance(pm10, (int, float)):
        gs.append(g10(pm10))
    if isinstance(pm25, (int, float)):
        gs.append(g25(pm25))
    if not gs:
        return None
    return ("좋음", "보통", "나쁨", "매우 나쁨")[max(gs)]


def _r(v):
    return None if not isinstance(v, (int, float)) else int(round(v))


def _num(v, d=0.0):
    return float(v) if isinstance(v, (int, float)) else d


def _hour_label(ts: str) -> str:
    try:
        h = int(ts[11:13])
    except (TypeError, ValueError):
        return ""
    if h == 0:
        return "밤 12시"
    if h == 12:
        return "낮 12시"
    return ("오전 %d시" % h) if h < 12 else ("오후 %d시" % (h - 12))


def _wet(prob, mm, code) -> bool:
    return _num(mm) >= 0.5 or _num(prob) >= 60 or (code in RAIN_CODES | SNOW_CODES and _num(mm) > 0)


def _cautions(feels, temp, rain_mm_h, raining, snowing, rain_expected, gust, dust, future=False):
    """휠체어 바깥 이동 주의 — 중요한 것부터. (문구, 심각도 2=어려움 1=주의).

    future=True 는 내일 예보 — 지금 일어나는 일처럼 말하지 않는다.
    """
    out = []
    if snowing:
        out.append(("눈 예보가 있어 길이 미끄럽고 바퀴가 빠지기 쉽겠습니다" if future
                    else "눈이 와 길이 미끄럽고 바퀴가 빠지기 쉽습니다", 2))
    if raining and (rain_mm_h >= 15):
        out.append(("많은 비 예보가 있어 바깥 이동이 어렵겠습니다" if future
                    else "비가 많이 와 바닥이 미끄럽고 앞이 잘 보이지 않습니다", 2))
    elif raining and not future:
        out.append(("비가 와 바닥이 미끄럽습니다. 우비나 방수 덮개가 필요합니다", 1))
    elif raining or rain_expected:
        out.append(("비 예보가 있어 우비나 방수 덮개를 챙기는 게 좋습니다", 1))
    if (raining or snowing) and temp is not None and temp <= 1:
        out.append(("기온이 낮아 젖은 길이 얼 수 있습니다", 2))
    if feels is not None:
        if feels <= -10:
            out.append(("체감 영하 %d도로 매우 춥습니다" % abs(feels), 2))
        elif feels <= 0:
            out.append(("추위에 손이 곱고 전동 휠체어 배터리가 빨리 닳을 수 있습니다", 1))
        elif feels >= 35:
            out.append(("체감 %d도의 폭염입니다. 오래 밖에 있지 않는 게 좋습니다" % feels, 2))
        elif feels >= 31:
            out.append(("더워서 물을 챙기고 그늘에서 쉬는 게 좋습니다. 손잡이·바퀴가 뜨거울 수 있습니다", 1))
    if gust is not None:
        if gust >= 20:
            out.append(("바람이 매우 강해 이동이 위험할 수 있습니다", 2))
        elif gust >= 14:
            out.append(("바람이 강해 휠체어가 밀릴 수 있습니다", 1))
    if dust == "매우 나쁨":
        out.append(("미세먼지가 매우 나쁩니다", 2))
    elif dust == "나쁨":
        out.append(("미세먼지가 나빠 마스크가 필요합니다", 1))
    return out


def summarize(fc: dict, air: Optional[dict], day: str = "today", now: Optional[datetime] = None) -> dict:
    """예보 응답 → 상담원이 말할 요약. 값이 없으면 그 항목만 비운다."""
    now = (now or datetime.now(KST)).astimezone(KST)
    cur = fc.get("current") or {}
    hourly = fc.get("hourly") or {}
    daily = fc.get("daily") or {}
    times = hourly.get("time") or []
    prob = hourly.get("precipitation_probability") or []
    prec = hourly.get("precipitation") or []
    codes = hourly.get("weather_code") or []

    def at(arr, i):
        return arr[i] if 0 <= i < len(arr) else None

    key = now.strftime("%Y-%m-%dT%H:00")
    hi = times.index(key) if key in times else 0
    today = now.strftime("%Y-%m-%d")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    air_cur = (air or {}).get("current") or {}
    pm10, pm25 = air_cur.get("pm10"), air_cur.get("pm2_5")
    dust = dust_grade(pm10, pm25)

    def day_block(ds: str, from_i: int) -> dict:
        dts = daily.get("time") or []
        di = dts.index(ds) if ds in dts else -1
        hours = [i for i in range(from_i, len(times)) if str(times[i]).startswith(ds)]
        wet = [i for i in hours if _wet(at(prob, i), at(prec, i), at(codes, i))]
        return {
            "date": ds,
            "sky": sky_label(at(daily.get("weather_code") or [], di)),
            "temp_max": _r(at(daily.get("temperature_2m_max") or [], di)),
            "temp_min": _r(at(daily.get("temperature_2m_min") or [], di)),
            "feels_max": _r(at(daily.get("apparent_temperature_max") or [], di)),
            "feels_min": _r(at(daily.get("apparent_temperature_min") or [], di)),
            "precip_prob_max": _r(at(daily.get("precipitation_probability_max") or [], di)),
            "precip_mm": round(_num(at(daily.get("precipitation_sum") or [], di)), 1),
            "snow_cm": round(_num(at(daily.get("snowfall_sum") or [], di)), 1),
            "wind_max_ms": _r(at(daily.get("wind_gusts_10m_max") or [], di)),
            "rain_hours": ("%s~%s" % (_hour_label(times[wet[0]]), _hour_label(times[wet[-1]]))
                           if wet else ""),
            "_wet": wet,
        }

    out = {"source": SOURCE, "observed": False,
           "updated": _hour_label(cur.get("time") or key) or None, "dust": dust,
           "pm10": _r(pm10), "pm25": _r(pm25)}

    if day == "tomorrow":
        t = day_block(tomorrow, 0)
        wet = t.pop("_wet")
        snowing = t["snow_cm"] > 0 or any(at(codes, i) in SNOW_CODES for i in wet)
        peak = max([_num(at(prec, i)) for i in wet] or [0.0])
        # 추운 날은 최저 체감, 아니면 최고 체감으로 본다
        fl = t["feels_min"] if (t["feels_min"] is not None and t["feels_min"] <= 0) else t["feels_max"]
        cautions = _cautions(fl, t["temp_min"], peak, bool(wet) and not snowing, snowing and bool(wet), False,
                             t["wind_max_ms"], None, future=True)
        out.update({"day": "tomorrow", "tomorrow": t})
    else:
        temp = _r(cur.get("temperature_2m"))
        feels = _r(cur.get("apparent_temperature"))
        code = cur.get("weather_code")
        # 지금 15분 강수 ×4 와 이번 시간 예보 중 큰 값을 시간당 강수로 본다
        mm_h = max(_num(cur.get("precipitation")) * 4, _num(at(prec, hi)))
        snowing = _num(cur.get("snowfall")) > 0 or code in SNOW_CODES
        raining = (not snowing) and (code in RAIN_CODES or mm_h >= 0.1)
        rest = day_block(today, hi + 1)
        wet = rest.pop("_wet")
        # 앞으로 6시간 안의 비 — 오늘이 6시간 안 남았으면 다음 날 새벽까지 본다
        nxt = [i for i in range(hi + 1, min(hi + 7, len(times)))]
        nxt_wet = [i for i in nxt if _wet(at(prob, i), at(prec, i), at(codes, i))]
        now_b = {"temp": temp, "feels_like": feels, "sky": sky_label(code),
                 "raining": raining, "snowing": snowing,
                 "rain_mm_h": round(mm_h, 1) if (raining or snowing) else 0.0,
                 "rain_label": rain_label(mm_h) if raining else "",
                 "humidity": _r(cur.get("relative_humidity_2m")),
                 "wind_ms": _r(cur.get("wind_speed_10m")), "gust_ms": _r(cur.get("wind_gusts_10m"))}
        if raining or snowing:
            dry = [i for i in nxt if not _wet(at(prob, i), at(prec, i), at(codes, i))]
            now_b["stops_around"] = _hour_label(times[dry[0]]) if dry else ""
        else:
            now_b["starts_around"] = _hour_label(times[nxt_wet[0]]) if nxt_wet else ""
        cautions = _cautions(feels, temp, mm_h, raining, snowing, bool(nxt_wet),
                             _r(cur.get("wind_gusts_10m")), dust)
        out.update({"day": "today", "now": now_b, "today": rest})

    out["cautions"] = [c for c, _ in cautions]
    sev = max([s for _, s in cautions] or [0])
    out["outdoor"] = {0: "좋음", 1: "주의", 2: "어려움"}[sev]
    return out


async def _get(client: httpx.AsyncClient, url: str, params: dict) -> Optional[dict]:
    r = await client.get(url, params=params)
    r.raise_for_status()
    return r.json()


async def fetch(lat: float, lng: float) -> dict:
    """예보·대기질 원자료. 실패하면 {"error": 사유}. 대기질만 실패하면 air=None."""
    k = (round(float(lat), 2), round(float(lng), 2))
    hit = _cache.get(k)
    if hit and time.monotonic() - hit[0] < CACHE_TTL_S:
        return hit[1]
    fp = {"latitude": k[0], "longitude": k[1], "timezone": "Asia/Seoul", "forecast_days": 2,
          "wind_speed_unit": "ms",
          "current": ("temperature_2m,apparent_temperature,precipitation,rain,snowfall,weather_code,"
                      "wind_speed_10m,wind_gusts_10m,relative_humidity_2m"),
          "hourly": "precipitation_probability,precipitation,weather_code",
          "daily": ("weather_code,temperature_2m_max,temperature_2m_min,apparent_temperature_max,"
                    "apparent_temperature_min,precipitation_sum,snowfall_sum,precipitation_probability_max,"
                    "wind_gusts_10m_max")}
    ap = {"latitude": k[0], "longitude": k[1], "timezone": "Asia/Seoul", "current": "pm10,pm2_5"}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as c:
            fc, air = await asyncio.gather(_get(c, FORECAST_URL, fp), _get(c, AIR_URL, ap),
                                           return_exceptions=True)
    except Exception as e:                      # noqa: BLE001
        fc, air = e, None
    if isinstance(fc, BaseException) or not isinstance(fc, dict) or not fc.get("current"):
        logger.warning("날씨 예보 조회 실패: %s", type(fc).__name__ if isinstance(fc, BaseException) else "빈 응답")
        return {"error": "forecast_unavailable"}
    if isinstance(air, BaseException) or not isinstance(air, dict):
        air = None
    data = {"fc": fc, "air": air}
    _cache[k] = (time.monotonic(), data)
    if len(_cache) > 200:
        for kk in sorted(_cache, key=lambda x: _cache[x][0])[:100]:
            _cache.pop(kk, None)
    return data
