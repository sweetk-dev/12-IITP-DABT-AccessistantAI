# -*- coding: utf-8 -*-
"""온프레미스 폴백의 도구 선언 회귀 테스트 (의존성 없이 단독 실행).

    python3 tests/test_local_fallback_tools.py

배경: Live 연결이 불가능할 때 쓰는 로컬 파이프라인에 정책 도구 5종만 선언돼 있어
경로·관광 도구가 통째로 빠져 있었다(누락). 폴백에서도 길안내가 되어야 한다.

지키는 계약:
  1) 폴백 선언 = Live 선언과 같은 도구 집합 (정책 5 + 경로 7)
  2) 경로 서비스가 꺼져 있으면 Live 와 동일하게 경로 도구를 선언하지 않는다
  3) 좌표·route_id 는 선언에 넣지 않는다 — 세션이 주입할 값이므로 모델이 만들면 안 된다
  4) get_current_guidance 를 뺀 나머지는 전부 디스패처에 구현이 있어야 한다
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ROUTE_API_BASE_URL", "http://route-api:18100")
os.environ.setdefault("FEATURE_ROUTE", "1")
os.environ.setdefault("FEATURE_TOUR", "1")

import types as _t
for name, attrs in (
    ("sqlalchemy", {"select": lambda *a, **k: None, "or_": lambda *a, **k: None,
                    "text": lambda *a, **k: None, "func": _t.SimpleNamespace()}),
    ("sqlalchemy.ext", {}),
    ("sqlalchemy.ext.asyncio", {"AsyncSession": object}),
    ("database", {"AsyncSessionLocal": None, "get_db": None, "engine": None}),
    ("models", {"WelfarePolicy": object, "PolicyChunk": object}),
):
    if name not in sys.modules:
        m = _t.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m

import local_pipeline as lp
from tool_handlers import get_tool_dispatcher

POLICY = {"search_policies_by_metadata", "search_by_keyword", "get_policy_details",
          "check_eligibility_criteria", "find_operating_agencies"}
ROUTE = {"find_bf_tour_spots", "plan_accessible_route", "explain_route_segment",
         "get_current_guidance", "find_nearby_transit", "open_navi_screen",
         "report_accessibility_issue"}

results = []


def check(name, fn):
    try:
        fn()
        results.append(("PASS", name))
    except Exception as e:
        results.append(("FAIL", "%s — %s" % (name, e)))


def names(tools):
    return {t["function"]["name"] for t in tools}


def props(tools, fname):
    for t in tools:
        if t["function"]["name"] == fname:
            return set((t["function"]["parameters"].get("properties") or {}).keys())
    raise AssertionError("선언에 %s 가 없음" % fname)


def t_has_route_tools():
    got = names(lp._ollama_tools())
    missing = (POLICY | ROUTE) - got
    assert not missing, "폴백에서 빠진 도구: %s" % sorted(missing)


def t_no_extra():
    got = names(lp._ollama_tools())
    extra = got - (POLICY | ROUTE)
    assert not extra, "폴백에만 있는 도구: %s" % sorted(extra)


def t_route_off_hides_route_tools():
    import route_client
    saved = (route_client.FEATURE_ROUTE, route_client.FEATURE_TOUR)
    route_client.FEATURE_ROUTE = False
    route_client.FEATURE_TOUR = False
    try:
        got = names(lp._ollama_tools())
        assert got == POLICY, "경로 기능이 꺼졌는데 선언에 남은 도구: %s" % sorted(got - POLICY)
    finally:
        route_client.FEATURE_ROUTE, route_client.FEATURE_TOUR = saved


def t_no_coordinate_params():
    tools = lp._ollama_tools()
    for fname in ("plan_accessible_route", "find_nearby_transit", "report_accessibility_issue"):
        p = props(tools, fname)
        leaked = p & {"lat", "lng", "origin_lat", "origin_lng", "route_id"}
        assert not leaked, "%s 선언에 세션이 주입할 값이 노출됨: %s" % (fname, sorted(leaked))


def t_explain_segment_ids_optional():
    tools = lp._ollama_tools()
    for t in tools:
        if t["function"]["name"] == "explain_route_segment":
            req = t["function"]["parameters"].get("required") or []
            assert "route_id" not in req, "route_id 를 필수로 두면 모델이 지어낸다"
            return
    raise AssertionError("explain_route_segment 선언 없음")


def t_dispatcher_backs_every_tool():
    disp = set(get_tool_dispatcher(None).keys())
    declared = names(lp._ollama_tools())
    # get_current_guidance 는 세션 상태만 읽어 디스패처를 거치지 않는다
    unbacked = declared - disp - {"get_current_guidance"}
    assert not unbacked, "구현 없는 도구를 선언함: %s" % sorted(unbacked)


for fn in (t_has_route_tools, t_no_extra, t_route_off_hides_route_tools,
           t_no_coordinate_params, t_explain_segment_ids_optional,
           t_dispatcher_backs_every_tool):
    check(fn.__name__, fn)

failed = 0
for st, name in results:
    print("  %s  %s" % (st, name))
    if st == "FAIL":
        failed += 1
print("\n%s" % ("ALL PASSED" if not failed else "%d FAILED" % failed))
sys.exit(1 if failed else 0)
