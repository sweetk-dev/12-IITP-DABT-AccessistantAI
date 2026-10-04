"""단말 재접속 이어받기 저장소 (v1.59.0) — sid 는 계정별로 따로, 만료·상한 정리."""
import sys
from pathlib import Path

import pytest

# 어느 디렉터리에서 실행해도 대상 모듈을 찾게 한다(스크립트로 직접 실행할 때 포함)
_APP = Path(__file__).resolve().parents[1]
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

# live_bridge 는 Gemini SDK·DB 계층·수치 라이브러리를 import 한다 — 없는 환경에서는 건너뛴다
pytest.importorskip("google.genai")
pytest.importorskip("sqlalchemy.orm")
pytest.importorskip("pgvector")
pytest.importorskip("asyncpg")
pytest.importorskip("numpy")

import live_bridge as lb                                 # noqa: E402


class _WS:
    def __init__(self, user):
        self.headers = {"x-remote-user": user} if user else {}


def setup_function(_):
    lb._CLIENT_RESUME.clear()


def test_key_is_per_account_and_validates_sid():
    assert lb._resume_key(_WS("tester"), "sabc12345") == ("tester", "sabc12345")
    assert lb._resume_key(_WS("P1"), "sabc12345") == ("p1", "sabc12345")
    assert lb._resume_key(_WS("tester"), None) is None
    assert lb._resume_key(_WS("tester"), "../etc") is None
    assert lb._resume_key(_WS("tester"), "x" * 80) is None


def test_put_get_roundtrip_and_other_account_cannot_read():
    k = lb._resume_key(_WS("tester"), "sabc12345")
    lb._client_resume_put(k, "H1", [("user", "안녕"), ("model", "네")])
    ent = lb._client_resume_get(k)
    assert ent["handle"] == "H1" and ent["history"] == [("user", "안녕"), ("model", "네")]
    assert lb._client_resume_get(lb._resume_key(_WS("P1"), "sabc12345")) is None


def test_expired_entry_is_dropped(monkeypatch):
    k = lb._resume_key(_WS("tester"), "sabc12345")
    lb._client_resume_put(k, "H1", [])
    now = lb._time.time()
    monkeypatch.setattr(lb._time, "time", lambda: now + lb._CLIENT_RESUME_TTL + 1)
    assert lb._client_resume_get(k) is None
    assert k not in lb._CLIENT_RESUME


def test_store_is_bounded_and_history_is_trimmed():
    for i in range(lb._CLIENT_RESUME_MAX + 20):
        lb._client_resume_put(("tester", "s%07d" % i), None, [("user", str(j)) for j in range(40)])
    assert len(lb._CLIENT_RESUME) <= lb._CLIENT_RESUME_MAX
    assert len(next(iter(lb._CLIENT_RESUME.values()))["history"]) == 16


def test_no_key_is_noop():
    lb._client_resume_put(None, "H", [])
    assert lb._CLIENT_RESUME == {}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
