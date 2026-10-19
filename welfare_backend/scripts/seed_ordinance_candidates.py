# -*- coding: utf-8 -*-
"""안양시 조례 근거 감면 사업 → 신규 정책 후보 (v2.7.0)

지자체 복지서비스 공공 API 에 없는 안양시 자체 감면(상·하수도 요금, 공영주차장 요금)을
조례 본문(국가법령정보센터 자치법규) 기준으로 정리해 발굴 후보와 같은 검토 큐에 넣는다.
운영 콘솔에서 사람이 승인해야 정책 항목이 된다(자동 등록하지 않는다).

    python3 -m scripts.seed_ordinance_candidates          # 후보 생성(이미 만든 것·이미 등재된 것은 건너뜀)
    python3 -m scripts.seed_ordinance_candidates --dry    # 무엇을 만들지 출력만

같은 항목은 한 번만 만든다: 상태 파일 $POLICY_DATA_DIR/discovery/ordinance_seed_state.json
"""
import argparse
import copy
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

_APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_APP))

import lcgv_collector as L  # noqa: E402  — 경로·이름 정규화·기존 제목 목록을 같이 쓴다

LAW_BASE = "https://www.law.go.kr/자치법규/"


def _ordin(name, article, serial, enforced):
    return {"name": name, "article": article, "url": LAW_BASE + name.replace(" ", ""),
            "law_id": None, "law_serial_no": serial, "law_ref_type": "ordin",
            "enforcement_date": enforced}


def _source(title, url, today, note):
    return {"title": title, "publisher": "국가법령정보센터(안양시 자치법규)", "publisher_type": "법령DB",
            "url": url, "priority": "primary",
            "official_api": {"available": False, "endpoint": None, "auth_required": False, "docs_url": None},
            "crawl": {"frequency": "monthly", "change_detection_method": "page_hash", "css_selector_hint": None,
                      "notes": note},
            "accessed": today}


