# -*- coding: utf-8 -*-
"""기상청 단기예보 조회서비스(공공데이터포털) — 지금 날씨는 초단기실황(관측), 6시간은 초단기예보, 오늘·내일은 단기예보 (v2.30.0).

weather_client.summarize 가 읽는 형태(current·hourly·daily)로 바꿔 돌려준다 — 요약·주의 규칙은 원천과 무관하게 한 곳에 둔다.
체감온도는 기상청 자료에 없어 기상청 산식으로 계산한다(겨울 풍속 냉각, 여름 습구온도 기반). 순간풍속은 없어 평균 풍속을 쓴다.

환경변수
  WEATHER_PROVIDER   openmeteo 이면 기상청을 쓰지 않는다(기본: 키가 있으면 기상청)
  KMA_API_KEY        공공데이터포털 인증키(없으면 DATA_GO_KR_API_KEY)
  KMA_API_BASE       기본 https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/
"""
import asyncio
import math
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

KST = timezone(timedelta(hours=9))
BASE = os.environ.get("KMA_API_BASE", "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/")
SOURCE = "기상청 단기예보"
TIMEOUT_S = 6.0


def api_key() -> str:
    return (os.environ.get("KMA_API_KEY") or os.environ.get("DATA_GO_KR_API_KEY") or "").strip()


def enabled() -> bool:
    return os.environ.get("WEATHER_PROVIDER", "").strip().lower() != "openmeteo" and bool(api_key())


# ── 위경도 → 기상청 격자 (Lambert Conformal Conic, 기상청 공개 산식) ──
def latlon_to_grid(lat: float, lon: float):
    RE, GRID, SLAT1, SLAT2, OLON, OLAT, XO, YO = 6371.00877, 5.0, 30.0, 60.0, 126.0, 38.0, 43, 136
    d = math.pi / 180.0
    re_ = RE / GRID
    s1, s2, olon, olat = SLAT1 * d, SLAT2 * d, OLON * d, OLAT * d
    sn = math.log(math.cos(s1) / math.cos(s2)) / math.log(math.tan(math.pi * 0.25 + s2 * 0.5) / math.tan(math.pi * 0.25 + s1 * 0.5))
    sf = math.tan(math.pi * 0.25 + s1 * 0.5) ** sn * math.cos(s1) / sn
    ro = re_ * sf / math.tan(math.pi * 0.25 + olat * 0.5) ** sn
    ra = re_ * sf / math.tan(math.pi * 0.25 + lat * d * 0.5) ** sn
    theta = lon * d - olon
    if theta > math.pi:
        theta -= 2.0 * math.pi
    if theta < -math.pi:
        theta += 2.0 * math.pi
    theta *= sn
    return int(math.floor(ra * math.sin(theta) + XO + 0.5)), int(math.floor(ro - ra * math.cos(theta) + YO + 0.5))


# ── 발표 시각 ──
def base_ncst(now: datetime) -> datetime:
    """초단기실황 — 매시 정시 자료, 40분쯤부터 제공."""
    return (now - timedelta(minutes=40)).replace(minute=0, second=0, microsecond=0)


def base_ufcst(now: datetime) -> datetime:
    """초단기예보 — 매시 30분 발표, 45분쯤부터 제공."""
    return (now - timedelta(minutes=45)).replace(minute=30, second=0, microsecond=0)


def base_vilage(now: datetime) -> datetime:
    """단기예보 — 02·05·08·11·14·17·20·23시 발표, 10분쯤부터 제공."""
    t = now - timedelta(minutes=10)
    hs = [h for h in (2, 5, 8, 11, 14, 17, 20, 23) if h <= t.hour]
    if hs:
        return t.replace(hour=hs[-1], minute=0, second=0, microsecond=0)
    return (t - timedelta(days=1)).replace(hour=23, minute=0, second=0, microsecond=0)


