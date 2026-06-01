# crawler/test_safety_net.py
# 반영 전 회귀 가드 + 스키마 강화 통합 테스트.
# 실행: python test_safety_net.py   (또는 pytest)
import json
from pathlib import Path

try:
    from .confirm_apply import _regression_check
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from crawler.confirm_apply import _regression_check  # type: ignore

POLICY_DB = Path(__file__).resolve().parent.parent  # policy_db/


def _doc():
    return {"id": "B001", "title": "t", "short_summary": "s",
            "sources": [{"a": 1}, {"b": 2}], "version": "1.0.0",
            "faq": [{"q": "1"}, {"q": "2"}, {"q": "3"}, {"q": "4"}],
            "legal_basis": [{"name": "a"}, {"name": "b"}],
            "filler": "x" * 200}


def test_regression_ok_small_change():
    old = _doc()
    new = dict(old); new["short_summary"] = "s2"
    assert _regression_check(old, new) == []


def test_regression_missing_required_key():
    old = _doc()
    new = dict(old); del new["sources"]
    assert any("필수 키 누락" in i for i in _regression_check(old, new))


def test_regression_size_shrink():
    old = _doc()
    new = {"id": "B001", "title": "t", "version": "1.0.0"}
    assert any("크기 급감" in i for i in _regression_check(old, new))


def test_regression_array_shrink():
    old = _doc()
    new = dict(old); new["faq"] = [{"q": "1"}]
    assert any("배열 급감" in i and "faq" in i for i in _regression_check(old, new))


def test_schema_validates_all_items():
    import jsonschema
    schema = json.loads((POLICY_DB / "schema.json").read_text(encoding="utf-8"))
    v = jsonschema.Draft7Validator(schema)
    items = sorted((POLICY_DB / "items").glob("B*.json"))
    assert items, "no items found"
    for f in items:
        d = json.loads(f.read_text(encoding="utf-8"))
        errs = list(v.iter_errors(d))
        assert not errs, (f.name, [e.message[:60] for e in errs[:2]])


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("PASS " + fn.__name__)
    print("\n%d passed" % len(fns))
