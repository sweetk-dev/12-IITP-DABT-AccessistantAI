# -*- coding: utf-8 -*-
"""지역 통계 조회 도구(lookup_local_stats) 회귀 테스트 (v2.7.0).

    python3 -m pytest tests/test_local_stats_tool.py

계약:
  1) 지역 이름 정리 — '안양'·'안양시' → 안양시, '만안구'·'동안구' → 안양시 + 구
  2) 주제 고르기 — topic 이 있으면 그대로, 없으면 사용자 말로(시설·의료·고용·버스), 아무것도 아니면 등록장애인
  3) 등록장애인 — 두 원천을 원천·기준 시점과 함께 싣고, 구를 물으면 그 구 값을 따로 싣는다
  4) 오해 쉬운 통계(의료이용·의무고용·저상버스)는 고정 설명 문구가 결과와 지침에 들어간다
  5) 자료 없음(404)은 not_found + '대신 말하지 말 것' 지침, 설정이 없으면 error
  6) 설정이 있을 때만 Live·폴백 선언에 들어가고, 선언 인자를 핸들러가 모두 받는다
"""
import asyncio
import inspect
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

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

import openapi_client                                      # noqa: E402
import tool_handlers as th                                 # noqa: E402

SUMMARY = {
    "sigun_name": "안양시",
    "gg_emd": {"source_name": "경기도 내 등록장애인 집계현황", "base_dt": "2025-08-07", "total_cnt": 21744,
               "sev_cnt": 7388, "nsev_cnt": 14356,
               "by_gu": [{"name": "동안구", "total_cnt": 10538, "sev_cnt": 3687},
                         {"name": "만안구", "total_cnt": 11206, "sev_cnt": 3701}],
               "by_type": [{"name": "지체", "total_cnt": 9219}]},
    "kosis": {"source_name": "KOSIS 장애인현황", "year": 2025, "total_cnt": 21968, "sev_cnt": 7445, "nsev_cnt": 14523,
              "by_gu": [{"name": "동안구", "total_cnt": 10570, "sev_cnt": 3692},
                        {"name": "만안구", "total_cnt": 11398, "sev_cnt": 3753}]},
    "notes": ["두 원천은 기준 시점이 다릅니다"],
}


