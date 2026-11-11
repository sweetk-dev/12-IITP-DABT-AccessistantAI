# -*- coding: utf-8 -*-
"""말로 안내 시작 · 장애 유형 값 정규화 · 입력 전사 언어 지정 (v2.26.0). 외부 호출 없음.

지키는 계약:
  1) 장애 유형 값은 빗금·쉼표로 이어져 와도, 다른 표기로 와도 경로 서비스가 아는 이름 목록이 된다
  2) start_guidance 는 경로가 있고 안내 전일 때만 화면에 지시(ui_action)를 보낸다
  3) 경로 성공 결과에는 안내 중이었는지에 따라 다른 후속 문구가 붙는다
  4) 안내 중이 아닐 때의 get_current_guidance 는 말로 시작하는 길을 알려 준다
  5) Live·온프레미스 폴백 두 쪽 도구 선언에 start_guidance 가 있다
"""
import asyncio
import os
import sys
from pathlib import Path

import pytest

_APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_APP))
os.environ.setdefault("ROUTE_API_BASE_URL", "http://route-api:18100")
os.environ.setdefault("FEATURE_ROUTE", "1")
os.environ.setdefault("FEATURE_TOUR", "1")

import nav_context as nc                                   # noqa: E402


# ── 1. 장애 유형 값 정규화 ───────────────────────────────────
@pytest.fixture(scope="module")
def th():
    return pytest.importorskip("tool_handlers")


@pytest.mark.parametrize("given, expected", [
    (["지체장애/휠체어"], ["지체장애", "휠체어"]),          # 현장 실증에서 0건이 난 값
    (["wheelchair"], ["휠체어"]),                          # 리허설에서 0건이 난 값
    ("지체장애, 시각장애", ["지체장애", "시각장애"]),
    (["전동휠체어"], ["휠체어"]),
    (["시각", "청각 장애", "유모차"], ["시각장애", "청각장애", "영유아동반"]),
    (["지체장애", "지체장애"], ["지체장애"]),               # 중복 제거
    (["모르는값"], ["지체장애"]),                          # 알아볼 값이 없으면 기본
    ([], ["지체장애"]),
    (None, ["지체장애"]),
    (["지체장애·영유아 동반"], ["지체장애", "영유아동반"]),
    (["시각장애 & 청각장애"], ["시각장애", "청각장애"]),
    (["시각장애와 청각장애"], ["시각장애", "청각장애"]),     # 구분자 없이 이어 온 값
])
def test_normalize_disabilities(th, given, expected):
    assert th.normalize_disabilities(given) == expected


def test_normalized_values_are_known_to_route_service(th):
    for v in th.normalize_disabilities(["지체장애/휠체어/시각장애/청각장애/영유아동반"]):
        assert v in th.DISABILITY_TYPES


def test_tour_tool_sends_normalized_values(th):
    import route_client
    seen = {}

    async def fake_call(method, path, *, params=None, json=None):
        seen["json"] = json
        return {"total": 0, "has_more": False, "items": []}
    orig = route_client._call
    route_client._call = fake_call
    try:
        asyncio.run(th.tool_find_bf_tour_spots(disabilities=["지체장애/휠체어"]))
    finally:
        route_client._call = orig
    assert seen["json"]["disabilities"] == ["지체장애", "휠체어"]


# ── 2. start_guidance ───────────────────────────────────────
def test_start_guidance_with_route_sends_ui_action():
    r = nc.start_guidance_result({"route_id": "r1", "guiding": False, "step_idx": 0, "total_steps": 5})
    assert r["status"] == "success"
    assert r["ui_action"] == {"action": "start_guidance"}
    assert "한 문장" in r["ai_instruction"]


def test_start_guidance_without_route_asks_destination():
    for nav in ({}, None, {"route_id": None, "guiding": False}):
        r = nc.start_guidance_result(nav)
        assert r["status"] == "no_route" and "ui_action" not in r
        assert "목적지" in r["ai_instruction"]


def test_start_guidance_while_guiding_does_not_restart():
    r = nc.start_guidance_result({"route_id": "r1", "guiding": True, "step_idx": 3, "total_steps": 9})
    assert r["status"] == "already_guiding" and "ui_action" not in r


def test_start_guidance_after_arrival_is_no_route():
    r = nc.start_guidance_result({"route_id": "r1", "guiding": False, "step_idx": 4, "total_steps": 5})
    assert r["status"] == "no_route"


