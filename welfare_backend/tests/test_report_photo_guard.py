# -*- coding: utf-8 -*-
"""접근성 제보 사진 검증 · 콘솔 사진 응답 형식 · 트랙 meta 정리 · 경로 서비스 토큰 헤더 (v2.24.0).

    python3 -m pytest -q tests/test_report_photo_guard.py

지키는 계약:
  1) 제보 사진은 JPEG·PNG·WebP 이고, 디코드 후 2MB 이하이며, 선언 형식과 파일 시그니처가 같을 때만
     경로 서비스로 넘긴다. 아니면 400(status=error, message) 이고 경로 서비스를 부르지 않는다.
  2) 넘길 때 형식 값은 시그니처로 판정한 값, base64 는 접두·공백을 뗀 정규형이다.
  3) 트랙 meta 는 경로 서비스 계약 항목만 넘긴다.
  4) 콘솔 사진 응답의 Content-Type 은 바이트 시그니처로 정한다. 이미지가 아니면
     application/octet-stream + attachment. 언제나 nosniff · CSP(sandbox) 를 붙인다.
  5) 경로 서비스 호출에는 ROUTE_API_TOKEN 이 Authorization: Bearer 로 실린다.
"""
import asyncio
import base64
import os
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ROUTE_API_BASE_URL", "http://route-api:18100")
os.environ.setdefault("FEATURE_ROUTE", "1")

import photo_guard  # noqa: E402


# ── 시험용 바이트 ──
def _png_bytes(w=2, h=2):
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


PNG = _png_bytes()
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + b"\x00" * 64 + b"\xff\xd9"
WEBP = b"RIFF" + struct.pack("<I", 30) + b"WEBPVP8 " + b"\x00" * 22
HTML = b"<!doctype html><html><body><p>not an image</p></body></html>"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"></svg>'
GIF = b"GIF89a\x01\x00\x01\x00\x00\x00\x00;"
WAV = b"RIFF" + struct.pack("<I", 30) + b"WAVEfmt " + b"\x00" * 22


def b64(data):
    return base64.b64encode(data).decode("ascii")


def _rejects(photo, mime):
    try:
        photo_guard.validate_report_photo(photo, mime)
    except photo_guard.PhotoRejected:
        return True
    return False


# ── 1) 검증 함수 단위 ──
def test_sniff():
    assert photo_guard.sniff_image_mime(PNG) == "image/png"
    assert photo_guard.sniff_image_mime(JPEG) == "image/jpeg"
    assert photo_guard.sniff_image_mime(WEBP) == "image/webp"
    for bad in (HTML, SVG, GIF, WAV, b"", b"\xff\xd8"):
        assert photo_guard.sniff_image_mime(bad) is None


def test_valid_photos_pass():
    assert photo_guard.validate_report_photo(b64(PNG), "image/png") == (b64(PNG), "image/png")
    assert photo_guard.validate_report_photo(b64(JPEG), "image/jpeg") == (b64(JPEG), "image/jpeg")
    assert photo_guard.validate_report_photo(b64(WEBP), "image/webp") == (b64(WEBP), "image/webp")
    # 형식 값이 없으면 시그니처로 정한다 · 별칭·대소문자·매개변수는 정리한다
    assert photo_guard.validate_report_photo(b64(JPEG), None) == (b64(JPEG), "image/jpeg")
    assert photo_guard.validate_report_photo(b64(JPEG), "IMAGE/JPG") == (b64(JPEG), "image/jpeg")
    assert photo_guard.validate_report_photo(b64(PNG), "image/png; q=1") == (b64(PNG), "image/png")
    # data URL 접두와 줄바꿈은 떼고 정규형으로 넘긴다
    wrapped = "data:image/png;base64," + "\n".join(b64(PNG)[i:i + 20] for i in range(0, len(b64(PNG)), 20))
    assert photo_guard.validate_report_photo(wrapped, None) == (b64(PNG), "image/png")


def test_no_photo():
    assert photo_guard.validate_report_photo(None, "image/png") == (None, None)
    assert photo_guard.validate_report_photo("", "text/html") == (None, None)


