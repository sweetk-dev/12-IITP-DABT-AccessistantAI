# -*- coding: utf-8 -*-
"""legal_basis 법령ID 표준 매핑 회귀 테스트 (#164, 의존성 없이 단독 실행).

    python3 tests/test_legal_basis_mapping.py

여기서 지키는 계약:
  1) 표기 정제 — 중점문자 통일, 이름에 섞인 조문 분리, 주석성 괄호 제거
  2) target 분류 — 법령/행정규칙/자치법규/비법령 판정과 수기 확정(MANUAL_OVERRIDE)
  3) 아이템 반영 결과 — 모든 legal_basis 항목이 law_ref_type·mapping_status 를 갖고,
     비법령(none)은 not_applicable 로 표기된다 (법령ID 매핑률 실측: 2026-07-30, 96.0%)
  4) 임베딩 청크 오염 방지 — 법적 근거 청크에는 표시 필드(name/article/url)만 남기고
     기계판독 매핑 메타(law_id 등)는 제외한다
"""
import glob
import json
import os
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "policy_db"))

from enrich_legal_basis import (  # noqa: E402
    MANUAL_OVERRIDE, classify_target, normalize_name,
)

# ingest_sync 는 psycopg2/pgvector 를 모듈 수준에서 import 한다 — 청크 로직만 검증하므로 스텁
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
        setattr(mod, k, v)

import ingest_sync  # noqa: E402

ITEMS_DIR = str(ROOT / "policy_db" / "items")


class NormalizeTests(unittest.TestCase):
    def test_middot_unified(self):
        name, _, _ = normalize_name("장애인ㆍ노인ㆍ임산부 등의 편의증진 보장에 관한 법률")
        self.assertEqual(name, "장애인·노인·임산부 등의 편의증진 보장에 관한 법률")

    def test_article_split_from_name(self):
        name, art, flags = normalize_name("방송법 시행령 제43조")
        self.assertEqual(name, "방송법 시행령")
        self.assertEqual(art, "제43조")
        self.assertIn("article_split", flags)

    def test_annotation_paren_stripped(self):
        name, _, flags = normalize_name("장애등급판정기준 (보건복지부 고시)")
        self.assertEqual(name, "장애등급판정기준")
        self.assertIn("annotation_stripped", flags)


class ClassifyTests(unittest.TestCase):
    def test_law_types(self):
        self.assertEqual(classify_target("장애인복지법", "장애인복지법"), "law")
        self.assertEqual(classify_target("장애인복지법 시행령", "장애인복지법 시행령"), "law")
        self.assertEqual(classify_target("도시가스사업법 시행규칙", "도시가스사업법 시행규칙"), "law")

    def test_admrul_ordin_none(self):
        self.assertEqual(classify_target("x", "국민건강보험 보험료 경감고시"), "admrul")
        self.assertEqual(classify_target("x", "서울특별시 장애인 지원 조례"), "ordin")
        self.assertEqual(classify_target("x", "KTX 여객운송약관"), "none")
        self.assertEqual(classify_target("x", "장애인복지 사업안내"), "none")

    def test_manual_override_documented(self):
        # 리서치로 확정한 3건 — 전부 행정규칙
        for name in MANUAL_OVERRIDE:
            self.assertEqual(MANUAL_OVERRIDE[name], "admrul")


class AppliedItemsTests(unittest.TestCase):
    """items/B*.json 반영 상태 — 스키마 필드 존재·일관성."""

    def _iter_lb(self):
        for fp in sorted(glob.glob(os.path.join(ITEMS_DIR, "B*.json"))):
            data = json.load(open(fp, encoding="utf-8"))
            for lb in data.get("legal_basis") or []:
                yield fp, lb

    def test_every_entry_has_type_and_status(self):
        cnt = 0
        for fp, lb in self._iter_lb():
            cnt += 1
            self.assertIn(lb.get("law_ref_type"), ("law", "admrul", "ordin", "none"),
                          msg=f"{fp}: {lb.get('name')}")
            self.assertIn(lb.get("mapping_status"),
                          ("matched", "fuzzy", "not_found", "not_applicable"),
                          msg=f"{fp}: {lb.get('name')}")
        self.assertGreater(cnt, 100)

    def test_none_is_not_applicable_and_matched_has_id(self):
        for fp, lb in self._iter_lb():
            if lb.get("law_ref_type") == "none":
                self.assertEqual(lb.get("mapping_status"), "not_applicable",
                                 msg=f"{fp}: {lb.get('name')}")
            if lb.get("mapping_status") == "matched":
                self.assertTrue(lb.get("law_id"), msg=f"{fp}: {lb.get('name')}")
                self.assertTrue(lb.get("law_serial_no"), msg=f"{fp}: {lb.get('name')}")


class ChunkFilterTests(unittest.TestCase):
    """법적 근거 청크에 매핑 메타가 새지 않아야 한다."""

    DATA = {
        "id": "B999", "title": "테스트",
        "legal_basis": [{
            "name": "장애인복지법", "article": "제30조", "url": None,
            "law_ref_type": "law", "law_id": "000187", "law_serial_no": "281941",
            "enforcement_date": "20260701", "mapping_status": "matched",
            "mapping_confidence": "",
        }],
    }

    def test_chunk_keeps_display_fields_only(self):
        chunks = ingest_sync.extract_chunks(self.DATA)
        lb_chunks = [c for c in chunks if c["type"] == "legal_basis"]
        self.assertEqual(len(lb_chunks), 1)
        content = lb_chunks[0]["content"]
        self.assertIn("장애인복지법", content)
        self.assertIn("제30조", content)
        for token in ("law_id", "mapping_status", "281941", "law_ref_type"):
            self.assertNotIn(token, content)

    def test_empty_legal_basis_makes_no_chunk(self):
        chunks = ingest_sync.extract_chunks({"id": "B998", "title": "빈", "legal_basis": []})
        self.assertEqual([c for c in chunks if c["type"] == "legal_basis"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