def base_dayrun(now: datetime) -> datetime:
    """오늘 최저·최고기온(TMN·TMX)이 들어 있는 발표 — 오늘 02시(02:10 전이면 어제 23시)."""
    t = now - timedelta(minutes=10)
    if t.hour >= 2:
        return t.replace(hour=2, minute=0, second=0, microsecond=0)
    return (t - timedelta(days=1)).replace(hour=23, minute=0, second=0, microsecond=0)


# ── 값 해석 ──
_NUM = re.compile(r"(\d+(?:\.\d+)?)")


def parse_amount(v) -> float:
    """강수량(PCP·RN1, mm)·신적설(SNO, cm) 문자열 → 숫자. '강수없음'·'적설없음' 0, '1mm 미만' 0.5, '30.0~50.0mm' 가운데값."""
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return max(0.0, float(v))
    s = str(v).strip()
    if not s or "없음" in s or s in ("-", "0"):
        return 0.0
    nums = [float(x) for x in _NUM.findall(s)]
    if not nums:
        return 0.0
    if "미만" in s:
        return nums[0] / 2.0
    if len(nums) >= 2 and "~" in s:
        return (nums[0] + nums[1]) / 2.0
    return nums[0]


def _f(v) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if x <= -900 or x >= 900 else x   # 기상청 결측값(-999·900 이상)


def feels_like(t: Optional[float], rh: Optional[float], wind_ms: Optional[float]) -> Optional[float]:
    """기상청 체감온도 산식 — 10도 이하·풍속 1.3m/s 이상이면 겨울 체감(풍속 냉각), 25도 이상이면 여름 체감(습구온도)."""
    if t is None:
        return None
    v_kmh = (wind_ms or 0.0) * 3.6
    if t <= 10 and v_kmh >= 4.8:
        p = v_kmh ** 0.16
        return 13.12 + 0.6215 * t - 11.37 * p + 0.3965 * t * p
    if t >= 25 and rh is not None:
        tw = (t * math.atan(0.151977 * (rh + 8.313659) ** 0.5) + math.atan(t + rh) - math.atan(rh - 1.67633)
              + 0.00391838 * rh ** 1.5 * math.atan(0.023101 * rh) - 4.686035)
        return -0.2442 + 0.55399 * tw + 0.45535 * t - 0.0022 * tw ** 2 + 0.00278 * tw * t + 3.0
    return t


PTY_LABEL = {1: "비", 2: "비 또는 눈", 3: "눈", 4: "소나기", 5: "빗방울", 6: "빗방울 또는 눈날림", 7: "눈날림"}
SKY_LABEL = {1: "맑음", 3: "구름 많음", 4: "흐림"}


def _int(v) -> Optional[int]:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def wmo_code(pty: Optional[int], sky: Optional[int], mm: float) -> int:
    """요약 규칙이 쓰는 WMO 날씨 코드로 — 비 61·63·65, 눈(비 또는 눈 포함) 71·73, 비 없음은 하늘 상태."""
    if pty in (1, 4, 5):
        return 65 if mm >= 15 else 63 if mm >= 3 else 61
    if pty in (2, 3, 6, 7):
        return 73 if pty in (2, 3) else 71
    return {1: 0, 3: 2, 4: 3}.get(sky, 1)


def _label(pty: Optional[int], sky: Optional[int]) -> str:
    if pty:
        return PTY_LABEL.get(pty, "")
    return SKY_LABEL.get(sky, "")


def _table(items: list, value_key: str) -> dict:
    """[{fcstDate,fcstTime,category,fcstValue}] → {"YYYYMMDDHH": {cat: value}}"""
    out: dict = {}
    for it in items or []:
        k = str(it.get("fcstDate", "")) + str(it.get("fcstTime", ""))[:2]
        out.setdefault(k, {})[it.get("category")] = it.get(value_key)
    return out


