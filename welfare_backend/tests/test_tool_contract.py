# -*- coding: utf-8 -*-
"""도구 선언 ↔ 핸들러 인자 계약, 도구 실행 공통부(run_tool) 회귀 테스트.

    python3 tests/test_tool_contract.py        (또는 python3 -m pytest tests/test_tool_contract.py)

배경: 모델은 선언에 적힌 인자를 그대로 보낸다. 선언에는 있는데 핸들러가 받지 않는 인자가
하나라도 있으면 그 도구는 TypeError 로 실패한다(get_bus_arrivals 의 route_name 이 그랬다).

지키는 계약:
  1) Live 선언·온프레미스 폴백 선언의 모든 속성 이름을 디스패처의 핸들러가 받는다
  2) 선언의 required 는 선언된 속성 안에 있고, explain_route_segment 는 route_id 를
     필수로 두지 않는다(세션이 채운다) — Live·폴백 동일
  3) 세션이 주입하는 인자(좌표·route_id·station_wait·handoff 등)는 걸러지지 않는다
  4) 핸들러가 모르는 인자는 걸러지고(TypeError 없음) 이름만 경고 로그에 남는다
  5) 실행 상한을 넘기면 예외 대신 status="error" 값을 돌려준다. 예외도 값으로 돌려준다
  6) get_bus_arrivals 는 route_name 으로 결과를 추리고, 없으면 전체 + 그 사실을 표시한다
  7) explain_route_segment 는 route_id 가 없으면 '안내한 경로 없음' 상태를 돌려준다
  8) 결과 로그 요약에는 상태 키만 있고 본문 값은 없다
"""
import asyncio
import logging
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ROUTE_API_BASE_URL", "http://route-api:18100")
os.environ.setdefault("FEATURE_ROUTE", "1")
os.environ.setdefault("FEATURE_TOUR", "1")

# Live 선언 대조에는 진짜 DB 계층·SDK 가 필요하다. 쓸 수 있으면 먼저 올려 두고(그러면 아래
# 스텁이 끼어들지 않는다), 없으면 스텁으로 폴백 선언·실행부만 검증하고 Live 대조는 건너뛴다.
for _real in ("sqlalchemy", "sqlalchemy.ext.asyncio", "database", "models"):
    try:
        __import__(_real)
    except Exception:                          # noqa: BLE001
        break

for name, attrs in (
    ("sqlalchemy", {"select": lambda *a, **k: None, "or_": lambda *a, **k: None,
                    "text": lambda *a, **k: None, "func": types.SimpleNamespace()}),
    ("sqlalchemy.ext", {}),
    ("sqlalchemy.ext.asyncio", {"AsyncSession": object}),
    ("database", {"AsyncSessionLocal": None, "get_db": None, "engine": None}),
    ("models", {"WelfarePolicy": object, "PolicyChunk": object}),
):
    if name not in sys.modules:
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m

import route_client                                        # noqa: E402
import tool_handlers                                       # noqa: E402
import local_pipeline as lp                                # noqa: E402
from nav_context import inject_nav_defaults, inject_handoff  # noqa: E402

FAILS = []
SKIPS = []

# 세션 상태만 읽어 디스패처를 거치지 않는 도구
_SESSION_ONLY = {"get_current_guidance"}


class _Skip(Exception):
    pass


def check(name, fn):
    try:
        fn()
        print("  PASS  %s" % name)
    except _Skip as e:
        SKIPS.append(name)
        print("  SKIP  %s — %s" % (name, e))
    except AssertionError as e:
        FAILS.append(name)
        print("  FAIL  %s — %s" % (name, e))
    except Exception as e:                     # noqa: BLE001
        FAILS.append(name)
        print("  ERROR %s — %r" % (name, e))


def _run(coro):
    return asyncio.run(coro)


def _local_decls():
    """{도구 이름: (속성 이름 집합, required 목록)} — 온프레미스 폴백 선언."""
    out = {}
    for t in lp._ollama_tools():
        f = t["function"]
        p = f.get("parameters") or {}
        out[f["name"]] = (set((p.get("properties") or {}).keys()), list(p.get("required") or []))
    return out


