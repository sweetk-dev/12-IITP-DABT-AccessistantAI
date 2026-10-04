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
    from .detectors import ChangeResult, SNAPSHOT_FILES, _read_prev_hash, save_snapshot, _normalize_html_text, _hash_bytes, _chunk_html, _chunk_diff, _js_suspect, JS_SUSPECT_MIN_CHARS
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from crawler.detectors import ChangeResult, SNAPSHOT_FILES, _read_prev_hash, save_snapshot, _normalize_html_text, _hash_bytes, _chunk_html, _chunk_diff, _js_suspect, JS_SUSPECT_MIN_CHARS  # type: ignore


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
            res = ChangeResult(True, "x", None, "HASH_" + method)
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
        save_snapshot(snap, "last_modified_field", ChangeResult(True, "x", None, h))
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


def test_js_suspect_threshold():
    """본문 길이가 임계 미만이면 JS 의심(True), 이상이면 False."""
    assert _js_suspect(10) is True
    assert _js_suspect(JS_SUSPECT_MIN_CHARS - 1) is True
    assert _js_suspect(JS_SUSPECT_MIN_CHARS) is False
    assert _js_suspect(5000) is False


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


# ── last_modified_field: 비교 키를 만들 수 없는 경우 ─────────
class _FakeResp:
    def __init__(self, content: bytes, headers=None, status_code=200):
        self.content = content
        self.headers = headers or {}
        self.status_code = status_code


class _FakeClient:
    """httpx.AsyncClient 대역 — 항상 같은 응답을 돌려준다(네트워크 없음)."""
    def __init__(self, resp):
        self._resp = resp

    async def get(self, url, **kwargs):
        return self._resp


def _run_last_modified(html: str, headers=None, snapshot_dir=None, revalidate=False):
    import asyncio
    try:
        from .detectors import detect_last_modified_field
    except ImportError:
        from crawler.detectors import detect_last_modified_field  # type: ignore
    client = _FakeClient(_FakeResp(html.encode("utf-8"), headers))
    with tempfile.TemporaryDirectory() as d:
        snap = Path(snapshot_dir or d)
        return asyncio.run(detect_last_modified_field(
            {"target_id": "t", "url": "https://example.go.kr/law"}, snap,
            client=client, revalidate=revalidate))


_BODY_NO_DATE = "<html><body><p>" + ("장애인 지원 제도 안내 본문입니다. " * 30) + "</p></body></html>"


def test_last_modified_without_any_key_is_detect_failed():
    """헤더·본문 어디에도 수정일이 없으면 '변경 없음'이 아니라 감지 실패로 돌려준다."""
    res = _run_last_modified(_BODY_NO_DATE)
    assert res.status == "detect_failed"
    assert res.changed is False, "변경으로 치면 LLM 갱신 호출로 이어진다"
    assert res.new_hash is None, "상수 키의 해시가 비교 기준으로 저장되면 안 된다"
    assert res.new_content is None
    assert "last_modified" in res.reason


def test_last_modified_without_key_stays_failed_even_with_old_constant_baseline():
    """상수 키("|")의 해시가 이미 기준으로 저장돼 있어도 '변경 없음'으로 보고하지 않는다."""
    with tempfile.TemporaryDirectory() as d:
        snap = Path(d)
        save_snapshot(snap, "last_modified_field",
                      ChangeResult(True, "x", None, _hash_bytes("|".encode("utf-8"))))
        res = _run_last_modified(_BODY_NO_DATE, snapshot_dir=snap)
        assert res.status == "detect_failed"
        assert res.reason != "변경 없음"


def test_last_modified_detect_failed_passes_body_on_revalidate():
    res = _run_last_modified(_BODY_NO_DATE, revalidate=True)
    assert res.status == "detect_failed"
    assert res.new_content is not None, "재검증 모드에서는 받은 본문을 넘겨야 한다"


def test_last_modified_with_body_date_still_works():
    html = "<html><body><p>시행일 2026-01-01</p><p>" + ("본문 " * 100) + "</p></body></html>"
    res = _run_last_modified(html)
    assert res.status == "ok"
    assert res.changed is True and res.reason == "최초 스냅샷"
    assert res.new_hash


def test_last_modified_with_header_only_still_works():
    res = _run_last_modified(_BODY_NO_DATE, headers={"Last-Modified": "Wed, 01 Jan 2026 00:00:00 GMT"})
    assert res.status == "ok" and res.new_hash