def test_forged_or_disallowed_rejected():
    assert _rejects(b64(HTML), "image/png")          # 내용은 HTML, 형식은 이미지라고 적음
    assert _rejects(b64(PNG), "text/html")           # 내용은 PNG, 형식은 HTML 이라고 적음
    assert _rejects(b64(JPEG), "image/png")          # 허용 형식끼리라도 선언과 내용이 다름
    assert _rejects(b64(SVG), "image/svg+xml")
    assert _rejects(b64(GIF), "image/gif")
    assert _rejects(b64(WAV), None)
    assert _rejects(b64(HTML), None)
    assert _rejects("data:text/html;base64," + b64(PNG), None)        # 접두 형식이 허용 밖
    assert _rejects("data:image/jpeg;base64," + b64(PNG), "image/png")  # 접두와 선언이 다름
    assert _rejects("####", "image/png")             # base64 알파벳 밖
    assert _rejects(12345, "image/png")              # 문자열 아님
    assert _rejects({"a": 1}, "image/png")


def test_size_limits():
    big = JPEG[:4] + b"\x00" * (photo_guard.MAX_PHOTO_BYTES + 1)
    assert _rejects(b64(big), "image/jpeg")
    assert _rejects("A" * (photo_guard.MAX_PHOTO_B64_CHARS + 1), "image/jpeg")
    ok = JPEG[:4] + b"\x00" * (photo_guard.MAX_PHOTO_BYTES - 4)
    assert photo_guard.validate_report_photo(b64(ok), "image/jpeg")[1] == "image/jpeg"


def test_response_parts():
    mt, h = photo_guard.photo_response_parts(PNG, 7)
    assert mt == "image/png" and h["Content-Disposition"].startswith("inline")
    mt, h = photo_guard.photo_response_parts(HTML, 7)
    assert mt == "application/octet-stream" and h["Content-Disposition"].startswith("attachment")
    for data in (PNG, HTML):
        _, h = photo_guard.photo_response_parts(data, 7)
        assert h["X-Content-Type-Options"] == "nosniff"
        assert h["Content-Security-Policy"] == "default-src 'none'; img-src 'self' data:; sandbox"


# ── 2) 엔드포인트 (FastAPI 앱 전체) ──
def _client():
    import pytest
    pytest.importorskip("fastapi")
    pytest.importorskip("google.genai")
    from fastapi.testclient import TestClient
    import main
    return TestClient(main.app), main


class _Spy:
    def __init__(self, ret=None):
        self.calls, self.ret = [], ret

    async def __call__(self, *a):
        self.calls.append(a)
        return self.ret


def _with(attr, fake, fn):
    import route_client
    saved = getattr(route_client, attr)
    setattr(route_client, attr, fake)
    try:
        return fn()
    finally:
        setattr(route_client, attr, saved)


REPORT = {"lat": 37.39, "lng": 126.95, "reason": "curb", "detail": None, "route_id": None}


def test_report_endpoint_rejects_forged_photo():
    cli, _ = _client()
    spy = _Spy({"report_id": 1})
    cases = [(b64(HTML), "image/png"), (b64(PNG), "text/html"), (b64(SVG), "image/svg+xml"),
             (b64(JPEG), "image/png"), ("not-base64!!", "image/jpeg")]
    for photo, mime in cases:
        r = _with("report_accessibility", spy,
                  lambda: cli.post("/api/v1/nav/report", json=dict(REPORT, photo_base64=photo, photo_mime=mime)))
        assert r.status_code == 400, (mime, r.status_code)
        d = r.json()
        assert d["status"] == "error" and d["error"] == "invalid_photo" and d["message"]
    assert spy.calls == []                            # 경로 서비스로 하나도 넘어가지 않았다


