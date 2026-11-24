# -*- coding: utf-8 -*-
"""웹에서 계정 바꾸기 · 지금 계정 조회 (v2.35.0).

    python3 -m pytest -q tests/test_account_switch.py

지키는 계약:
  1) /api/v1/whoami 는 nginx 가 넣은 계정명(X-Remote-User)을 돌려준다. 없으면 null.
  2) /api/v1/account/switch 는 표시 쿠키(p.<계정>)를 둔다. 계정이 없으면 두지 않는다.
  3) 표시가 있고 화면 요청이 같은 계정으로 오면 한 번만 401 + 사이트 realm 으로 로그인 창을 띄운다(a.<계정>).
  4) 다른 계정으로 오거나 이미 한 번 띄웠으면 화면을 그대로 주고 표시를 지운다.
  5) 화면이 아닌 요청(API 등)은 표시와 상관없이 그대로 지나간다.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ROUTE_API_BASE_URL", "http://route-api:18100")
os.environ.setdefault("FEATURE_ROUTE", "1")


def _client():
    import pytest
    pytest.importorskip("fastapi")
    pytest.importorskip("google.genai")
    from fastapi.testclient import TestClient
    import main
    return TestClient(main.app), main


def test_whoami():
    c, _ = _client()
    assert c.get("/api/v1/whoami", headers={"X-Remote-User": "P2"}).json() == {"user": "P2"}
    assert c.get("/api/v1/whoami").json() == {"user": None}


def test_switch_sets_mark_only_with_user():
    c, m = _client()
    r = c.post("/api/v1/account/switch", headers={"X-Remote-User": "tester"})
    assert r.json() == {"ok": True}
    sc = r.headers.get("set-cookie", "")
    assert m._ACCT_COOKIE + "=p.tester" in sc and "HttpOnly" in sc and "Max-Age=300" in sc
    r = c.post("/api/v1/account/switch")
    assert r.json() == {"ok": False} and "set-cookie" not in r.headers


def test_gate_prompts_once_for_same_user():
    c, m = _client()
    for path in ("/policy", "/static/accessistant.html"):
        r = c.get(path, headers={"X-Remote-User": "tester", "Cookie": m._ACCT_COOKIE + "=p.tester"})
        assert r.status_code == 401, path
        assert r.headers["www-authenticate"].startswith('Basic realm="' + m._SITE_AUTH_REALM + '"')
        assert m._ACCT_COOKIE + "=a.tester" in r.headers.get("set-cookie", "")
        assert "다른 계정으로 로그인" in r.text


def test_gate_passes_other_user_and_second_try():
    c, m = _client()
    for user, mark in (("P2", "p.tester"), ("tester", "a.tester")):
        r = c.get("/policy", headers={"X-Remote-User": user, "Cookie": m._ACCT_COOKIE + "=" + mark})
        assert r.status_code == 200 and "<html" in r.text.lower()
        sc = r.headers.get("set-cookie", "")
        assert m._ACCT_COOKIE + '=""' in sc or (m._ACCT_COOKIE + "=" in sc and "Max-Age=0" in sc)


def test_gate_ignores_api_and_no_mark():
    c, m = _client()
    r = c.get("/api/v1/whoami", headers={"X-Remote-User": "tester", "Cookie": m._ACCT_COOKIE + "=p.tester"})
    assert r.status_code == 200
    r = c.get("/policy", headers={"X-Remote-User": "tester"})
    assert r.status_code == 200 and "set-cookie" not in r.headers


def test_front_page_theme_white_and_button():
    html = (ROOT / "static" / "accessistant.html").read_text(encoding="utf-8")
    assert '<meta name="theme-color" content="#ffffff">' in html
    assert 'id="acctBtn"' in html and "/api/v1/account/switch" in html
    man = (ROOT / "static" / "manifest.webmanifest").read_text(encoding="utf-8")
    assert '"theme_color": "#ffffff"' in man
