# crawler/test_updater.py
# 필드 단위 패치 적용 회귀 테스트 — 전체 재생성 대신 패치만 적용해
# 미변경 필드가 보존되는지, delete 가 자동 적용되지 않는지 검증한다.
# 실행: python test_updater.py   (또는 pytest)
from pathlib import Path

try:
    from .claude_updater import _apply_patch, _has_termination_evidence, _set_by_path, _add_by_path
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from crawler.claude_updater import _apply_patch, _has_termination_evidence, _set_by_path, _add_by_path  # type: ignore


def _sample():
    return {
        "id": "B001",
        "title": "지하철 요금 지원",
        "supported_amount": {"rate": "100%", "scope": "전 구간"},
        "eligibility": {"target": "등록 장애인", "income_criteria": None},
        "faq": [{"q": "환승 적용?", "a": "네"}],
        "version": "1.0.0",
    }


def test_update_changes_only_named_field():
    existing = _sample()
    patches = [{"op": "update", "path": "supported_amount.rate", "new": "90%"}]
    new_doc, applied, review = _apply_patch(existing, patches)
    assert new_doc["supported_amount"]["rate"] == "90%"
    # 미변경 필드 보존
    assert new_doc["supported_amount"]["scope"] == "전 구간"
    assert new_doc["title"] == "지하철 요금 지원"
    assert new_doc["faq"] == [{"q": "환승 적용?", "a": "네"}]
    assert new_doc["eligibility"] == {"target": "등록 장애인", "income_criteria": None}
    # 원본 불변(deepcopy)
    assert existing["supported_amount"]["rate"] == "100%"
    assert len(applied) == 1 and review == []


def test_add_appends_to_list():
    existing = _sample()
    patches = [{"op": "add", "path": "faq", "value": {"q": "신규?", "a": "추가"}}]
    new_doc, applied, review = _apply_patch(existing, patches)
    assert len(new_doc["faq"]) == 2
    assert new_doc["faq"][1] == {"q": "신규?", "a": "추가"}
    assert existing["faq"] == [{"q": "환승 적용?", "a": "네"}]  # 원본 불변
    assert len(applied) == 1 and review == []


def test_empty_patch_keeps_doc_identical():
    existing = _sample()
    import copy
    before = copy.deepcopy(existing)
    new_doc, applied, review = _apply_patch(existing, [])
    assert new_doc == before
    assert applied == [] and review == []


def test_delete_not_applied_goes_to_review():
    existing = _sample()
    patches = [{"op": "delete", "path": "faq", "evidence": "단순히 안 보임"}]
    new_doc, applied, review = _apply_patch(existing, patches)
    # 삭제 자동 적용 금지 — 필드 그대로 존재
    assert "faq" in new_doc and new_doc["faq"] == existing["faq"]
    assert applied == []
    assert len(review) == 1 and review[0]["classification"] == "review_needed"


def test_delete_with_termination_is_candidate():
    existing = _sample()
    patches = [{"op": "delete", "path": "supported_amount",
                "evidence": "2026년부터 본 제도는 폐지되었습니다."}]
    new_doc, applied, review = _apply_patch(existing, patches)
    assert "supported_amount" in new_doc  # 여전히 자동 삭제 안 함
    assert review[0]["classification"] == "delete_candidate"


def test_has_termination_evidence():
    assert _has_termination_evidence("지원이 종료되었습니다") is True
    assert _has_termination_evidence("금액이 50%로 변경") is False


def test_update_missing_path_goes_to_review():
    existing = _sample()
    patches = [{"op": "update", "path": "nonexistent.field", "new": "x"}]
    new_doc, applied, review = _apply_patch(existing, patches)
    assert applied == [] and len(review) == 1


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("PASS " + fn.__name__)
    print("\n%d passed" % len(fns))