def to_summary_shape(ncst: list, ufcst: list, vil: list, dayrun: list, now: datetime) -> dict:
    """기상청 응답 4종 → weather_client.summarize 가 읽는 current·hourly·daily."""
    now = now.astimezone(KST)
    obs = {it.get("category"): it.get("obsrValue") for it in (ncst or [])}
    uf, vf, dr = _table(ufcst, "fcstValue"), _table(vil, "fcstValue"), _table(dayrun, "fcstValue")
    days = [now.strftime("%Y%m%d"), (now + timedelta(days=1)).strftime("%Y%m%d")]

    def hv(key, *cats):
        for src in (uf, vf, dr):            # 가까운 시각일수록 초단기예보가 정확하다
            row = src.get(key) or {}
            for c in cats:
                if row.get(c) not in (None, ""):
                    return row.get(c)
        return None

    times, prob, prec, codes, temps, rhs, winds, ptys, skys, snows = [], [], [], [], [], [], [], [], [], []
    for d in days:
        for h in range(24):
            key = "%s%02d" % (d, h)
            mm = parse_amount(hv(key, "RN1", "PCP"))
            pty, sky = _int(hv(key, "PTY")), _int(hv(key, "SKY"))
            times.append("%s-%s-%sT%02d:00" % (d[:4], d[4:6], d[6:], h))
            prob.append(_int(hv(key, "POP")) or 0)
            prec.append(mm)
            codes.append(wmo_code(pty, sky, mm))
            temps.append(_f(hv(key, "T1H", "TMP")))
            rhs.append(_f(hv(key, "REH")))
            winds.append(_f(hv(key, "WSD")))
            ptys.append(pty)
            skys.append(sky)
            snows.append(parse_amount(hv(key, "SNO")))

    # 지금 — 관측(초단기실황), 하늘 상태만 이번 시각 예보에서
    hi = now.hour
    t, rh, ws = _f(obs.get("T1H")), _f(obs.get("REH")), _f(obs.get("WSD"))
    rn1, pty = parse_amount(obs.get("RN1")), _int(obs.get("PTY"))
    sky = skys[hi] if 0 <= hi < len(skys) else None
    fl = feels_like(t, rh, ws)
    current = {"time": now.strftime("%Y-%m-%dT%H:%M"),
               "temperature_2m": t, "apparent_temperature": round(fl, 1) if fl is not None else None,
               "precipitation": rn1 / 4.0, "precip_mm_h": rn1,
               "snowfall": 1.0 if pty in (2, 3, 6, 7) else 0.0,
               "weather_code": wmo_code(pty, sky, rn1), "sky_label": _label(pty, sky),
               "wind_speed_10m": ws, "wind_gusts_10m": None, "relative_humidity_2m": rh}

    # 오늘·내일 — 최저·최고는 02시 발표(TMN·TMX), 없으면 시간별 기온에서
    def tmx_tmn(d, cat, fn):
        for src in (dr, vf):
            for k, row in src.items():
                if k.startswith(d) and row.get(cat) not in (None, ""):
                    v = _f(row.get(cat))
                    if v is not None:
                        return v
        vals = [x for i, x in enumerate(temps) if times[i].replace("-", "").startswith(d) and x is not None]
        return fn(vals) if vals else None

    daily = {k: [] for k in ("time", "weather_code", "sky_label", "temperature_2m_max", "temperature_2m_min",
                             "apparent_temperature_max", "apparent_temperature_min", "precipitation_sum",
                             "snowfall_sum", "precipitation_probability_max", "wind_gusts_10m_max",
                             "wind_speed_10m_max")}
    for di, d in enumerate(days):
        idx = range(di * 24, di * 24 + 24)
        day_idx = [i for i in idx if 6 <= i % 24 <= 21]
        tmax, tmin = tmx_tmn(d, "TMX", max), tmx_tmn(d, "TMN", min)
        rh_avg = [rhs[i] for i in day_idx if rhs[i] is not None]
        ws_d = [winds[i] for i in idx if winds[i] is not None]
        wet = [ptys[i] for i in day_idx if ptys[i]]
        if wet:
            p = Counter(wet).most_common(1)[0][0]
            dcode, dlab = wmo_code(p, None, max(prec[i] for i in day_idx)), PTY_LABEL.get(p, "")
        else:
            s = Counter([skys[i] for i in day_idx if skys[i]]).most_common(1)
            s = s[0][0] if s else None
            dcode, dlab = wmo_code(0, s, 0), SKY_LABEL.get(s, "")
        wmean = sum(ws_d) / len(ws_d) if ws_d else 0.0
        fmax = feels_like(tmax, sum(rh_avg) / len(rh_avg) if rh_avg else None, wmean)
        fmin = feels_like(tmin, None, wmean)
        daily["time"].append("%s-%s-%s" % (d[:4], d[4:6], d[6:]))
        daily["weather_code"].append(dcode)
        daily["sky_label"].append(dlab)
        daily["temperature_2m_max"].append(tmax)
        daily["temperature_2m_min"].append(tmin)
        daily["apparent_temperature_max"].append(round(fmax, 1) if fmax is not None else None)
        daily["apparent_temperature_min"].append(round(fmin, 1) if fmin is not None else None)
        daily["precipitation_sum"].append(round(sum(prec[i] for i in idx), 1))
        daily["snowfall_sum"].append(round(sum(snows[i] for i in idx), 1))
        daily["precipitation_probability_max"].append(max(prob[i] for i in idx))
        daily["wind_gusts_10m_max"].append(None)
        daily["wind_speed_10m_max"].append(max(ws_d) if ws_d else None)

    return {"current": current, "daily": daily, "_source": SOURCE, "_observed": bool(obs),
            "hourly": {"time": times, "precipitation_probability": prob, "precipitation": prec, "weather_code": codes}}


