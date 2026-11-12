# -*- coding: utf-8 -*-
"""말로 하는 안내 제어 — 멈춤·재개·지도 크기, 말없이 처리 (v2.28.0). 외부 호출 없음."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nav_context as nc   # noqa: E402

GUIDING = {"route_id": "r1", "guiding": True, "step_idx": 2, "total_steps": 9, "current": "만안로를 따라 120m"}


def test_start_is_silent():
    r = nc.start_guidance_result({"route_id": "r1", "guiding": False, "step_idx": 0, "total_steps": 5})
    assert r["ui_action"] == {"action": "start_guidance"}
    assert r["ai_instruction"] == nc.SILENT_INSTRUCTION


def test_pause_while_guiding():
    r = nc.pause_guidance_result(dict(GUIDING))
    assert r["status"] == "success" and r["ui_action"] == {"action": "pause_guidance"}
    assert r["ai_instruction"] == nc.SILENT_INSTRUCTION


def test_pause_without_guidance_says_so():
    r = nc.pause_guidance_result({"route_id": "r1", "guiding": False})
    assert r["status"] == "idle" and "ui_action" not in r


def test_resume_after_pause():
    nav = dict(GUIDING, paused=True)
    r = nc.start_guidance_result(nav)
    assert r["status"] == "success" and r.get("resumed") is True
    assert r["ui_action"] == {"action": "start_guidance"}


def test_start_while_guiding_not_paused_does_not_restart():
    assert nc.start_guidance_result(dict(GUIDING))["status"] == "already_guiding"


def test_paused_state_comes_from_screen():
    nav = {}
    nc.update_nav_state(nav, dict(GUIDING, paused=True))
    assert nav["paused"] is True
    nc.update_nav_state(nav, dict(GUIDING))
    assert nav["paused"] is False


def test_guidance_query_while_paused():
    r = nc.current_guidance_result(dict(GUIDING, paused=True))
    assert r["status"] == "paused" and "다시 안내해줘" in r["ai_instruction"]


@pytest.mark.parametrize("given, d", [("in", "in"), ("확대", "in"), ("크게", "in"), ("out", "out"), ("축소", "out"), ("작게", "out")])
def test_map_zoom_direction(given, d):
    r = nc.map_zoom_result(given)
    assert r["ui_action"] == {"action": "map_zoom", "dir": d}
    assert r["ai_instruction"] == nc.SILENT_INSTRUCTION


def test_map_zoom_unknown_direction_asks():
    r = nc.map_zoom_result("")
    assert r["status"] == "idle" and "ui_action" not in r


def test_map_zoom_on_policy_screen_does_nothing():
    r = nc.map_zoom_result("in", mode="policy")
    assert r["status"] == "idle" and "ui_action" not in r


def test_live_declares_control_tools():
    lb = pytest.importorskip("live_bridge")
    decls = [d for t in lb.build_tool_declarations() if t is not None for d in (getattr(t, "function_declarations", None) or [])]
    names = {d.name for d in decls}
    assert {"start_guidance", "pause_guidance", "map_zoom"} <= names
    z = next(d for d in decls if d.name == "map_zoom")
    assert list(z.parameters.properties["direction"].enum) == ["in", "out"]