# ── 3. 경로 성공 뒤 후속 문구 ────────────────────────────────
def test_route_success_before_guidance_asks_to_start():
    res = {"status": "success", "route_id": "r2", "ai_instruction": "요약하세요."}
    nc.annotate_route_success(res, {"route_id": "r1", "guiding": False})
    assert "안내를 시작할까요" in res["ai_instruction"] and "start_guidance" in res["ai_instruction"]
    assert "guidance_continues" not in res


def test_route_success_while_guiding_says_it_continues():
    nav = {"route_id": "r1", "guiding": True}
    res = {"status": "success", "route_id": "r2", "ai_instruction": "요약하세요."}
    nc.annotate_route_success(res, nav)
    assert res["guidance_continues"] is True
    assert "이어" in res["ai_instruction"] and "안내를 시작할까요" not in res["ai_instruction"]
    # 그 뒤에 부르는 note_new_route 는 종전대로 안내 상태를 비운다
    nc.note_new_route(nav, "r2")
    assert nav == {"route_id": "r2", "guiding": False}


def test_route_failure_is_not_annotated():
    res = {"status": "place_not_found", "ai_instruction": "x"}
    nc.annotate_route_success(res, {"guiding": True})
    assert res["ai_instruction"] == "x"


# ── 4. 안내 중이 아닐 때의 안내 조회 ─────────────────────────
def test_idle_guidance_offers_voice_start():
    r = nc.current_guidance_result({"route_id": "r1", "guiding": False, "step_idx": 0, "total_steps": 5})
    assert r["status"] == "idle" and "start_guidance" in r["ai_instruction"]


# ── 5. 도구 선언 ────────────────────────────────────────────
def test_local_fallback_declares_start_guidance():
    lp = pytest.importorskip("local_pipeline")
    src = (_APP / "local_pipeline.py").read_text(encoding="utf-8")
    assert 'fn("start_guidance"' in src
    assert "result = start_guidance_result(_nav)" in src
    assert lp is not None


def test_live_declares_start_guidance_and_single_value_types():
    lb = pytest.importorskip("live_bridge")
    # 선언은 Tool 묶음 여러 개로 온다 — 그 안의 함수 선언을 펼친다
    decls = [d for t in lb.build_tool_declarations() if t is not None
             for d in (getattr(t, "function_declarations", None) or [])]
    names = [d.name for d in decls]
    assert "start_guidance" in names
    tour = next(d for d in decls if d.name == "find_bf_tour_spots")
    items = tour.parameters.properties["disabilities"].items
    assert list(items.enum) == ["지체장애", "시각장애", "청각장애", "영유아동반"]
    assert "/" not in (tour.parameters.properties["disabilities"].description or "")


def test_input_transcription_is_korean():
    lb = pytest.importorskip("live_bridge")
    cfg = lb.input_transcription_config()
    codes = getattr(cfg, "language_codes", None)
    if codes is None:
        pytest.skip("이 SDK 버전은 전사 언어 지정을 지원하지 않는다")
    assert list(codes) == ["ko-KR"]


def test_input_transcription_falls_back_when_unsupported(monkeypatch):
    lb = pytest.importorskip("live_bridge")

    class _Old:   # 언어 지정 인자를 모르는 옛 SDK 대역
        def __init__(self, **kw):
            if kw:
                raise TypeError("unexpected keyword")
    monkeypatch.setattr(lb.types, "AudioTranscriptionConfig", _Old)
    assert isinstance(lb.input_transcription_config(), _Old)


# ── 6. 새 경로로 넘어가는 사이 ───────────────────────────────
def test_switching_between_routes():
    nav = {"route_id": "r1", "guiding": True, "step_idx": 2, "total_steps": 9}
    res = {"status": "success", "route_id": "r2", "ai_instruction": "요약."}
    nc.annotate_route_success(res, nav)
    nc.note_new_route(nav, "r2", continues=bool(res.get("guidance_continues")))
    # 화면의 nav_state 가 오기 전 — 안내가 꺼졌다고 답하지 않고, 안내 시작도 다시 걸지 않는다
    assert nc.current_guidance_result(nav)["status"] == "switching"
    assert nc.start_guidance_result(nav)["status"] == "already_guiding"
    # 화면이 실제 상태를 알려 오면 표시는 사라진다
    nc.update_nav_state(nav, {"route_id": "r2", "guiding": True, "step_idx": 0, "total_steps": 4, "current": "앞으로"})
    assert "switching" not in nav
    assert nc.current_guidance_result(nav)["status"] == "guiding"


def test_single_step_route_is_not_arrived_before_start():
    r = nc.start_guidance_result({"route_id": "r1", "guiding": False, "step_idx": 0, "total_steps": 1})
    assert r["status"] == "success"