async def _call(client: httpx.AsyncClient, op: str, base: datetime, nx: int, ny: int) -> list:
    p = {"serviceKey": api_key(), "pageNo": 1, "numOfRows": 1000, "dataType": "JSON",
         "base_date": base.strftime("%Y%m%d"), "base_time": base.strftime("%H%M"), "nx": nx, "ny": ny}
    r = await client.get(BASE + op, params=p)
    r.raise_for_status()
    j = r.json()
    head = ((j or {}).get("response") or {}).get("header") or {}
    if head.get("resultCode") != "00":
        raise RuntimeError("kma %s %s" % (op, head.get("resultCode")))
    return ((((j.get("response") or {}).get("body") or {}).get("items") or {}).get("item")) or []


_dayrun_cache: dict = {}


async def fetch(lat: float, lng: float, now: Optional[datetime] = None) -> dict:
    """기상청 4종을 함께 불러 요약용 형태로. 실패하면 예외(호출 쪽이 예비 원천으로 넘어간다)."""
    now = (now or datetime.now(KST)).astimezone(KST)
    nx, ny = latlon_to_grid(lat, lng)
    b_v, b_d = base_vilage(now), base_dayrun(now)
    dkey = (nx, ny, b_d.strftime("%Y%m%d%H"))
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as c:
        jobs = [_call(c, "getUltraSrtNcst", base_ncst(now), nx, ny),
                _call(c, "getUltraSrtFcst", base_ufcst(now), nx, ny),
                _call(c, "getVilageFcst", b_v, nx, ny)]
        need_day = b_d != b_v and dkey not in _dayrun_cache
        if need_day:
            jobs.append(_call(c, "getVilageFcst", b_d, nx, ny))
        res = await asyncio.gather(*jobs)
    ncst, ufcst, vil = res[0], res[1], res[2]
    if b_d == b_v:
        day = vil
    elif need_day:
        day = res[3]
        _dayrun_cache.clear()
        _dayrun_cache[dkey] = day
    else:
        day = _dayrun_cache[dkey]
    if not ncst and not ufcst and not vil:
        raise RuntimeError("kma empty")
    return to_summary_shape(ncst, ufcst, vil, day, now)