def _live_decls():
    """{도구 이름: (속성 이름 집합, required 목록)} — Live 선언. SDK 가 없으면 건너뛴다."""
    try:
        import live_bridge as lb
    except Exception as e:                     # noqa: BLE001 — SDK·DB 계층이 없는 환경
        if "pytest" in sys.modules and os.environ.get("PYTEST_CURRENT_TEST"):
            import pytest
            pytest.skip("live_bridge 를 import 할 수 없음: %r" % (e,))
        raise _Skip("live_bridge 를 import 할 수 없음: %r" % (e,))
    out = {}
    for tool in lb.build_tool_declarations():
        for fd in (getattr(tool, "function_declarations", None) or []):
            params = getattr(fd, "parameters", None)
            props = (getattr(params, "properties", None) or {}) if params else {}
            req = (getattr(params, "required", None) or []) if params else []
            out[fd.name] = (set(props.keys()), list(req))
    return out


def _assert_handlers_accept(decls, label):
    disp = tool_handlers.get_tool_dispatcher(None)
    assert decls, "%s 선언이 비어 있음" % label
    problems = []
    for fname, (props, required) in sorted(decls.items()):
        if fname in _SESSION_ONLY:
            continue
        assert fname in disp, "%s 선언 %s 의 구현이 디스패처에 없음" % (label, fname)
        accepted = tool_handlers.tool_accepted_params(disp[fname])
        assert accepted is not None, "%s 의 시그니처를 읽지 못함" % fname
        unknown = props - accepted
        if unknown:
            problems.append("%s: 핸들러가 받지 않는 선언 인자 %s" % (fname, sorted(unknown)))
        stray = set(required) - props
        if stray:
            problems.append("%s: required 가 선언 밖 %s" % (fname, sorted(stray)))
    assert not problems, "%s 선언 불일치 — %s" % (label, "; ".join(problems))


# ── 1·2) 선언 ↔ 핸들러 ──────────────────────────────────────────
def t_local_declarations_fit_handler_signatures():
    _assert_handlers_accept(_local_decls(), "폴백")


def t_live_declarations_fit_handler_signatures():
    _assert_handlers_accept(_live_decls(), "Live")


def t_explain_route_segment_route_id_not_required_in_both():
    assert "route_id" not in _local_decls()["explain_route_segment"][1]
    assert "route_id" not in _live_decls()["explain_route_segment"][1], \
        "Live 선언이 route_id 를 필수로 둠 — 모델이 값을 지어낸다"


def t_live_and_local_declare_same_required():
    live, local = _live_decls(), _local_decls()
    diff = {n: (sorted(live[n][1]), sorted(local[n][1]))
            for n in live if n in local and sorted(live[n][1]) != sorted(local[n][1])}
    assert not diff, "Live·폴백 required 불일치: %s" % diff


# ── 3) 세션 주입 인자는 걸러지지 않는다 ─────────────────────────
def t_session_injected_args_survive_filter():
    nav = {"route_id": "r_x", "guiding": True, "step_idx": 2, "total_steps": 9,
           "board_station_id": "208000069", "board_route_id": "208000096", "board_stop_name": "안양역",
           "station_wait": {"kind": "exit", "station": "안양", "choices": []}}
    loc = {"lat": 37.39, "lng": 126.95}
    disp = tool_handlers.get_tool_dispatcher(None)
    for fname, handler in sorted(disp.items()):
        for nav_state, user_loc in ((nav, loc), ({"route_id": "r_x"}, loc), (nav, {})):
            for mode in ("navi", None):
                fargs = {}
                if fname == "plan_accessible_route" and user_loc.get("lat") is not None:
                    fargs.update(origin_lat=user_loc["lat"], origin_lng=user_loc["lng"])
                fargs = inject_nav_defaults(fname, fargs, dict(nav_state), dict(user_loc))
                fargs = inject_handoff(fname, fargs, mode)
                kept, dropped = tool_handlers.filter_tool_args(handler, fargs)
                assert dropped == [], "%s: 세션이 주입한 인자가 걸러짐 %s" % (fname, dropped)
                assert kept == fargs