class _Fake:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def __call__(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        r = self.responses.get(path)
        if callable(r):
            return r(params or {})
        return r


def _run(responses, **kw):
    fake = _Fake(responses)
    saved = (openapi_client.get, openapi_client.BASE_URL, openapi_client.API_KEY)
    openapi_client.get = fake
    openapi_client.BASE_URL, openapi_client.API_KEY = "http://api.test", "k"
    try:
        return asyncio.run(th.tool_lookup_local_stats(**kw)), fake
    finally:
        openapi_client.get, openapi_client.BASE_URL, openapi_client.API_KEY = saved


def test_region_normalize():
    assert th._local_region("안양") == ("안양시", None, "안양")
    assert th._local_region("안양시") == ("안양시", None, "안양")
    assert th._local_region("만안구") == ("안양시", "만안구", "안양")
    assert th._local_region("안양시 동안구") == ("안양시", "동안구", "안양")
    assert th._local_region("") == ("안양시", None, "안양")
    assert th._local_region("가평군") == ("가평군", None, "가평")


def test_topic_pick():
    assert th._topic_from_query("employment", "시설") == "employment"
    assert th._topic_from_query("", "안양에 장애인 거주시설 몇 곳") == "welfare_facility"
    assert th._topic_from_query("", "의료 이용") == "medical_usage"
    assert th._topic_from_query("", "의무고용") == "employment"
    assert th._topic_from_query("", "저상버스 비율") == "low_floor_bus"
    assert th._topic_from_query("", "장애인이 몇 명이야") == "registered"


def test_registered_two_sources_and_gu():
    out, fake = _run({"/basic/housing/reg/local/summary": SUMMARY}, region="만안구", query="중증장애인 몇 명")
    assert out["status"] == "success" and out["topic"] == "registered" and out["region"] == "만안구"
    assert fake.calls[0] == ("/basic/housing/reg/local/summary", {"sigunName": "안양시"})
    srcs = out["result"]["sources"]
    assert [s["when"] for s in srcs] == ["2025-08-07 기준", "2025년 말 기준"]
    assert srcs[0]["gu_value"]["total"] == 11206 and srcs[1]["gu_value"]["total"] == 11398
    ai = out["ai_instruction"]
    assert "원천" in ai and "기준 시점" in ai and "만안구" in ai and "두 구를 더한" in ai


def test_fixed_notes():
    med = {"items": [
        {"prdDe": 2024, "c1Nm": "안양시 만안구 ", "c2Nm": "적용인구", "data": "11061.000"},
        {"prdDe": 2024, "c1Nm": "안양시 동안구 ", "c2Nm": "적용인구", "data": "10428.000"},
        {"prdDe": 2023, "c1Nm": "안양시 만안구 ", "c2Nm": "적용인구", "data": "1.000"},
        {"prdDe": 2024, "c1Nm": "광주시 ", "c2Nm": "적용인구", "data": "9.000"}]}
    out, _ = _run({"/basic/health/medicalUsg/latest": med}, topic="medical_usage", region="안양")
    r = out["result"]
    assert r["when"] == "2024년" and r["sum_covered_population"] == 21489 and len(r["areas"]) == 2
    assert "등록장애인 수가 아닙니다" in r["fixed_note"] and r["fixed_note"] in out["ai_instruction"]

    emp = [{"year": 2025, "region": "경기 안양시", "companyCount": 388, "workerCount": 90669,
            "severe2xCount": 2792, "severe2xRate": 3.08},
           {"year": 2022, "region": "경기 안양시", "severe2xRate": 2.98}]
    out, _ = _run({"/emp/emp/region": emp}, topic="employment")
    assert out["result"]["when"] == "2025년" and out["result"]["rate_percent"] == 3.08
    assert "조사 고용률" in out["ai_instruction"]

    def bus(params):
        return {"total": 63 if params.get("lowBusYn") == "Y" else 119,
                "content": [{"low_bus_base_dt": "2026-10-14"}]}
    out, _ = _run({"/transit/bus/routes/search/paging": bus}, topic="low_floor_bus")
    assert out["result"]["ratio_percent"] == 52.9 and out["result"]["when"] == "2026-10-14 기준"
    assert "차량 도입률이 아닙니다" in out["ai_instruction"]


def test_welfare_counts():
    rows = [{"sigun_name": "안양시", "facility_type": "residence", "facility_type_name": "장애유형별 거주시설",
             "name": "사랑의집", "capacity_cnt": 30, "current_cnt": 30, "checked_dt": "2026-10-15"},
            {"sigun_name": "안양시", "facility_type": "il_center", "facility_type_name": "중증장애인자립생활지원센터",
             "name": "밀알", "capacity_cnt": None, "current_cnt": None, "checked_dt": "2026-10-15"},
            {"sigun_name": "의왕시", "facility_type": "residence", "name": "x"}]
    out, fake = _run({"/poi/welfare-facility/search/paging": {"content": rows, "total": 3}}, topic="welfare_facility")
    assert fake.calls[0][1]["sigunName"] == "안양"
    r = out["result"]
    assert r["total_facilities"] == 2 and r["when"] == "2026-10-15 확인"
    assert {g["type"]: g["capacity"] for g in r["by_type"]} == {"장애유형별 거주시설": 30, "중증장애인자립생활지원센터": 0}


def test_not_found_and_disabled():
    out, _ = _run({"/basic/housing/reg/local/summary": {"status": "not_found", "message": "없음"}}, region="서울시")
    assert out["status"] == "not_found" and "대신 말하지 마세요" in out["ai_instruction"]
    saved = (openapi_client.BASE_URL, openapi_client.API_KEY)
    openapi_client.BASE_URL, openapi_client.API_KEY = "", ""
    try:
        out = asyncio.run(th.tool_lookup_local_stats(region="안양"))
    finally:
        openapi_client.BASE_URL, openapi_client.API_KEY = saved
    assert out["status"] == "error" and "지어내지" in out["ai_instruction"]


def test_declarations_follow_config_and_match_handler():
    import local_pipeline as lp
    saved = (openapi_client.BASE_URL, openapi_client.API_KEY)
    try:
        openapi_client.BASE_URL, openapi_client.API_KEY = "", ""
        assert lp._ollama_stats_tools() == []
        openapi_client.BASE_URL, openapi_client.API_KEY = "http://api.test", "k"
        decl = lp._ollama_stats_tools()
        assert [d["function"]["name"] for d in decl] == ["lookup_local_stats"]
        props = set(decl[0]["function"]["parameters"]["properties"])
        accepted = set(inspect.signature(th.tool_lookup_local_stats).parameters)
        assert props <= accepted
        assert th.get_tool_dispatcher(None)["lookup_local_stats"] is th.tool_lookup_local_stats
    finally:
        openapi_client.BASE_URL, openapi_client.API_KEY = saved


def test_review_fixes():
    """교차검토 반영: 다른 시의 구, 문자열 연도, 시 합계 행, 고용 지역 끝 일치, 저상 기준일."""
    assert th._local_region("수원시 팔달구") == ("수원시", "팔달구", "수원")
    med = {"items": [{"prdDe": "2024", "c1Nm": "안양시", "c2Nm": "적용인구", "data": "21489"},
                     {"prdDe": "2024", "c1Nm": "안양시 만안구", "c2Nm": "적용인구", "data": "11061"}]}
    out, _ = _run({"/basic/health/medicalUsg/latest": med}, topic="medical_usage", region="안양")
    assert out["result"]["when"] == "2024년" and len(out["result"]["areas"]) == 1
    emp = [{"year": 2025, "region": "광주 광산구", "severe2xRate": 9.9},
           {"year": 2024, "region": "경기 광주시", "severe2xRate": 3.1}]
    out, _ = _run({"/emp/emp/region": emp}, topic="employment", region="광주시")
    assert out["result"]["rate_percent"] == 3.1

    def bus(params):
        if params.get("lowBusYn") == "Y":
            return {"total": 1, "content": [{"low_bus_base_dt": "2026-10-14"}]}
        return {"total": 2, "content": [{"route": "1"}]}
    out, _ = _run({"/transit/bus/routes/search/paging": bus}, topic="low_floor_bus")
    assert out["result"]["when"] == "2026-10-14 기준"
