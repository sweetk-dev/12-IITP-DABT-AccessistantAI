# -*- coding: utf-8 -*-
"""지자체 복지서비스 수집기(lcgv_collector) 회귀 테스트 (v2.7.0) — 네트워크 없이 돈다.

계약:
  1) 장애인 관련만 고른다(서비스명·요약·대상 구분의 '장애')
  2) 초안은 정책 스키마를 통과하고, 금액이 적힌 경우만 amount 를 채운다(추측하지 않는다)
  3) 정책 DB 에 같은 이름이 있으면 후보를 만들지 않는다(exists)
  4) 한 번 후보로 만든 사업은 다시 만들지 않고, 원천 수정일이 바뀌면 changed 로 센다
  5) 인증키가 없으면 error, 상세 실패는 그 건만 건너뛴다
  6) 상세 XML 의 근거법령·문의처 목록을 읽는다
"""
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import lcgv_collector as L  # noqa: E402

ROWS = [
    {"servId": "WLF1", "servNm": "중증장애인 콜 승합차 운영", "sggNm": "안양시", "servDgst": "이동편의",
     "trgterIndvdlNmArray": "장애인", "lastModYmd": "20260723", "servDtlLink": "https://www.bokjiro.go.kr/x?id=WLF1"},
    {"servId": "WLF2", "servNm": "청년 월세 지원", "sggNm": "안양시", "servDgst": "청년", "lastModYmd": "20260716"},
    {"servId": "WLF3", "servNm": "경기도 장애인 누림통장", "sggNm": "", "servDgst": "자산형성", "lastModYmd": "20260729",
     "servDtlLink": "https://www.bokjiro.go.kr/x?id=WLF3"},
]
DET = {
    "WLF1": {"servNm": "중증장애인 콜 승합차 운영", "sggNm": "안양시", "ctpvNm": "경기도", "bizChrDeptNm": "안양시 장애인복지과",
             "servDgst": "이동편의", "srvPvsnNm": "현물지급", "sprtTrgtCn": "안양시 거주 장애인", "slctCritCn": "",
             "alwServCn": "외출 시 차량 지원", "aplyMtdCn": "예약제", "lastModYmd": "20260723", "sprtCycNm": "수시",
             "servDtlLink": "https://www.bokjiro.go.kr/x?id=WLF1", "laws": ["장애인복지법", "법률.pdf"],
             "contacts": [("장애인복지과", "031-000-0000")], "homepages": []},
    "WLF3": {"servNm": "경기도 장애인 누림통장", "sggNm": "", "ctpvNm": "경기도", "bizChrDeptNm": "경기도 장애인복지과",
             "servDgst": "자산형성", "srvPvsnNm": "현금지급", "sprtTrgtCn": "근로 장애인", "slctCritCn": "",
             "alwServCn": "월 10만원 적립 시 1:1 매칭", "aplyMtdCn": "온라인", "lastModYmd": "20260729",
             "servDtlLink": "https://www.bokjiro.go.kr/x?id=WLF3", "laws": [], "contacts": [], "homepages": []},
}


