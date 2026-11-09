# -*- coding: utf-8 -*-
"""정책 갱신 패치의 목록 칸 반영 (faq.0.a · operating_agencies.3.notes 등) — v2.25.2."""
import sys
from pathlib import Path

_APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_APP / "policy_db"))

from crawler.llm_updater import _apply_patch  # noqa: E402


def _doc():
    return {
        "id": "B999",
        "supported_amount": {"rate": "정액", "amount": "100만원"},
        "faq": [{"q": "얼마인가요?", "a": "태아 1인당 100만원입니다."},
                {"q": "쌍둥이는요?", "a": "200만원입니다."}],
        "operating_agencies": [{"agency": "보건복지부 장애인서비스과", "phones": ["129"]}],
        "exceptions_and_caveats": ["사업기간 내 출산", "인공임신중절 제외"],
    }


def test_list_item_update_applies_when_old_matches():
    d = _doc()
    new, applied, review = _apply_patch(d, [
        {"op": "update", "path": "faq.0.a", "old": "태아 1인당 100만원입니다.", "new": "태아 1인당 120만원입니다."},
        {"op": "update", "path": "operating_agencies.0.agency", "old": "보건복지부 장애인서비스과", "new": "보건복지부 장애인건강과"},
        {"op": "update", "path": "exceptions_and_caveats.1", "old": "인공임신중절 제외", "new": "인공임신중절 제외(모자보건법 허용 사유는 지원)"},
    ])
    assert [a["path"] for a in applied] == ["faq.0.a", "operating_agencies.0.agency", "exceptions_and_caveats.1"]
    assert not review
    assert new["faq"][0]["a"] == "태아 1인당 120만원입니다."
    assert new["operating_agencies"][0]["agency"] == "보건복지부 장애인건강과"
    assert new["exceptions_and_caveats"][1].startswith("인공임신중절 제외(")
    assert d["faq"][0]["a"] == "태아 1인당 100만원입니다."          # 원본 불변


def test_wrong_index_goes_to_review():
    """모델이 목록 번호를 잘못 센 경우 — 기존 값이 다르면 덮어쓰지 않는다."""
    new, applied, review = _apply_patch(_doc(), [
        {"op": "update", "path": "faq.1.a", "old": "태아 1인당 100만원입니다.", "new": "120만원"}])
    assert not applied and review[0]["reason"].startswith("목록 칸 기존 값 불일치")
    assert new["faq"][1]["a"] == "200만원입니다."


def test_list_item_without_old_goes_to_review():
    _, applied, review = _apply_patch(_doc(), [{"op": "update", "path": "faq.0.a", "new": "x"}])
    assert not applied and review


def test_old_prefix_match_and_whitespace():
    long = "가" * 50 + " 끝"
    d = _doc()
    d["faq"][0]["a"] = long
    new, applied, _ = _apply_patch(d, [{"op": "update", "path": "faq.0.a", "old": "가" * 40 + "…", "new": "바뀜"}])
    assert applied and new["faq"][0]["a"] == "바뀜"


def test_out_of_range_and_bad_index():
    _, applied, review = _apply_patch(_doc(), [
        {"op": "update", "path": "faq.9.a", "old": "x", "new": "y"},
        {"op": "update", "path": "faq.x.a", "old": "x", "new": "y"},
        {"op": "update", "path": "exceptions_and_caveats.5", "old": "x", "new": "y"}])
    assert not applied and len(review) == 3
    assert all(r["reason"] == "update 경로 없음" for r in review)


def test_add_into_nested_list():
    new, applied, review = _apply_patch(_doc(), [
        {"op": "add", "path": "operating_agencies.0.phones", "value": "044-202-0000"},
        {"op": "add", "path": "faq", "value": {"q": "새 질문", "a": "답"}},
        {"op": "add", "path": "operating_agencies.0.email", "value": "a@b.c"}])
    assert len(applied) == 3 and not review
    assert new["operating_agencies"][0]["phones"] == ["129", "044-202-0000"]
    assert new["faq"][-1]["q"] == "새 질문" and new["operating_agencies"][0]["email"] == "a@b.c"


def test_add_cannot_create_list_slot():
    _, applied, review = _apply_patch(_doc(), [{"op": "add", "path": "faq.5", "value": {"q": "x"}},
                                               {"op": "add", "path": "faq.0", "value": "x"}])
    assert not applied and len(review) == 2


def test_dict_paths_unchanged():
    new, applied, review = _apply_patch(_doc(), [
        {"op": "update", "path": "supported_amount.amount", "new": "120만원"},
        {"op": "update", "path": "supported_amount.nope", "new": "x"}])
    assert [a["path"] for a in applied] == ["supported_amount.amount"]
    assert review[0]["reason"] == "update 경로 없음" and new["supported_amount"]["amount"] == "120만원"


def test_two_patches_same_item_compare_against_original():
    new, applied, review = _apply_patch(_doc(), [
        {"op": "update", "path": "faq.0.a", "old": "태아 1인당 100만원입니다.", "new": "120만원"},
        {"op": "update", "path": "faq.0.q", "old": "얼마인가요?", "new": "지원금은 얼마인가요?"}])
    assert len(applied) == 2 and new["faq"][0] == {"q": "지원금은 얼마인가요?", "a": "120만원"}
