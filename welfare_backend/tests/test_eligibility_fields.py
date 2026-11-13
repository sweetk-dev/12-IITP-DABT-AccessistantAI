# -*- coding: utf-8 -*-
"""자격요건 구조화 칸(소득기준 여부·나이 범위·중증도) 회귀 테스트.

    python3 -m pytest -q tests/test_eligibility_fields.py

지키는 계약:
  1) 명시 칸(has_income_criteria·age_min·age_max·severity_levels)이 있으면 그 값을 그대로 쓴다
  2) 명시 칸이 없을 때 "소득기준 없음"·"소득 무관" 같은 문구를 '있음'으로 보지 않는다
  3) 나이는 원칙 문장에서만, 포함 경계(미만 N → N-1)로 뽑고 "연령 제한 없음"·"차등" 은 비운다
  4) 적재(ingest_sync)가 위 값을 DB 칸에 넣는다
  5) 중증도 검색 조건은 중증도 칸이 빈(제한 없음) 정책도 포함한다
"""
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "policy_db"))

import eligibility_fields as ef  # noqa: E402

# --- 2) 소득기준 문구 -------------------------------------------------------
INCOME_CASES = [
    ("소득기준 없음 — 신청 단계에서는 소득 무관. 단, 본인부담금이 소득 수준에 따라 차등 부과", False),
    ("소득기준 없음(전액 지원). 부모 소득·재산과 무관.", False),
    ("소득 기준 없음(중증장애인 자격만으로 신청 가능). 단, 기초생활수급자·차상위계층 자격을 동시에 보유한 경우", False),
    ("중증장애인은 소득 기준 없이 신청 가능. 단, 장애수당 수급자도 대상", False),
    ("건강보험 가입자는 소득 무관. 의료급여 100% 지원은 의료급여 수급권자에 한함.", False),
    ("해당 없음 (소득과 무관하게 지원, 서울시 기준)", False),
    ("장애인 특수교육비는 부양가족의 소득 제한 없이 공제 가능함.", False),
    ("기준 중위소득 120% 이하. 가구원수별 기준", True),
    ("기초생활보장 수급자 또는 차상위계층(소득인정액 기준 중위소득 50% 이하)", True),
    ("2026년 선정기준액: 단독가구 월 140만원 이하", True),
    (None, False),
    ("", False),
]


@pytest.mark.parametrize("text,expected", INCOME_CASES)
def test_income_flag_from_text(text, expected):
    assert ef.derive_income_flag(text) is expected


def test_income_flag_from_target_when_no_income_text():
    assert ef.derive_income_flag(None, "중위소득 80%이하 등록장애인을 지원") is True
    assert ef.derive_income_flag(None, "생계 또는 의료급여 수급자 중 중증 장애인 가구") is True
    assert ef.derive_income_flag(None, "안양시에 주민등록을 두고 거주하는 등록 장애인") is False


# --- 3) 나이 ---------------------------------------------------------------
AGE_CASES = [
    ("만 6세 이상 ~ 만 65세 미만이 원칙. ① 만 65세 도달 시 노인장기요양보험으로 이관", (6, 64)),
    ("만 18세 이상(18세가 되는 생일 달의 다음 달부터 적용 가능). 단, 만 18세 ~ 만 20세 이하이면서 학교", (18, None)),
    ("자녀(또는 손자녀) 만 12세 미만(생일 기준, 12세 도래 월까지). 12세 생일이 있는 달까지만", (None, 11)),
    ("원칙: 만 12세 이하의 미취학 장애아동. 예외 인정: 만 5세 이하", (None, 12)),
    ("만 18세 미만(단, 학교에 재학 중인 경우 만 20세가 되는 달까지 지원 연장 가능)", (None, 17)),
    ("여성장애인 본인의 연령 제한 없음. 단, 미성년자(만 19세 미만) 신청 시 친권자가 대리신청.", (None, None)),
    ("연령 제한 없음. 단, 만 65세 이상이 되어 장기요양 수급자로 결정되면 해당 경감은 종료", (None, None)),
    ("시설 종류별 차등 — 장애영유아 거주시설은 만 6세 미만(연장 시 만 18세까지). 그 외 시설은 만 18세 이상", (None, None)),
    ("품목별 상이. 예: 전동휠체어는 만 6세 이상", (None, None)),
    ("경기도에 주민등록을 둔 13세 이상 64세 이하", (13, 64)),
    ("만2세 이상(25개월) ~ 만64세 이하", (2, 64)),
    (None, (None, None)),
]