def _setup(tmp_path, monkeypatch, titles=()):
    data = tmp_path / "data"
    (data / "items").mkdir(parents=True)
    for i, t in enumerate(titles):
        (data / "items" / ("B9%02d.json" % i)).write_text(json.dumps({"title": t}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(L, "_ITEMS", data / "items")
    monkeypatch.setattr(L, "_CAND_DIR", data / "discovery" / "candidates")
    monkeypatch.setattr(L, "_STATE", data / "discovery" / "lcgv_state.json")
    monkeypatch.setenv("LCGV_API_KEY", "k")
    monkeypatch.setattr(L, "fetch_list", lambda ctpv, sgg="": [r for r in ROWS if (r["sggNm"] == sgg) or (not sgg)])
    monkeypatch.setattr(L, "fetch_detail", lambda sid: DET[sid])
    return data


def test_filter_and_candidates(tmp_path, monkeypatch):
    data = _setup(tmp_path, monkeypatch)
    out = L.run(ctpv="경기도", sggs=["안양시"])
    assert out["disability"] == 2 and out["new"] == 2 and out["errors"] == 0
    files = sorted((data / "discovery" / "candidates").glob("C*.json"))
    assert len(files) == 2
    c = json.loads(files[0].read_text(encoding="utf-8"))
    assert c["status"] == "pending" and c["classification"]["source"] == "lcgv"


def test_draft_schema_and_amount(tmp_path):
    import jsonschema
    schema = json.loads((ROOT / "policy_db" / "schema.json").read_text(encoding="utf-8"))
    d1 = L.build_draft(ROWS[0], DET["WLF1"], today="2026-10-15")
    d3 = L.build_draft(ROWS[2], DET["WLF3"], today="2026-10-15")
    for d in (d1, d3):
        d = dict(d, id="B999")
        assert not list(jsonschema.Draft7Validator(schema).iter_errors(d))
    assert d1["supported_amount"]["amount"] is None and d1["supported_amount"]["scope"] == "외출 시 차량 지원"
    assert d3["supported_amount"]["amount"] == "월 10만원 적립 시 1:1 매칭"
    assert d1["category"] == "교통" and d1["benefit_type"] == "현물지급"
    assert d3["benefit_type"] == "현금지급"
    assert [x["name"] for x in d1["legal_basis"]] == ["장애인복지법"]            # pdf 첨부 이름은 법령이 아니다
    assert "031-000-0000" in d1["operating_agencies"][0]["notes"]
    assert L.guess_category("장애인활동지원 이동 지원") == "활동지원"


def test_existing_title_and_rerun(tmp_path, monkeypatch):
    data = _setup(tmp_path, monkeypatch, titles=["경기도 장애인 누림 통장"])
    out = L.run(ctpv="경기도", sggs=["안양시"])
    assert out["new"] == 1 and out["exists"] == 1
    again = L.run(ctpv="경기도", sggs=["안양시"])
    assert again["new"] == 0 and again["changed"] == []
    ROWS[0]["lastModYmd"] = "20261001"
    try:
        third = L.run(ctpv="경기도", sggs=["안양시"])
    finally:
        ROWS[0]["lastModYmd"] = "20260723"
    assert third["changed"] == ["WLF1"] and third["new"] == 0
    assert len(list((data / "discovery" / "candidates").glob("C*.json"))) == 1


def test_no_key_and_detail_failure(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.delenv("LCGV_API_KEY")
    monkeypatch.delenv("DATA_GO_KR_API_KEY", raising=False)
    assert "error" in L.run()
    monkeypatch.setenv("LCGV_API_KEY", "k")

    def bad(sid):
        if sid == "WLF1":
            raise RuntimeError("boom")
        return DET[sid]
    monkeypatch.setattr(L, "fetch_detail", bad)
    out = L.run(ctpv="경기도", sggs=["안양시"])
    assert out["new"] == 1 and out["errors"] == 1


def test_detail_xml_lists(monkeypatch):
    xml = """<wantedDtl><resultCode>0</resultCode><servNm>X</servNm><sggNm>안양시</sggNm>
      <baslawList><wlfareInfoReldNm>장애인복지법</wlfareInfoReldNm></baslawList>
      <inqplCtadrList><wlfareInfoReldNm>장애인복지과</wlfareInfoReldNm><wlfareInfoReldCn>031-1</wlfareInfoReldCn></inqplCtadrList>
      <inqplHmpgReldList><wlfareInfoReldNm>안양시청</wlfareInfoReldNm><wlfareInfoReldCn>https://a.go.kr</wlfareInfoReldCn></inqplHmpgReldList>
    </wantedDtl>"""
    monkeypatch.setattr(L, "_get", lambda op, params: ET.fromstring(xml))
    d = L.fetch_detail("WLF9")
    assert d["laws"] == ["장애인복지법"] and d["contacts"] == [("장애인복지과", "031-1")]
    assert d["homepages"] == ["https://a.go.kr"]


def test_ordinance_seed(tmp_path, monkeypatch):
    """조례 근거 감면 후보 — 스키마 통과, 한 번만 생성, 이미 등재된 제목은 건너뜀."""
    import jsonschema
    from scripts import seed_ordinance_candidates as S
    data = _setup(tmp_path, monkeypatch, titles=["안양시 공영주차장 요금 감면(장애인)"])
    schema = json.loads((ROOT / "policy_db" / "schema.json").read_text(encoding="utf-8"))
    for _, d in S.drafts("2026-10-15"):
        errs = list(jsonschema.Draft7Validator(schema).iter_errors(dict(d, id="B999")))
        assert not errs, [e.message for e in errs]
        assert all(x["law_ref_type"] == "ordin" and x["law_serial_no"] for x in d["legal_basis"])
    out = S.run(today="2026-10-15")
    assert out["created"] == ["ordin:anyang:water-sewer"] and out["skipped"] == ["ordin:anyang:parking"]
    again = S.run(today="2026-10-15")
    assert again["created"] == []
    files = list((data / "discovery" / "candidates").glob("C*.json"))
    assert len(files) == 1
    c = json.loads(files[0].read_text(encoding="utf-8"))
    assert c["classification"]["source"] == "ordinance" and c["status"] == "pending"
