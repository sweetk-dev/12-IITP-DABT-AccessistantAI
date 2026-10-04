# -*- coding: utf-8 -*-
"""경로 클라이언트(route_client)의 "실패는 값으로" 계약 + URL 경로 식별자 검증.

    python3 tests/test_route_client_contract.py

지키는 계약:
  1) 2xx 인데 본문이 JSON 이 아니면 예외가 아니라 {"status":"error"} 값을 돌려준다
  2) 4xx 본문이 JSON 객체가 아니어도(배열·문자열·JSON 아님) 값으로 돌려준다
  3) route_id·poi_id 를 URL 경로에 넣기 전에 형식을 검증한다 — 형식이 다르면 호출하지 않는다
"""
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ROUTE_API_BASE_URL", "http://route-api:18100")
os.environ.setdefault("FEATURE_ROUTE", "1")

import route_client                                        # noqa: E402

FAILS = []


def check(name, fn):
    try:
        fn()
        print("  PASS  %s" % name)
    except AssertionError as e:
        FAILS.append(name)
        print("  FAIL  %s — %s" % (name, e))
    except Exception as e:                     # noqa: BLE001
        FAILS.append(name)
        print("  ERROR %s — %r" % (name, e))


class _Resp:
    def __init__(self, status, text):
        self.status_code, self._text, self.headers = status, text, {}

    def json(self):
        return json.loads(self._text)


class _Http:
    """httpx.AsyncClient 대역 — 정해 둔 응답 하나를 돌려주고 요청 URL 을 기록한다."""
    urls = []
    resp = None

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method, url, **kw):
        _Http.urls.append(url)
        return _Http.resp


def _call(status, text, coro_fn):
    saved = (route_client.httpx.AsyncClient, route_client.BASE_URL, route_client._call_log_disabled,
             route_client._fail_count, route_client._open_until)
    route_client.httpx.AsyncClient = _Http
    route_client.BASE_URL = "http://route-api:18100"
    route_client._call_log_disabled = True          # 계측 파일을 만들지 않는다
    route_client._open_until = 0.0
    _Http.urls, _Http.resp = [], _Resp(status, text)
    try:
        return asyncio.run(coro_fn()), list(_Http.urls)
    finally:
        (route_client.httpx.AsyncClient, route_client.BASE_URL, route_client._call_log_disabled,
         route_client._fail_count, route_client._open_until) = saved


def t_2xx_non_json_body_is_error_value():
    for text in ("<html>502 Bad Gateway</html>", ""):
        r, urls = _call(200, text, route_client.profiles)
        assert isinstance(r, dict) and r["status"] == "error", r
        assert r["ai_instruction"] == route_client._AI_TRANSIENT
        assert len(urls) == 1, "서버가 응답했는데 재시도함"


def t_2xx_json_body_passes_through():
    r, _ = _call(200, '{"profiles": ["walk"]}', route_client.profiles)
    assert r == {"profiles": ["walk"]}
    r, _ = _call(200, '[{"poi_id": "1"}]', route_client.profiles)
    assert r == [{"poi_id": "1"}], "JSON 배열 응답은 그대로 돌려준다"


def t_4xx_non_object_body_is_error_value():
    for text in ('["잘못된 요청"]', '"문자열"', "null", "<html>404</html>", ""):
        r, _ = _call(404, text, route_client.profiles)
        assert isinstance(r, dict) and r["status"] == "error", (text, r)
        assert r["detail"] == "HTTP 404" and r["message"]


def t_4xx_object_body_keeps_detail():
    r, _ = _call(400, '{"detail": "경로가 만료되었습니다"}', route_client.profiles)
    assert r["status"] == "error" and r["message"] == "경로가 만료되었습니다"
    assert "만료" in r["ai_instruction"]


def t_valid_path_id():
    ok = ("r_0123abcdef", "TBF-1", "KRNA_1_MHK", "14792", "a", "x" * 64)
    bad = ("", "x" * 65, "../meta/network", "r_x/..", "r_x?a=1", "r x", "r_x\n", "경로", "r_x#f",
           "r%2Fx", None, 123, ["r_x"])
    for v in ok:
        assert route_client.valid_path_id(v), v
    for v in bad:
        assert not route_client.valid_path_id(v), repr(v)


def t_get_route_rejects_bad_id_without_calling():
    for bad in ("../meta/network", "r_x?step=1", "", "r_x\n"):
        r, urls = _call(200, "{}", lambda: route_client.get_route(bad))
        assert r["status"] == "error" and urls == [], (bad, r, urls)
    r, urls = _call(200, '{"route_id": "r_0123abcdef"}', lambda: route_client.get_route("r_0123abcdef"))
    assert r == {"route_id": "r_0123abcdef"}
    assert urls == ["http://route-api:18100/route/r_0123abcdef"]


def t_tour_detail_rejects_bad_id_without_calling():
    r, urls = _call(200, "{}", lambda: route_client.tour_detail("../../admin"))
    assert r["status"] == "error" and urls == []
    r, urls = _call(200, '{"poi_id": "TBF-1"}', lambda: route_client.tour_detail("TBF-1"))
    assert urls == ["http://route-api:18100/tour/bf-spots/TBF-1"]
    r, urls = _call(200, '{"poi_id": "14792"}', lambda: route_client.tour_detail(14792))
    assert urls == ["http://route-api:18100/tour/bf-spots/14792"], "숫자 식별자는 문자열로 바꿔 쓴다"


def t_explain_endpoint_validates_route_id():
    """main.py 의 구간 설명 엔드포인트가 route_id 형식을 확인하는지 — 소스로 확인한다.

    main.py 는 import 시 앱·DB 엔진을 만들므로(다른 테스트와 같은 이유로) 불러오지 않는다.
    """
    import ast
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    fn = [n for n in ast.parse(src).body
          if isinstance(n, ast.AsyncFunctionDef) and n.name == "explain_route_segment"]
    assert len(fn) == 1
    body = ast.get_source_segment(src, fn[0])
    assert "route_client.valid_path_id(route_id)" in body and "HTTPException" in body, body


if __name__ == "__main__":
    for nm, fn in sorted((k, v) for k, v in list(globals().items())
                         if k.startswith("t_") and callable(v)):
        check(nm, fn)
    print()
    if FAILS:
        print("FAILED: %d" % len(FAILS))
        sys.exit(1)
    print("all passed")