def test_report_endpoint_passes_valid_photo():
    cli, _ = _client()
    spy = _Spy({"report_id": 1, "message": "접수"})
    r = _with("report_accessibility", spy,
              lambda: cli.post("/api/v1/nav/report", json=dict(REPORT, photo_base64=b64(PNG), photo_mime="image/png")))
    assert r.status_code == 200 and r.json()["report_id"] == 1
    sent = spy.calls[-1][0]
    assert sent["photo_base64"] == b64(PNG) and sent["photo_mime"] == "image/png"
    # 안내 화면이 실제로 보내는 꼴: JPEG + image/jpeg
    r = _with("report_accessibility", spy,
              lambda: cli.post("/api/v1/nav/report", json=dict(REPORT, photo_base64=b64(JPEG), photo_mime="image/jpeg")))
    assert r.status_code == 200 and spy.calls[-1][0]["photo_mime"] == "image/jpeg"
    # 사진 없는 제보 — 형식 값만 와도 버린다
    r = _with("report_accessibility", spy,
              lambda: cli.post("/api/v1/nav/report", json=dict(REPORT, photo_mime="text/html")))
    assert r.status_code == 200
    assert spy.calls[-1][0]["photo_base64"] is None and spy.calls[-1][0]["photo_mime"] is None
    assert len(spy.calls) == 3


def test_track_meta_allowlist():
    cli, _ = _client()
    spy = _Spy({"saved": 1})
    meta = {"profile": "wheelchair_manual", "planned_dist_m": 523.6, "geometry": [[37.39, 126.95], [37.4, 126.96], "x"],
            "outcome": "arrived", "network_version": "nv1", "started_at": "2026-11-02T09:00:00Z",
            "finished_at": "2026-11-02T09:20:00Z", "user_name": "홍길동", "extra": {"a": 1}}
    r = _with("log_track", spy, lambda: cli.post("/api/v1/nav/track", json={
        "route_id": "r1", "points": [{"seq": 0, "lat": 37.39, "lng": 126.95}], "meta": meta}))
    assert r.status_code == 200
    m = spy.calls[-1][0]["meta"]
    assert set(m) == {"profile", "planned_dist_m", "geometry", "outcome", "network_version", "started_at", "finished_at"}
    assert m["planned_dist_m"] == 524 and m["geometry"] == [[37.39, 126.95], [37.4, 126.96]]
    r = _with("log_track", spy, lambda: cli.post("/api/v1/nav/track", json={"route_id": "r1", "points": [], "meta": "x"}))
    assert r.status_code == 200 and spy.calls[-1][0]["meta"] is None


def test_admin_photo_response_headers():
    cli, _ = _client()

    def fake(data, mime):
        async def _f(report_id):
            return data, mime
        return _f

    r = _with("get_report_photo", fake(PNG, "text/html"), lambda: cli.get("/admin/api/nav-reports/5/photo"))
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content == PNG
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in r.headers["content-security-policy"]
    assert r.headers["content-disposition"].startswith("inline")

    r = _with("get_report_photo", fake(HTML, "image/png"), lambda: cli.get("/admin/api/nav-reports/5/photo"))
    assert r.status_code == 200 and r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"].startswith("attachment")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-security-policy"] == "default-src 'none'; img-src 'self' data:; sandbox"

    r = _with("get_report_photo", fake(None, None), lambda: cli.get("/admin/api/nav-reports/5/photo"))
    assert r.status_code == 404


# ── 3) 경로 서비스 토큰 헤더 ──
def test_route_token_header():
    import route_client as rc
    seen = []

    class _Resp:
        def __init__(self, ctype="application/json"):
            self.status_code, self.headers, self.content = 200, {"content-type": ctype}, PNG

        def json(self):
            return {"ok": True}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, params=None, json=None, headers=None):
            seen.append(headers); return _Resp()

        async def get(self, url, headers=None):
            seen.append(headers); return _Resp("image/png")

    saved = (rc.httpx.AsyncClient, rc.API_TOKEN, rc.BASE_URL, getattr(rc, "_call_log_disabled", None))
    rc.httpx.AsyncClient, rc.API_TOKEN, rc.BASE_URL = _Client, "t-123", "http://route-api:18100"
    rc._call_log_disabled = True
    try:
        asyncio.run(rc.report_accessibility({"lat": 1, "lng": 1, "reason": "etc"}))
        asyncio.run(rc.log_track({"route_id": "r", "points": []}))
        asyncio.run(rc.list_access_reports())
        asyncio.run(rc.get_report_photo(3))
    finally:
        rc.httpx.AsyncClient, rc.API_TOKEN, rc.BASE_URL, rc._call_log_disabled = saved
    assert len(seen) == 4
    assert all(h.get("Authorization") == "Bearer t-123" for h in seen), seen


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
