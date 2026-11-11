# -*- coding: utf-8 -*-
"""Gemini 클라이언트 생성 — API 버전 명시와 SDK 버전 범위 (v2.25.3). 외부 호출 없음."""
import re
import sys
import types
from pathlib import Path

_APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_APP))

import genai_client  # noqa: E402


class _FakeClient:
    """genai.Client 대역 — 받은 인자만 기록한다."""
    def __init__(self, **kw):
        self.kw = kw


def _install_fake_sdk(monkeypatch):
    google = types.ModuleType("google")
    genai = types.ModuleType("google.genai")
    genai.Client = _FakeClient
    google.genai = genai
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.genai", genai)


def test_client_always_passes_api_version(monkeypatch):
    _install_fake_sdk(monkeypatch)
    monkeypatch.delenv("GEMINI_API_VERSION", raising=False)
    c = genai_client.make_client("k")
    assert c.kw["api_key"] == "k"
    assert c.kw["http_options"] == {"api_version": "v1beta"}


def test_api_version_env_override(monkeypatch):
    _install_fake_sdk(monkeypatch)
    monkeypatch.setenv("GEMINI_API_VERSION", " v1alpha ")
    assert genai_client.make_client("k").kw["http_options"] == {"api_version": "v1alpha"}
    monkeypatch.setenv("GEMINI_API_VERSION", "")
    assert genai_client.api_version() == "v1beta"


def test_no_key_returns_none():
    assert genai_client.make_client("") is None
    assert genai_client.make_client(None) is None


def test_no_direct_client_construction():
    """클라이언트를 직접 만드는 곳이 남아 있으면 API 버전 명시가 빠진다."""
    hits = []
    for p in _APP.rglob("*.py"):
        if p.name == "genai_client.py" or "tests" in p.parts:
            continue
        if re.search(r"genai\.Client\(", p.read_text(encoding="utf-8")):
            hits.append(str(p.relative_to(_APP)))
    assert hits == []


def test_sdk_major_version_is_pinned():
    line = next(l for l in (_APP / "requirements.txt").read_text(encoding="utf-8").splitlines()
                if l.startswith("google-genai"))
    assert re.search(r"<\s*3(\D|$)", line), line