# ── 4) 모르는 인자 걸러내기 ─────────────────────────────────────
def t_filter_drops_unknown_and_keeps_known():
    async def h(a: str = "", b: int = 0):
        return {"a": a, "b": b}
    kept, dropped = tool_handlers.filter_tool_args(h, {"a": "x", "zz": 1, "b": 2, "yy": None})
    assert kept == {"a": "x", "b": 2} and dropped == ["yy", "zz"]

    async def anything(**kw):
        return kw
    kept, dropped = tool_handlers.filter_tool_args(anything, {"q": 1})
    assert kept == {"q": 1} and dropped == [], "**kwargs 핸들러는 거르지 않는다"
    assert tool_handlers.filter_tool_args(h, None) == ({}, [])


def t_embed_wrapper_keeps_handler_signature_and_drops_embed_fn():
    disp = tool_handlers.get_tool_dispatcher(lambda x: [0.0])
    acc = tool_handlers.tool_accepted_params(disp["search_by_keyword"])
    assert {"query", "top_k", "expand"} <= acc, acc
    assert "embed_fn" not in acc, "서버가 묶는 인자를 모델이 보낼 수 있으면 안 된다"
    kept, dropped = tool_handlers.filter_tool_args(disp["search_by_keyword"],
                                                   {"query": "q", "embed_fn": 1})
    assert kept == {"query": "q"} and dropped == ["embed_fn"]


class _Capture(logging.Handler):
    def __init__(self):
        logging.Handler.__init__(self)
        self.lines = []

    def emit(self, record):
        self.lines.append((record.levelno, record.getMessage()))


def t_run_tool_unknown_arg_no_typeerror_and_logs_names_only():
    async def h(place: str = ""):
        return {"status": "success", "place": place}
    cap = _Capture()
    tool_handlers.logger.addHandler(cap)
    try:
        r = _run(tool_handlers.run_tool({"find_x": h}, "find_x", {"place": "안양역", "memo": "비밀값"}))
    finally:
        tool_handlers.logger.removeHandler(cap)
    assert r == {"status": "success", "place": "안양역"}, r
    warn = [m for lv, m in cap.lines if lv == logging.WARNING]
    assert warn and "memo" in warn[0], warn
    assert not any("비밀값" in m for _, m in cap.lines), "걸러낸 인자의 값이 로그에 남음"


def t_run_tool_real_dispatcher_tolerates_undeclared_args():
    disp = tool_handlers.get_tool_dispatcher(None)
    r = _run(tool_handlers.run_tool(disp, "get_bus_arrivals", {"route_name": "51", "bogus": 1}))
    assert r.get("status") == "need_location", r        # 위치·정류장이 없어 되묻는다 — TypeError 가 아니다
    r = _run(tool_handlers.run_tool(disp, "open_navi_screen", {"anything": "x"}))
    assert r.get("status") == "success", r


def t_run_tool_unknown_tool():
    r = _run(tool_handlers.run_tool({}, "nope", {}))
    assert "unknown tool" in r.get("error", ""), r


# ── 5) 실행 상한·예외는 값으로 ──────────────────────────────────
def t_run_tool_timeout_returns_error_value():
    async def slow():
        await asyncio.sleep(5)
        return {"status": "success"}
    saved = (tool_handlers.TOOL_TIMEOUT_ROUTE_SEC, tool_handlers.TOOL_TIMEOUT_POLICY_SEC)
    tool_handlers.TOOL_TIMEOUT_ROUTE_SEC = 0.05
    tool_handlers.TOOL_TIMEOUT_POLICY_SEC = 0.05
    try:
        t0 = asyncio.new_event_loop().time()
        r_route = _run(tool_handlers.run_tool({"plan_accessible_route": slow}, "plan_accessible_route", {}))
        r_policy = _run(tool_handlers.run_tool({"search_by_keyword": slow}, "search_by_keyword", {}))
    finally:
        tool_handlers.TOOL_TIMEOUT_ROUTE_SEC, tool_handlers.TOOL_TIMEOUT_POLICY_SEC = saved
    for r in (r_route, r_policy):
        assert r["status"] == "error" and r["error"] == "timeout", r
        assert "잠시 후" in r["ai_instruction"]


