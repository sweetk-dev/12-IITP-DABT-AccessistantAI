# -*- coding: utf-8 -*-
"""하차역 역 안/밖 안내·요청 출처 태그·추천 10건·관리기관 번호 (v1.50.0, #298 · 02 v1.27.0).

    python3 tests/test_egress_client_tag.py

계약:
  1) nav_state.station_egress — 허용 필드·길이로 정리되고, 안내 중 get_current_guidance 에 실린다
  2) find_bf_tour_spots — 현재 위치가 있으면 origin 주입, 기본 10건, category=tour 로 요청
  3) 요청 출처 태그 — 정규화, 경로 서버 헤더(X-Client-Tag)와 호출 로그(client)에 실린다
  4) 긴급대응 카드에 관리기관 번호 표시(tel_owner)·지침, 화장실 카드에 시설 내 화장실 표시
"""
import asyncio
import json
import os
import sys
import tempfile
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
import tool_handlers                                       # noqa: E402
from nav_context import (update_nav_state, current_guidance_result,   # noqa: E402
                         inject_nav_defaults)

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


EG = {"station": "관악", "exit_no": "2", "where": "inside",
      "inside": ["내린 승강장 쪽에는 휠체어리프트만 있습니다", "2번 출구 승강기로 나갑니다"],
      "outside": "관악역 2번 출구 앞에서 도보 안내를 시작합니다", "hack": "drop"}


# ── 1. station_egress ─────────────────────────────────────────
def t_egress_sanitized_and_in_guidance():
    nav = {}
    update_nav_state(nav, {"route_id": "r1", "guiding": True, "step_idx": 2, "total_steps": 6,
                           "current": "관악역에서 하차합니다", "station_egress": EG})
    eg = nav["station_egress"]
    assert set(eg) == {"station", "exit_no", "where", "inside", "outside"}, eg
    assert eg["where"] == "inside" and len(eg["inside"]) == 2
    r = current_guidance_result(nav)
    assert r["station_egress"]["exit_no"] == "2"
    assert "station_egress" in r["ai_instruction"] and "inside" in r["ai_instruction"]


def t_egress_bad_values_dropped():
    nav = {}
    update_nav_state(nav, {"guiding": True, "station_egress": {"station": "", "inside": []}})
    assert nav["station_egress"] is None
    update_nav_state(nav, {"guiding": True, "station_egress": "x"})
    assert nav["station_egress"] is None
    update_nav_state(nav, {"guiding": True, "station_egress": dict(EG, where="hacked", inside=["a" * 500])})
    assert nav["station_egress"]["where"] is None and len(nav["station_egress"]["inside"][0]) == 160
    r = current_guidance_result({"guiding": True, "step_idx": 0, "total_steps": 3})
    assert "station_egress" not in r["ai_instruction"]


# ── 2. 관광지 추천 ────────────────────────────────────────────
def t_tour_origin_injected():
    a = inject_nav_defaults("find_bf_tour_spots", {"topk": 5}, {}, {"lat": 37.4, "lng": 126.9})
    assert a["origin_lat"] == 37.4 and a["origin_lng"] == 126.9
    a = inject_nav_defaults("find_bf_tour_spots", {"origin_lat": 1, "origin_lng": 2}, {}, None)
    assert "origin_lat" not in a, "위치를 모르면 모델이 준 좌표도 버린다"


def t_tour_default_10_and_category():
    seen = {}

    async def fake_call(method, path, *, params=None, json=None):
        seen["path"], seen["json"] = path, json
        return {"total": 1, "has_more": False, "items": [
            {"poi_id": "14792", "name": "김중업 건축박물관", "lat": 37.41, "lng": 126.91, "distance_m": 900,
             "facilities": {"toilet_yn": True}, "score": 0.8, "category": "tour", "category_label": "관광지"}]}
    orig = route_client._call
    route_client._call = fake_call
    try:
        r = _run(tool_handlers.tool_find_bf_tour_spots(disabilities=["지체장애"]))
    finally:
        route_client._call = orig
    assert seen["path"] == "/tour/recommend"
    assert seen["json"]["topk"] == 10 and seen["json"]["category"] == "tour", seen["json"]
    assert r["results"][0]["category"] == "관광지"


# ── 3. 요청 출처 태그 ─────────────────────────────────────────
def t_client_tag_normalized_header_and_log():
    assert route_client.normalize_client_tag("trial_p1<script>") == "trial_p1script"
    assert route_client.normalize_client_tag("") is None
    route_client.set_client_tag("trial_p1")
    try:
        h = route_client._headers()
        assert h.get("X-Client-Tag") == "trial_p1", h
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "rc.jsonl")
            saved = (route_client.CALL_LOG_PATH, route_client._call_log_fh, route_client._call_log_disabled)
            route_client.CALL_LOG_PATH, route_client._call_log_fh, route_client._call_log_disabled = path, None, False
            try:
                route_client._call_log({"path": "/route/plan", "status": 200, "ms": 12.0})
                route_client._call_log_fh.close()
            finally:
                route_client.CALL_LOG_PATH, route_client._call_log_fh, route_client._call_log_disabled = saved
            rec = json.loads(open(path, encoding="utf-8").read().strip())
            assert rec["client"] == "trial_p1", rec
    finally:
        route_client.set_client_tag(None)
    assert "X-Client-Tag" not in route_client._headers()


def t_recommend_log_has_category():
    out = route_client._summarize_for_log("/tour/recommend", {"topk": 10, "category": "tour"}, {"total": 3, "items": []})
    assert out["category"] == "tour" and out["topk"] == 10


# ── 4. 긴급·화장실 카드 ───────────────────────────────────────
def t_support_manager_phone():
    async def fake(lat, lng, types="", radius_m=2000, limit=3, **kw):
        return {"items": [{"support_type": "charge", "name": "만안구청", "dist_m": 180, "tel": "031-455-1313",
                           "tel_owner": "manager", "tel_owner_name": "온누리 부흥센터", "open_hours": "09-18",
                           "open_hours_status": "known", "lat": 37.38, "lng": 126.93}]}
    orig = route_client.support_nearby
    route_client.support_nearby = fake
    try:
        r = _run(tool_handlers.tool_find_emergency_support(lat=37.38, lng=126.93, types="charge"))
    finally:
        route_client.support_nearby = orig
    it = r["items"][0]
    assert it["tel_owner"] == "manager" and it["tel_owner_name"] == "온누리 부흥센터"
    assert "관리기관" in r["ai_instruction"]


def t_toilet_facility_flag():
    async def fake(lat, lng, radius_m=800, limit=5, accessible_only=True, **kw):
        return {"items": [{"name": "김중업 건축박물관 (시설 내 장애인화장실)", "dist_m": 20, "accessible": True,
                           "facility_toilet": True, "open_time": "시설 운영시간 내", "lat": 37.41, "lng": 126.91}]}
    orig = route_client.toilet_nearby
    route_client.toilet_nearby = fake
    try:
        r = _run(tool_handlers.tool_find_toilet(lat=37.41, lng=126.91))
    finally:
        route_client.toilet_nearby = orig
    assert r["items"][0]["facility_toilet"] is True
    assert "시설 운영시간" in r["ai_instruction"]


if __name__ == "__main__":
    for nm, fn in sorted((k, v) for k, v in list(globals().items()) if k.startswith("t_")):
        check(nm, fn)
    print()
    if FAILS:
        print("FAILED: %d" % len(FAILS))
        sys.exit(1)
    print("all passed")
