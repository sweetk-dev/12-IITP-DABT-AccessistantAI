"""길안내 화면 세션 지침·신호 이름 지우기 (v2.18.0).

현장 점검(2026-10-20): 길안내 도중 Gemini 정기 재연결 뒤 잡음 전사("17")에 모델이 정책상담 인사말을
"[SYSTEM:GREETING]" 까지 붙여 그대로 말했다. 길안내 지침에서 그 인사말을 빼고, 출력 전사에서 신호 이름을 지운다.
"""
import sys
from pathlib import Path

import pytest

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


def test_navi_instruction_drops_policy_greeting():
    base = lb.SYSTEM_INSTRUCTION
    navi = lb.navi_system_instruction(base)
    assert "장애인 복지 정책에 대해 궁금한 점이 있으신가요" in base
    assert "장애인 복지 정책에 대해 궁금한 점이 있으신가요" not in navi       # 베껴 말할 문장이 없다
    assert "이동 경로와 무장애 관광지 안내를 도와드릴게요" in navi            # 길안내 인사말은 그대로
    assert "GREETING_NAVI" in navi


def test_instruction_forbids_greeting_on_user_speech():
    assert "인사말은 위 인사 신호" in lb.SYSTEM_INSTRUCTION
    assert "잘 못 들었어요. 다시 말씀해 주시겠어요?" in lb.SYSTEM_INSTRUCTION


def test_navi_instruction_unknown_base_is_untouched():
    assert lb.navi_system_instruction("다른 지침") == "다른 지침"


@pytest.mark.parametrize("raw,want", [
    ("[SYSTEM:GREETING]", ""),
    ("[SYSTEM:GREETING] 안녕하세요!", " 안녕하세요!"),
    ("[SYSTEM:", ""),
    (" 안녕하세요!", " 안녕하세요!"),
    ("SYSTEM 점검 안내", "SYSTEM 점검 안내"),
    (None, None),
])
def test_strip_signal_tokens(raw, want):
    assert lb.strip_signal_tokens(raw) == want