def drafts(today):
    water = _ordin("안양시 수도급수 조례", "제36조 제1항 제5호", "2038719", "20250516")
    sewer = _ordin("안양시 하수도 조례", "제22조 제1항 제1호", "1862393", "20231010")
    park = _ordin("안양시 주차장 설치 및 관리 조례", "제3조 제2항, 별표 2", "2102223", "20251231")
    return [
        ("ordin:anyang:water-sewer", {
            "id": "", "leaflet_section": "기타", "leaflet_number": 0,
            "title": "안양시 상·하수도 요금 감면(심한 장애인 세대)",
            "short_summary": "심한 장애인이 있는 세대는 수도요금·하수도사용료를 세대당 월 10㎥ 이하 사용분까지 감면받습니다.",
            "category": "주거", "benefit_type": "감면",
            "supported_amount": {"rate": None, "amount": None,
                                 "scope": "세대당 월 10㎥ 이하 사용분의 수도요금(수도급수 조례 제36조①5호)과 "
                                          "하수도사용료(하수도 조례 제22조①1호)"},
            "eligibility": {"target": "안양시에서 수돗물을 쓰는 세대 중 심한 장애인이 있는 세대"
                                      "(수급자 세대, 수도요금은 18세 이하 자녀 3명 이상 가정도 해당)",
                            "residency_criteria": "안양시 급수 세대"},
            "legal_basis": [water, sewer],
            "how_to_use": {"default": "주민센터 또는 안양시 상하수도사업소에 감면 신청(장애인 등록 확인). "
                                      "감면 범위·절차는 조례 원문과 상하수도사업소 안내를 확인하세요."},
            "application": {},
            "operating_agencies": [{"region": "안양시", "agency": "안양시 상하수도사업소", "lines_or_routes": [],
                                    "apply_channel": "주민센터·상하수도사업소", "card_or_proof_type": "장애인 등록 확인",
                                    "url": water["url"], "notes": None}],
            "sources": [
                _source("안양시 수도급수 조례 제36조(수도요금 등의 감면)", water["url"], today,
                        "자치법규 일련번호 %s, 2025-05-16 일부개정(제3755호)" % water["law_serial_no"]),
                _source("안양시 하수도 조례 제22조(감면 등)", sewer["url"], today,
                        "자치법규 일련번호 %s, 2023-10-10 일부개정(제3560호)" % sewer["law_serial_no"]),
            ],
            "last_verified": today, "version": "1.0.0",
        }),
        ("ordin:anyang:parking", {
            "id": "", "leaflet_section": "기타", "leaflet_number": 0,
            "title": "안양시 공영주차장 요금 감면(장애인)",
            "short_summary": "안양시 공영주차장에서 장애인 차량은 주차요금을 감면받습니다"
                             "(심한 장애 50%·최초 2시간 면제, 심하지 않은 장애 50%).",
            "category": "교통", "benefit_type": "감면",
            "supported_amount": {"rate": "50%(심한 장애 일일·월정 70%, 병원 진료 목적 100%)", "amount": None,
                                 "scope": "심한 장애(본인 자가운전, 본인 동승 대리운전 포함): 1회 주차 50%·최초 2시간 면제, "
                                          "일일·월정 70% / 심하지 않은 장애(본인 자가운전): 50% / "
                                          "심한 장애인 병원 진료 목적: 100%(진료 영수증 제시)"},
            "eligibility": {"target": "장애인 등록 차량(장애인 사용자동차 표지). 심하지 않은 장애는 본인 자가운전일 때",
                            "residency_criteria": None},
            "legal_basis": [park],
            "how_to_use": {"default": "안양시 공영주차장 출차 시 장애인 사용자동차 표지·장애인 등록 확인으로 감면. "
                                      "병원 진료 목적 면제는 진료 영수증 제시."},
            "application": {},
            "operating_agencies": [{"region": "안양시", "agency": "안양시 공영주차장(시설관리공단 등 운영 주체)",
                                    "lines_or_routes": [], "apply_channel": "주차장 현장",
                                    "card_or_proof_type": "장애인 사용자동차 표지", "url": park["url"], "notes": None}],
            "sources": [
                _source("안양시 주차장 설치 및 관리 조례 제3조·별표 2(주차요금 감면)", park["url"], today,
                        "자치법규 일련번호 %s, 2025-12-31 일부개정(제3832호)" % park["law_serial_no"]),
            ],
            "last_verified": today, "version": "1.0.0",
        }),
    ]


def _state_path():
    return L._STATE.parent / "ordinance_seed_state.json"


def run(dry=False, today=None):
    today = today or date.today().isoformat()
    sp = _state_path()
    try:
        state = json.loads(sp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    titles = L.existing_titles()
    out = {"created": [], "skipped": []}
    L._CAND_DIR.mkdir(parents=True, exist_ok=True)
    for key, draft in drafts(today):
        if key in state or L._norm(draft["title"]) in titles:
            out["skipped"].append(key)
            continue
        cid = "C" + datetime.now().strftime("%Y%m%d%H%M%S%f")[:18]
        cand = {"candidate_id": cid, "topic": "안양시 조례 감면: %s" % draft["title"],
                "cluster_queries": [], "query_ids": [],
                "classification": {"source": "ordinance", "key": key, "sgg": "안양시", "ctpv": "경기도"},
                "draft_item": copy.deepcopy(draft), "status": "pending",
                "created_at": datetime.now().isoformat(timespec="seconds")}
        if dry:
            print(json.dumps(cand, ensure_ascii=False, indent=2))
            out["created"].append(key)
            continue
        (L._CAND_DIR / ("%s.json" % cid)).write_text(json.dumps(cand, ensure_ascii=False, indent=2), encoding="utf-8")
        state[key] = {"candidate_id": cid, "title": draft["title"], "created": today}
        out["created"].append(key)
    if not dry:
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    print(json.dumps(run(dry=a.dry), ensure_ascii=False))