def t_timeouts_cover_route_client_retry_budget():
    # 경로 클라이언트: 호출 1건 = 타임아웃 × 2회 시도, 서킷은 연속 3건 실패 뒤 열린다
    per_call = route_client.TIMEOUT_SEC * 2
    assert tool_handlers.TOOL_TIMEOUT_ROUTE_SEC > per_call * route_client._FAIL_THRESHOLD, \
        "경로 도구 상한이 경로 클라이언트의 재시도·서킷 예산보다 짧다"
    assert tool_handlers.tool_timeout_sec("search_by_keyword") == tool_handlers.TOOL_TIMEOUT_POLICY_SEC
    assert tool_handlers.tool_timeout_sec("find_toilet") == tool_handlers.TOOL_TIMEOUT_ROUTE_SEC
    assert tool_handlers.TOOL_TIMEOUT_POLICY_SEC < tool_handlers.TOOL_TIMEOUT_ROUTE_SEC


def t_run_tool_exception_returns_error_value():
    async def boom():
        raise ValueError("깨짐")
    saved = tool_handlers.logger.disabled
    tool_handlers.logger.disabled = True          # 예상된 예외의 트레이스로 출력이 어지럽지 않게
    try:
        r = _run(tool_handlers.run_tool({"x": boom}, "x", {}))
    finally:
        tool_handlers.logger.disabled = saved
    assert r == {"error": "깨짐"}, r


def t_local_fallback_dispatch_filters_and_times_out():
    """온프레미스 폴백의 도구 루프도 같은 실행부를 쓴다 — 모르는 인자·시간 초과가 턴을 깨지 않는다."""
    import httpx
    seen = {}

    async def toilet(place: str = "", lat=None, lng=None):
        seen["kwargs"] = {"place": place, "lat": lat, "lng": lng}
        return {"status": "success", "count": 0}

    async def slow():
        await asyncio.sleep(5)

    replies = [
        {"message": {"content": "", "tool_calls": [
            {"function": {"name": "find_toilet", "arguments": {"place": "안양역", "bogus": 1}}},
            {"function": {"name": "open_navi_screen", "arguments": {}}}]}},
        {"message": {"content": "가까운 화장실을 찾지 못했어요."}},
    ]

    class _Resp:
        def __init__(self, body):
            self._b = body

        def raise_for_status(self):
            pass

        def json(self):
            return self._b

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None):
            return _Resp(replies.pop(0))

    saved_client, saved_to = httpx.AsyncClient, tool_handlers.TOOL_TIMEOUT_ROUTE_SEC
    httpx.AsyncClient = _Client
    tool_handlers.TOOL_TIMEOUT_ROUTE_SEC = 0.05
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "화장실"}]
    try:
        answer = _run(lp._run_llm_turn(messages, {"find_toilet": toilet, "open_navi_screen": slow},
                                       None, None, {}, {}, None, "navi"))
    finally:
        httpx.AsyncClient, tool_handlers.TOOL_TIMEOUT_ROUTE_SEC = saved_client, saved_to
    assert answer == "가까운 화장실을 찾지 못했어요."
    assert seen["kwargs"]["place"] == "안양역", seen
    tool_msgs = [m["content"] for m in messages if m["role"] == "tool"]
    assert len(tool_msgs) == 2 and '"timeout"' in tool_msgs[1], tool_msgs


# ── 6) get_bus_arrivals route_name ──────────────────────────────
_ARR = {"status": "success", "station_id": "208000069", "items": [
    {"route_id": "1", "route_name": "5", "route_type": "마을버스", "end_station": "A",
     "vehicles": [{"predict_min": 2, "stops_away": 1, "low_floor": True}]},
    {"route_id": "2", "route_name": "51", "route_type": "일반형시내버스", "end_station": "충훈부",
     "vehicles": [{"predict_min": 9, "stops_away": 6, "low_floor": False},
                  {"predict_min": 17, "stops_away": 14, "low_floor": True}]},
    {"route_id": "3", "route_name": "5-1", "route_type": "마을버스", "end_station": "B",
     "vehicles": [{"predict_min": None, "stops_away": 3, "low_floor": True}]},
], "next_low_floor": {"route_name": "5", "route_type": "마을버스", "end_station": "A",
                       "predict_min": 2, "stops_away": 1, "plate_no": "x"}}


def _arrivals(**kw):
    async def fake(station_id, route_id=""):
        return _ARR
    orig = route_client.bus_arrivals
    route_client.bus_arrivals = fake
    try:
        return _run(tool_handlers.tool_get_bus_arrivals(station_id="208000069", **kw))
    finally:
        route_client.bus_arrivals = orig