@pytest.mark.parametrize("text,expected", AGE_CASES)
def test_age_bounds(text, expected):
    assert ef.parse_age_bounds(text) == expected


def test_age_min_greater_than_max_is_dropped():
    assert ef.parse_age_bounds("만 18세 이상 만 6세 미만") == (None, None)


# --- 중증도 ------------------------------------------------------------------
def test_severity_from_target():
    assert ef.derive_severity("경기도 거주 심한 장애인, 19세~23세") == [ef.SEVERE]
    assert ef.derive_severity("생계 또는 의료급여 수급자 중 중증(1~2급) 장애인 가구") == [ef.SEVERE]
    assert ef.derive_severity("심한장애 100만원 이내, 심하지 않은 장애 70만원 이내") == ef.SEVERITY_ALL
    assert ef.derive_severity("장애인 등록 차량. 심하지 않은 장애는 본인 자가운전일 때") == [ef.MILD]
    assert ef.derive_severity("안양시 거주 등록 장애인") == []


# --- 1) 명시 칸 우선 · 초안 채우기 ---------------------------------------------
def test_explicit_fields_win_over_text():
    e = {"income_criteria": "기준 중위소득 120% 이하", "has_income_criteria": False,
         "age_criteria": "만 6세 이상", "age_min": None, "age_max": None,
         "severity_levels": [], "target": "심한 장애인"}
    assert ef.structured_fields(e) == (False, None, None, [])


def test_fill_missing_keeps_existing_values():
    e = {"target": "경기도 거주 심한 장애인 중 기준 중위소득 120% 이하", "has_income_criteria": False}
    ef.fill_missing(e)
    assert e["has_income_criteria"] is False          # 있던 값 유지
    assert e["severity_levels"] == [ef.SEVERE]         # 비어 있던 칸은 추정
    assert e["age_min"] is None and e["age_max"] is None


# --- 4) 적재 ----------------------------------------------------------------
def _import_ingest():
    for name, attrs in (
        ("psycopg2", {"connect": lambda *a, **k: None}),
        ("psycopg2.extras", {"Json": dict}),
        ("pgvector", {}),
        ("pgvector.psycopg2", {"register_vector": lambda *a, **k: None}),
        ("requests", {"post": lambda *a, **k: None}),
        ("dotenv", {"load_dotenv": lambda *a, **k: None}),
    ):
        mod = sys.modules.setdefault(name, types.ModuleType(name))
        for k, v in attrs.items():
            if not hasattr(mod, k):
                setattr(mod, k, v)
    import ingest_sync
    return ingest_sync


class _Cur:
    def __init__(self):
        self.params = []

    def execute(self, q, params=None):
        self.params.append((q, params))


class _Conn:
    def commit(self):
        pass

    def rollback(self):
        pass


def test_ingest_writes_structured_fields(tmp_path):
    ingest_sync = _import_ingest()
    item = {"id": "B998", "title": "시험 정책", "active": False,
            "eligibility": {"income_criteria": "소득기준 없음 — 신청 단계에서는 소득 무관.",
                            "age_criteria": "만 6세 이상 ~ 만 65세 미만이 원칙.",
                            "severity_levels": [ef.SEVERE]}}
    f = tmp_path / "B998_test.json"
    import json
    f.write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")
    cur = _Cur()
    assert ingest_sync.process_file(str(f), "h", cur, _Conn()) is True
    q, p = cur.params[0]
    assert "INSERT INTO welfare_policies" in q
    # (…, severity_levels, has_companion_benefit, has_income_criteria, age_min, age_max, …)
    assert p[7] == [ef.SEVERE]
    assert p[9] is False and p[10] == 6 and p[11] == 64


# --- 5) 중증도 검색 조건 -------------------------------------------------------
def test_severity_filter_includes_unrestricted():
    models = pytest.importorskip("models")
    from sqlalchemy.dialects import postgresql
    sql = str(models.severity_filter(ef.SEVERE).compile(dialect=postgresql.dialect()))
    assert "@>" in sql and "cardinality" in sql and "IS NULL" in sql


# --- 등록 전 초안 보정(콘솔 승인·발굴·보강 경로) ----------------------------------
def test_coerce_fills_eligibility_fields():
    dc = pytest.importorskip("discovery_core")
    d = dc._coerce_schema_shapes({"eligibility": "경기도 거주 심한 장애인 중 기준 중위소득 120% 이하"})
    e = d["eligibility"]
    assert e["target"].startswith("경기도")
    assert e["has_income_criteria"] is True
    assert e["severity_levels"] == [ef.SEVERE]
