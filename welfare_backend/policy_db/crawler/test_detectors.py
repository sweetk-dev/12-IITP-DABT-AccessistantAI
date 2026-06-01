# crawler/test_detectors.py
# detectors 스냅샷 비교/저장 회귀 테스트 — 네트워크 없이 순수 헬퍼만 검증.
#
# 핵심 회귀: last_modified_field 가 "저장=해시 / 비교=원문 키" 불일치로
# 매 회차 거짓 변경(changed=True)을 내던 버그가 재발하지 않는지 확인.
#
# 실행: python test_detectors.py   (또는 pytest)
import tempfile
from pathlib import Path

try:
    from .detectors import ChangeResult, SNAPSHOT_FILES, _read_prev_hash, save_snapshot, _normalize_html_text, _hash_bytes, _chunk_html, _chunk_diff
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from crawler.detectors import ChangeResult, SNAPSHOT_FILES, _read_prev_hash, save_snapshot, _normalize_html_text, _hash_bytes, _chunk_html, _chunk_diff  # type: ignore


def _decide(prev_hash, new_hash):
    """검출기 공통 판정 규칙과 동일."""
    return (prev_hash is None) or (prev_hash != new_hash)


def test_first_run_is_changed():
    assert _decide(None, "h1") is True


def test_same_hash_is_not_changed():
    assert _decide("h1", "h1") is False


def test_different_hash_is_changed():
    assert _decide("h1", "h2") is True


def test_save_then_read_roundtrip_all_methods():
    """저장한 해시를 그대로 돌려받고, 동일 해시면 변경 없음으로 판정되어야 한다."""
    for method in SNAPSHOT_FILES:
        with tempfile.TemporaryDirectory() as d:
            snap = Path(d)
            res = ChangeResult(True, "x", None, "HASH_" + method, "u")
            save_snapshot(snap, method, res)
            prev = _read_prev_hash(snap, method)
            assert prev == "HASH_" + method, (method, prev)
            assert _decide(prev, "HASH_" + method) is False, method
            assert _decide(prev, "OTHER") is True, method


def test_last_modified_regression():
    """버그 재현 방지: 저장 후 동일 해시 비교 시 changed=False (이전엔 항상 True)."""
    with tempfile.TemporaryDirectory() as d:
        snap = Path(d)
        h = "abc123"
        save_snapshot(snap, "last_modified_field", ChangeResult(True, "x", None, h, "u"))
        prev = _read_prev_hash(snap, "last_modified_field")
        assert prev == h
        assert _decide(prev, h) is False


def test_normalize_masks_dynamic_noise():
    """본문이 같고 날짜/조회수 같은 노이즈만 다른 두 HTML 은 동일 해시를 내야 한다."""
    html_a = (b"<html><body><h1>Subway Free</h1><p>Discount 50%</p>"
              b"<span>views 1,234</span><time>2026-05-01</time>"
              b"<script>var t=1</script></body></html>")
    html_b = (b"<html><body><h1>Subway Free</h1><p>Discount 50%</p>"
              b"<span>views 9,999</span><time>2026-06-15 10:20:30</time>"
              b"<script>var t=2</script></body></html>")
    a = _normalize_html_text(html_a)
    b = _normalize_html_text(html_b)
    assert a == b, (a, b)
    assert _hash_bytes(a.encode("utf-8")) == _hash_bytes(b.encode("utf-8"))


def test_chunk_html_splits_blocks():
    html = (b"<html><body><h1>Welcome heading title</h1>"
            b"<p>First paragraph with enough text.</p>"
            b"<ul><li>List item number one here</li>"
            b"<li>List item number two here</li></ul></body></html>")
    chunks = _chunk_html(html)
    assert len(chunks) >= 3, chunks
    assert any("First paragraph" in c for c in chunks)


def test_chunk_diff_added_removed():
    old = ["apple pie recipe details", "banana bread instructions"]
    new = ["apple pie recipe details", "chocolate cake steps here"]
    d = _chunk_diff(old, new)
    assert d["unchanged"] == 1
    assert any("chocolate" in c for c in d["added"])
    assert any("banana" in c for c in d["removed"])
    assert d["changed"] == []


def test_chunk_diff_detects_changed():
    old = ["discount rate is 50 percent for everyone"]
    new = ["discount rate is 40 percent for everyone"]
    d = _chunk_diff(old, new)
    assert len(d["changed"]) == 1, d
    assert d["added"] == [] and d["removed"] == []


def test_chunk_diff_unchanged():
    chunks = ["same one here yes", "same two here yes"]
    d = _chunk_diff(chunks, list(chunks))
    assert d["added"] == [] and d["removed"] == [] and d["changed"] == []
    assert d["unchanged"] == 2


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("PASS " + fn.__name__)
    print("\n%d passed" % len(fns))