def t_bus_arrivals_filters_by_route_name():
    for said in ("51", "51번", " 51번 버스 "):
        r = _arrivals(route_name=said)
        assert r["status"] == "success" and r["route_name_matched"] is True, r
        assert [i["route_name"] for i in r["items"]] == ["51"], "번호는 완전 일치로만 추린다: %s" % r["items"]
        assert r["count"] == 1
        # 저상 차량은 그 노선 안에서 다시 고른다 — 정류장 전체 기준(5번)이 아니라 51번의 17분 뒤 차량
        assert r["next_low_floor"]["route_name"] == "51" and r["next_low_floor"]["predict_min"] == 17, r
        assert "51번만 추린" in r["ai_instruction"]


def t_bus_arrivals_route_name_not_found_returns_all_and_says_so():
    r = _arrivals(route_name="900")
    assert r["status"] == "success" and r["route_name_matched"] is False, r
    assert r["count"] == 3 and r["next_low_floor"]["route_name"] == "5"
    assert "900번은" in r["ai_instruction"] and "도착정보에 없습니다" in r["ai_instruction"]


def t_bus_arrivals_without_route_name_unchanged():
    r = _arrivals()
    assert r["route_name_matched"] is None and r["route_name"] is None
    assert r["count"] == 3 and r["next_low_floor"]["route_name"] == "5"
    assert "추린" not in r["ai_instruction"]


# ── 7) explain_route_segment 기본값 ─────────────────────────────
def t_explain_without_route_id_reports_no_route():
    called = []

    async def fake(route_id):
        called.append(route_id)
        return {"status": "error", "message": "x"}
    orig = route_client.get_route
    route_client.get_route = fake
    try:
        r = _run(tool_handlers.tool_explain_route_segment())
        # 세션에도 경로가 없으면 주입이 일어나지 않는다 → 빈 값 그대로 핸들러에 온다
        fargs = inject_nav_defaults("explain_route_segment", {}, {}, {})
        r2 = _run(tool_handlers.run_tool(tool_handlers.get_tool_dispatcher(None),
                                         "explain_route_segment", fargs))
    finally:
        route_client.get_route = orig
    for x in (r, r2):
        assert x["status"] == "no_route" and x["tool_name"] == "explain_route_segment", x
        assert "안내한 경로가 없" in x["ai_instruction"]
    assert called == [], "경로가 없는데 경로 API 를 호출함"


def t_explain_uses_session_route_when_model_omits_it():
    called = []

    async def fake(route_id):
        called.append(route_id)
        return {"route_id": route_id, "routes": [{"steps": [
            {"idx": 0, "instruction": "직진", "warnings": []},
            {"idx": 1, "instruction": "경사 구간", "warnings": ["경사"]}]}]}
    orig = route_client.get_route
    route_client.get_route = fake
    try:
        fargs = inject_nav_defaults("explain_route_segment", {},
                                    {"route_id": "r_abc", "guiding": True, "step_idx": 1}, {})
        r = _run(tool_handlers.run_tool(tool_handlers.get_tool_dispatcher(None),
                                        "explain_route_segment", fargs))
    finally:
        route_client.get_route = orig
    assert called == ["r_abc"] and r["status"] == "success", r
    assert [s["idx"] for s in r["segments"]] == [1]


# ── 8) 결과 로그 요약 ───────────────────────────────────────────
def t_result_summary_has_status_keys_not_values():
    s = tool_handlers.summarize_tool_result(
        {"status": "success", "count": 2, "base_label": "우리집앞", "items": [{"name": "비밀식당"}]})
    assert "status=success" in s and "count=2" in s and "items" in s
    assert "우리집앞" not in s and "비밀식당" not in s, s
    assert "error=yes" in tool_handlers.summarize_tool_result({"error": "깨짐 상세"})
    assert "깨짐 상세" not in tool_handlers.summarize_tool_result({"error": "깨짐 상세"})
    assert tool_handlers.summarize_tool_result(None) == "type=NoneType"


if __name__ == "__main__":
    for nm, fn in sorted((k, v) for k, v in list(globals().items())
                         if k.startswith("t_") and callable(v)):
        check(nm, fn)
    print()
    if FAILS:
        print("FAILED: %d" % len(FAILS))
        sys.exit(1)
    print("all passed%s" % (" (%d skipped)" % len(SKIPS) if SKIPS else ""))
